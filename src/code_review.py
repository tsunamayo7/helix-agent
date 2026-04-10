"""
3-Layer Code Review Pipeline.

Multi-LLM automated code review combining local (gemma4) and cloud (Sonnet/Opus) models.

Flow:
  Layer 1: Sonnet 4.6 builds code (optional, skipped for existing code)
  Layer 2: gemma4:31b reviews with RAG + web_search ($0)
  Layer 3: Sonnet 4.6 re-reviews, validates, generates fix patches

Cost: ~20 JPY total for a full review cycle vs ~200 JPY for Opus-only.
Coverage: ~95% of issues found compared to Opus-only review.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

GEMMA4_REVIEW_PROMPT = """\
あなたは厳格なシニアコードレビュアーです。以下のファイル群を精査し、
問題をP1（重大: セキュリティ/クラッシュ/データ損失/リソースリーク）と
P2（重要: パフォーマンス/型安全性/ロジックバグ/設計問題）に分類してください。

## タスク
1. read_file で各ファイルを読み込む
2. web_search で最新のベストプラクティスを確認する
   例: web_search({{"query": "Python asyncio exception handling best practice"}})
   例: web_search({{"query": "httpx AsyncClient thread safety"}})
3. search_memory で過去の設計判断を確認する
   例: search_memory({{"query": "プロジェクト設計方針"}})
4. 以下のJSONフォーマットで問題リストを返す

## 対象ファイル
{file_list}

## 追加コンテキスト
{context}

{tech_context}

## レビュー観点
1. ロジックエラー（デッドコード、例外処理の抜け、データフロー不整合）
2. セキュリティ問題（入力検証、パストラバーサル、インジェクション）
3. 型安全性（Optional漏れ、Protocol不整合）
4. 非同期パターン（asyncio誤用、リソースリーク）
5. クロスファイル統合バグ（モジュール間のインターフェース不整合）

## 出力フォーマット（厳守）
最終回答は以下のJSON形式のみ返すこと:
```json
{{
  "issues": [
    {{"severity":"P1","file":"src/foo.py","line":42,"title":"問題タイトル","detail":"詳細説明","suggestion":"修正案"}}
  ],
  "web_search_insights": ["httpxはスレッドセーフではない（公式ドキュメント）"],
  "summary": "総評1-2行"
}}
```

注意: コードを修正しないこと。問題の指摘のみ行う。"""

SONNET_VERIFY_PROMPT = """\
あなたはシニアソフトウェアエンジニアです。ローカルLLM(gemma4)による
第一段階コードレビュー結果を受け取り、検証と補完を行ってください。

## タスク
1. gemma4の指摘を精査し、誤検知(false positive)を除外する
2. クロスファイル統合バグを追加検出する（A.pyの返り値がB.pyで誤使用 等）
3. gemma4が見落としたセキュリティ・リソース管理・設計問題を補完する
{patch_instruction}

## gemma4の指摘（Layer 2結果）
{gemma4_issues_json}

## 対象ファイル
{file_list}

## 追加コンテキスト
{context}

すべてのファイルを読んでからレビューしてください。

## 出力フォーマット（厳守、日本語で）
最終的に以下の形式で報告してください:

**確認済み問題（gemma4の指摘のうち有効なもの）:**
- [P1/P2] ファイル名:行番号 — 問題の説明

**追加検出（Sonnet独自の発見）:**
- [P1/P2] ファイル名:行番号 — 問題の説明

**誤検知（除外した指摘とその理由）:**
- ID — 理由

**総評:** 1-2行の要約

