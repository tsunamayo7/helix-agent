"""Tests for the 3-Layer Code Review Pipeline."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.code_review import CodeReviewPipeline, ReviewIssue, ReviewResult


# ---------------------------------------------------------------------------
# Unit tests (no external dependencies)
# ---------------------------------------------------------------------------


class TestCollectFiles:
    def test_single_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        files = CodeReviewPipeline._collect_files(str(f), max_files=10)
        assert len(files) == 1
        assert str(f) in files[0]

    def test_directory(self, tmp_path):
        for i in range(5):
            (tmp_path / f"mod_{i}.py").write_text(f"# module {i}")
        files = CodeReviewPipeline._collect_files(str(tmp_path), max_files=10)
        assert len(files) == 5

    def test_max_files_limit(self, tmp_path):
        for i in range(10):
            (tmp_path / f"mod_{i}.py").write_text(f"# module {i}")
        files = CodeReviewPipeline._collect_files(str(tmp_path), max_files=3)
        assert len(files) == 3

    def test_skips_pycache(self, tmp_path):
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "cached.py").write_text("# cached")
        (tmp_path / "real.py").write_text("# real")
        files = CodeReviewPipeline._collect_files(str(tmp_path), max_files=10)
        assert len(files) == 1
        assert "real.py" in files[0]

    def test_nonexistent_path(self):
        files = CodeReviewPipeline._collect_files("/nonexistent/path", max_files=10)
        assert files == []


class TestParseGemma4Issues:
    def test_json_format(self):
        raw = '{"issues": [{"severity": "P1", "file": "a.py", "line": 10, "title": "bug", "detail": "desc"}], "summary": "ok"}'
        issues = CodeReviewPipeline._parse_gemma4_issues(raw)
        assert len(issues) == 1
        assert issues[0].severity == "P1"
        assert issues[0].file == "a.py"
        assert issues[0].line == 10

    def test_json_with_surrounding_text(self):
        raw = 'Here is my review:\n{"issues": [{"severity": "P2", "file": "b.py", "line": 5, "title": "perf", "detail": "slow"}], "summary": "done"}\nEnd.'
        issues = CodeReviewPipeline._parse_gemma4_issues(raw)
        assert len(issues) == 1
        assert issues[0].severity == "P2"

    def test_markdown_fallback(self):
        raw = "Review:\n- [P1] Critical security issue\n- [P2] Minor style problem\n"
        issues = CodeReviewPipeline._parse_gemma4_issues(raw)
        assert len(issues) == 2

    def test_empty_response(self):
        issues = CodeReviewPipeline._parse_gemma4_issues("")
        assert issues == []


class TestParseSonnetIssues:
    def test_additional_section(self):
        raw = (
            "**確認済み問題:**\n"
            "- [P1] a.py:10 — confirmed\n\n"
            "**追加検出:**\n"
            "- [P1] b.py:20 — new security issue\n"
            "- [P2] c.py:30 — type safety\n\n"
            "**総評:** good"
        )
        issues = CodeReviewPipeline._parse_sonnet_issues(raw)
        assert len(issues) == 2
        assert issues[0].severity == "P1"
        assert issues[1].severity == "P2"


class TestDeduplicate:
    def test_removes_exact_duplicates(self):
        issues = [
            ReviewIssue("P1", "a.py", 10, "bug", "desc", source="gemma4"),
            ReviewIssue("P1", "a.py", 10, "bug", "desc2", source="sonnet"),
        ]
        result = CodeReviewPipeline._deduplicate(issues)
        assert len(result) == 1

    def test_keeps_different_issues(self):
        issues = [
            ReviewIssue("P1", "a.py", 10, "security", "desc", source="gemma4"),
            ReviewIssue("P2", "b.py", 20, "performance", "desc", source="sonnet"),
        ]
        result = CodeReviewPipeline._deduplicate(issues)
        assert len(result) == 2

    def test_sorts_p1_first(self):
        issues = [
            ReviewIssue("P2", "z.py", 1, "minor", ""),
            ReviewIssue("P1", "a.py", 1, "critical", ""),
        ]
        result = CodeReviewPipeline._deduplicate(issues)
        assert result[0].severity == "P1"


class TestReviewResult:
    def test_to_dict(self):
        result = ReviewResult(
            summary="test",
            issues=[
                ReviewIssue("P1", "a.py", 10, "bug", "detail", source="gemma4"),
                ReviewIssue("P2", "b.py", 20, "perf", "detail", source="sonnet"),
            ],
            files_reviewed=2,
            elapsed_sec=5.5,
        )
        d = result.to_dict()
        assert d["stats"]["p1_count"] == 1
        assert d["stats"]["p2_count"] == 1
        assert d["stats"]["gemma4_issues"] == 1
        assert d["stats"]["sonnet_issues"] == 1
        assert d["stats"]["files_reviewed"] == 2


# ---------------------------------------------------------------------------
# Integration-like tests (with mocked agent)
# ---------------------------------------------------------------------------


class TestPipelineRun:
    @pytest.fixture
    def mock_agent(self):
        agent = MagicMock()
        agent.agent = AsyncMock(return_value={
            "answer": json.dumps({
                "issues": [
                    {"severity": "P1", "file": "src/main.py", "line": 42,
                     "title": "SQL injection", "detail": "Unsanitized input"},
                ],
                "summary": "1 critical issue found",
            })
        })
        agent.think = AsyncMock(return_value={
            "answer": (
                "**確認済み問題:**\n"
                "- [P1] src/main.py:42 — SQL injection confirmed\n\n"
                "**追加検出:**\n"
                "- [P2] src/utils.py:10 — Missing error handling\n\n"
                "**総評:** 2 issues total"
            )
        })
        return agent

    @pytest.mark.asyncio
    async def test_full_pipeline(self, mock_agent, tmp_path):
        (tmp_path / "main.py").write_text("x = input()")
        pipeline = CodeReviewPipeline(mock_agent)
        result = await pipeline.run(
            target=str(tmp_path),
            context="test app",
            skip_sonnet=False,
        )
        assert result.files_reviewed == 1
        assert len(result.issues) >= 1
        assert any(i.source == "gemma4" for i in result.issues)

    @pytest.mark.asyncio
    async def test_gemma4_only(self, mock_agent, tmp_path):
        (tmp_path / "app.py").write_text("print(1)")
        pipeline = CodeReviewPipeline(mock_agent)
        result = await pipeline.run(
            target=str(tmp_path),
            skip_sonnet=True,
        )
        assert all(i.source == "gemma4" for i in result.issues)

    @pytest.mark.asyncio
    async def test_empty_target(self, mock_agent):
        pipeline = CodeReviewPipeline(mock_agent)
        result = await pipeline.run(target="/nonexistent/path")
        assert result.files_reviewed == 0
        assert "No reviewable files" in result.summary