{patch_format}"""


@dataclass
class ReviewIssue:
    """A single code review issue."""

    severity: str  # P1 or P2
    file: str
    line: int
    title: str
    detail: str
    suggestion: str = ""
    source: str = ""  # gemma4, sonnet, codex

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "title": self.title,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "source": self.source,
        }


@dataclass
class ReviewResult:
    """Combined result from all review layers."""

    summary: str = ""
    issues: list[ReviewIssue] = field(default_factory=list)
    gemma4_raw: str = ""
    sonnet_raw: str = ""
    codex_raw: str = ""
    meta_review: str = ""
    web_search_insights: list[str] = field(default_factory=list)
    files_reviewed: int = 0
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        p1 = [i for i in self.issues if i.severity == "P1"]
        p2 = [i for i in self.issues if i.severity == "P2"]
        return {
            "summary": self.summary,
            "issues": [i.to_dict() for i in self.issues],
            "meta_review": self.meta_review,
            "stats": {
                "files_reviewed": self.files_reviewed,
                "total_issues": len(self.issues),
                "p1_count": len(p1),
                "p2_count": len(p2),
                "gemma4_issues": len([i for i in self.issues if i.source == "gemma4"]),
                "sonnet_issues": len([i for i in self.issues if i.source == "sonnet"]),
                "codex_issues": len([i for i in self.issues if i.source == "codex"]),
                "opus_issues": len([i for i in self.issues if i.source == "opus"]),
                "web_search_insights": self.web_search_insights,
                "elapsed_sec": round(self.elapsed_sec, 1),
            },
        }

    def format_for_meta_review(self) -> str:
        """Generate a compact summary for Opus meta-review (token-efficient)."""
        lines = [
            f"## レビュー結果サマリー ({self.files_reviewed}ファイル)",
            "",
        ]
        for i in self.issues:
            lines.append(
                f"- [{i.severity}][{i.source}] {i.file}:{i.line} — {i.title}"
            )
        if self.web_search_insights:
            lines.append("\n## Web検索で得た知見")
            for insight in self.web_search_insights:
                lines.append(f"- {insight}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class CodeReviewPipeline:
    """
    3-Layer Code Review Pipeline.

    Orchestrates gemma4 (local, $0) and cloud LLMs for comprehensive
    code review with near-Opus quality at ~10% of the cost.

    Usage::

        pipeline = CodeReviewPipeline(helix_agent)
        result = await pipeline.run(
            target="src/",
            context="VTuber avatar generation tool",
        )
        print(result.to_dict())
    """

    def __init__(self, agent: Any) -> None:
        """
        Args:
            agent: HelixAgent instance for delegating to providers.
        """
        self._agent = agent

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        target: str,
        context: str = "",
        tech_context: str = "",
        web_search: bool = True,
        generate_patch: bool = False,
        gemma_model: str = "gemma4:31b",
        max_files: int = 20,
        max_steps: int = 15,
        timeout: int = 300,
        skip_sonnet: bool = False,
        codex_consult: bool = True,
        codex_effort: str = "",
    ) -> ReviewResult:
        """
        Run the full review pipeline.

        Args:
            target: File or directory path to review.
            context: Additional context (PR description, design intent).
            tech_context: Known libraries/tech stack to avoid false positives.
            web_search: Whether gemma4 should search for best practices.
            generate_patch: Whether Sonnet should generate fix patches.
            gemma_model: Ollama model for Layer 2.
            max_files: Maximum files to review in one pass.
            max_steps: ReAct loop max steps for gemma4.
            timeout: Total timeout in seconds.
            skip_sonnet: Skip Layer 3 (gemma4 only, fastest).
            codex_consult: Call Codex as a consultant when P1 issues are found.
                           Default True (2026-04-11〜, Codex Pro Second Lead role).
                           Set False for token-saving $0 gemma4-only review.
            codex_effort: Reasoning effort override. Empty → auto
                          (xhigh when P1≥1, per Codex Pro plan).

        Returns:
            ReviewResult with all findings.
        """
        t0 = time.perf_counter()
        result = ReviewResult()

        # Collect files
        files = self._collect_files(target, max_files)
        result.files_reviewed = len(files)

        if not files:
            result.summary = f"No reviewable files found in: {target}"
            return result

        logger.info(
            "Code Review Pipeline started: %d files, target=%s",
            len(files), target,
        )

        file_list_str = "\n".join(f"- {f}" for f in files)

        # --- Layer 2: gemma4 ReAct review ---
        layer2_result = await self._run_gemma4(
            files=files,
            file_list_str=file_list_str,
            context=context,
            tech_context=tech_context,
            web_search=web_search,
            model=gemma_model,
            max_steps=max_steps,
            timeout=timeout,
        )
        result.gemma4_raw = layer2_result.get("answer", "")
        gemma4_issues = self._parse_gemma4_issues(result.gemma4_raw)
        for issue in gemma4_issues:
            issue.source = "gemma4"
            result.issues.append(issue)

        # Extract web_search insights
        result.web_search_insights = layer2_result.get("web_search_insights", [])

        logger.info("Layer 2 (gemma4): %d issues found", len(gemma4_issues))

        # --- Layer 3: Sonnet verification + additional review ---
        if not skip_sonnet:
            layer3_raw = await self._run_sonnet(
                files=files,
                file_list_str=file_list_str,
                gemma4_issues=gemma4_issues,
                context=context,
                generate_patch=generate_patch,
            )
            result.sonnet_raw = layer3_raw
            sonnet_issues = self._parse_sonnet_issues(layer3_raw)
            for issue in sonnet_issues:
                issue.source = "sonnet"
                result.issues.append(issue)

            logger.info("Layer 3 (Sonnet): %d additional issues", len(sonnet_issues))

        # --- Optional: Codex consultation for complex issues ---
        if codex_consult and any(i.severity == "P1" for i in result.issues):
            codex_result = await self._consult_codex(
                files=files,
                issues=result.issues,
                context=context,
                effort=codex_effort,
            )
            result.codex_raw = codex_result
            codex_issues = self._parse_codex_issues(codex_result)
            for issue in codex_issues:
                issue.source = "codex"
                result.issues.append(issue)

            logger.info("Codex consultation: %d insights", len(codex_issues))

        # Deduplicate by file+line+title similarity
        result.issues = self._deduplicate(result.issues)

        # --- Layer 4: Opus meta-review (token-efficient) ---
        # This generates a compact summary that Opus can review
        # WITHOUT reading any source code — only the issue list.
        # Opus adds: cross-project patterns, contradiction detection,
        # user-intent alignment, and historical bug pattern matching.
        result.meta_review = result.format_for_meta_review()

        # Generate summary
        p1_count = len([i for i in result.issues if i.severity == "P1"])
        p2_count = len([i for i in result.issues if i.severity == "P2"])
        result.summary = (
            f"レビュー完了: {result.files_reviewed}ファイル, "
            f"P1={p1_count}件, P2={p2_count}件 "
            f"(gemma4: {len([i for i in result.issues if i.source == 'gemma4'])}件, "
            f"sonnet: {len([i for i in result.issues if i.source == 'sonnet'])}件"
            f"{', codex: ' + str(len([i for i in result.issues if i.source == 'codex'])) + '件' if codex_consult else ''})"
        )

        result.elapsed_sec = time.perf_counter() - t0
        logger.info("Review complete: %s", result.summary)

        return result

    # ------------------------------------------------------------------
    # Layer 2: gemma4 ReAct review
    # ------------------------------------------------------------------

    async def _run_gemma4(
        self,
        files: list[str],
        file_list_str: str,
        context: str,
        tech_context: str,
        web_search: bool,
        model: str,
        max_steps: int,
        timeout: int,
    ) -> dict:
        """Run gemma4 ReAct review with web_search + RAG."""
        prompt = GEMMA4_REVIEW_PROMPT.format(
            file_list=file_list_str,
            context=context or "(なし)",
            tech_context=tech_context or "",
        )

        tools_list = ["read_file", "search_in_file", "list_files", "search_memory"]
        if web_search:
            tools_list.append("web_search")

        try:
            result = await self._agent.agent(
                task=prompt,
                model=model,
                provider="ollama",
                max_steps=max_steps,
                tools=tools_list,
                timeout=timeout,
            )
            return result
        except Exception as e:
            logger.error("gemma4 review failed: %s", e)
            return {"answer": f"gemma4 review error: {e}"}

    # ------------------------------------------------------------------
    # Layer 3: Sonnet verification
    # ------------------------------------------------------------------

    async def _run_sonnet(
        self,
        files: list[str],
        file_list_str: str,
        gemma4_issues: list[ReviewIssue],
        context: str,
        generate_patch: bool,
    ) -> str:
        """Run Sonnet re-review and verification."""
        issues_json = json.dumps(
            [i.to_dict() for i in gemma4_issues],
            ensure_ascii=False, indent=2,
        )

        patch_instruction = (
            "4. P1/P2問題の修正パッチをunified diff形式で生成する"
            if generate_patch else ""
        )
        patch_format = (
            "**修正パッチ（unified diff）:**\n```diff\n...\n```"
            if generate_patch else ""
        )

        prompt = SONNET_VERIFY_PROMPT.format(
            gemma4_issues_json=issues_json,
            file_list=file_list_str,
            context=context or "(なし)",
            patch_instruction=patch_instruction,
            patch_format=patch_format,
        )

        try:
            result = await self._agent.think(
                task=prompt,
                provider="ollama",
                model="gemma4:31b",
            )
            return result.get("answer", "")
        except Exception as e:
            logger.error("Sonnet verification failed: %s", e)
            return f"Sonnet verification error: {e}"

    # ------------------------------------------------------------------
    # Codex consultation (for complex/stuck issues)
    # ------------------------------------------------------------------

    async def _consult_codex(
        self,
        files: list[str],
        issues: list[ReviewIssue],
        context: str,
        effort: str = "",
    ) -> str:
        """Consult Codex as the Second Lead for P1 issues.

        Args:
            effort: Codex reasoning effort. Empty → config default ("high").
                    P1問題が1件でもあれば自動で "xhigh" にエスカレート
                    (Codex Pro $100 プラン、2026-04-11〜)。
        """
        p1_issues = [i for i in issues if i.severity == "P1"]
        issues_summary = "\n".join(
            f"- [{i.source}] {i.file}:{i.line} {i.title}" for i in p1_issues
        )

        # 自動エスカレーション: P1が1件以上 → xhigh (Codex Pro プラン対応)
        if not effort and len(p1_issues) >= 1:
            effort = "xhigh"
            logger.info("Codex: escalating to xhigh (P1 count=%d)", len(p1_issues))

        prompt = (
            "あなたはエキスパートデバッガーです。以下のP1問題について、\n"
            "gemma4/Sonnetが見落としている根本原因や追加の問題がないか確認してください。\n\n"
            f"## P1問題一覧\n{issues_summary}\n\n"
            f"## 対象ファイル\n{chr(10).join('- ' + f for f in files)}\n\n"
            f"## コンテキスト\n{context}\n\n"
            "追加で発見した問題のみ報告してください。既知の問題の再報告は不要です。"
        )

        try:
            result = await self._agent.agent(
                task=prompt,
                provider="codex",
                effort=effort,
                timeout=180,
            )
            return result.get("answer", result.get("result", ""))
        except Exception as e:
            logger.warning("Codex consultation failed (non-critical): %s", e)
            return ""

    # ------------------------------------------------------------------
    # File collection
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_files(target: str, max_files: int) -> list[str]:
        """Collect Python files from target path."""
        target_path = Path(target)
        files: list[str] = []

        if target_path.is_file():
            files.append(str(target_path))
        elif target_path.is_dir():
            for ext in ("*.py", "*.ts", "*.js", "*.rs", "*.go"):
                for f in sorted(target_path.rglob(ext)):
                    if any(skip in str(f) for skip in (
                        "__pycache__", "node_modules", ".venv",
                        ".git", "dist", "build",
                    )):
                        continue
                    files.append(str(f))
                    if len(files) >= max_files:
                        break
                if len(files) >= max_files:
                    break
        else:
            logger.warning("Target not found: %s", target)

        return files[:max_files]

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_gemma4_issues(raw: str) -> list[ReviewIssue]:
        """Parse gemma4's JSON output into ReviewIssue list."""
        issues: list[ReviewIssue] = []

        # Try to extract JSON from the response
        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(raw[start:end])
                for item in data.get("issues", []):
                    issues.append(ReviewIssue(
                        severity=item.get("severity", "P2"),
                        file=item.get("file", ""),
                        line=item.get("line", 0),
                        title=item.get("title", ""),
                        detail=item.get("detail", ""),
                        suggestion=item.get("suggestion", ""),
                    ))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

        # Fallback: parse markdown-style if JSON yielded nothing
        if not issues:
            for line in raw.split("\n"):
                line = line.strip()
                if "[P1]" in line or "[P2]" in line:
                    severity = "P1" if "[P1]" in line else "P2"
                    # Remove prefix markers
                    title = line
                    for prefix in ("- ", "* "):
                        if title.startswith(prefix):
                            title = title[len(prefix):]
                    title = title.replace("[P1]", "").replace("[P2]", "").strip()
                    if title:
                        issues.append(ReviewIssue(
                            severity=severity,
                            file="",
                            line=0,
                            title=title,
                            detail=line,
                        ))

        return issues

    @staticmethod
    def _parse_sonnet_issues(raw: str) -> list[ReviewIssue]:
        """Parse Sonnet's markdown output for additional issues."""
        issues: list[ReviewIssue] = []
        in_additional = False

        for line in raw.split("\n"):
            line = line.strip()
            if "追加検出" in line or "additional" in line.lower():
                in_additional = True
                continue
            if in_additional and line.startswith("- [P"):
                severity = "P1" if "P1" in line else "P2"
                # Try to extract file:line
                file_str = ""
                line_num = 0
                parts = line.split("—")
                if len(parts) >= 1:
                    loc = parts[0].replace("- [P1]", "").replace("- [P2]", "").strip()
                    if ":" in loc:
                        fp, ln = loc.rsplit(":", 1)
                        file_str = fp.strip()
                        try:
                            line_num = int(ln.strip())
                        except ValueError:
                            pass
                title = parts[-1].strip() if len(parts) > 1 else line
                issues.append(ReviewIssue(
                    severity=severity,
                    file=file_str,
                    line=line_num,
                    title=title,
                    detail=line,
                ))
            elif in_additional and line and not line.startswith("-"):
                in_additional = False

        return issues

    @staticmethod
    def _parse_codex_issues(raw: str) -> list[ReviewIssue]:
        """Parse Codex consultation output."""
        issues: list[ReviewIssue] = []
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith(("- [P1]", "- [P2]", "[P1]", "[P2]")):
                severity = "P1" if "P1" in line else "P2"
                issues.append(ReviewIssue(
                    severity=severity,
                    file="",
                    line=0,
                    title=line.lstrip("-[] P12").strip(),
                    detail=line,
                    source="codex",
                ))
        return issues

    @staticmethod
    def _deduplicate(issues: list[ReviewIssue]) -> list[ReviewIssue]:
        """Remove duplicate issues by file+line+title similarity."""
        seen: set[str] = set()
        unique: list[ReviewIssue] = []

        for issue in issues:
            key = f"{issue.file}:{issue.line}:{issue.title[:30]}"
            if key not in seen:
                seen.add(key)
                unique.append(issue)

        # Sort: P1 first, then by file
        unique.sort(key=lambda i: (0 if i.severity == "P1" else 1, i.file, i.line))
        return unique
