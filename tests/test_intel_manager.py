"""
Tests for scripts/intel_manager.py — Corp intelligence lifecycle management CLI.

Covers: Topics CRUD, Review, Queue Management, Cleanup, Summary, Status, Migrate.
"""

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# We import the module so we can monkeypatch its module-level path constants.
import scripts.intel_manager as im


# ── Fixtures & Helpers ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_paths(tmp_path, monkeypatch):
    """Redirect all module-level path constants to tmp_path subdirectories."""
    intel_dir = tmp_path / "intelligence"
    intel_dir.mkdir()

    monkeypatch.setattr(im, "INTEL_DIR", intel_dir)
    monkeypatch.setattr(im, "TOPICS_FILE", intel_dir / "topics.json")
    monkeypatch.setattr(im, "ROUTING_FILE", intel_dir / "routing.json")
    monkeypatch.setattr(im, "COLLECTED_DIR", intel_dir / "collected")
    monkeypatch.setattr(im, "SUMMARIES_DIR", intel_dir / "summaries")
    monkeypatch.setattr(im, "ARCHIVE_DIR", intel_dir / "archive")
    monkeypatch.setattr(im, "ACTION_QUEUE_FILE", intel_dir / "action_queue.json")
    monkeypatch.setattr(im, "X_MONITOR_DIR", tmp_path / "x_monitor")


def _write_topics(topics_list: list[dict], tmp_path: Path) -> None:
    """Write a topics.json file with the given topic entries."""
    data = {
        "version": 1,
        "updated_at": im.now_iso(),
        "topics": topics_list,
    }
    path = tmp_path / "intelligence" / "topics.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_routing(routes: dict, cleanup_policy: dict | None = None,
                   tmp_path: Path | None = None) -> None:
    """Write a routing.json file."""
    data = {
        "version": 1,
        "routes": routes,
        "cleanup_policy": cleanup_policy or {},
    }
    path = tmp_path / "intelligence" / "routing.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_collected(filename: str, entries: list[dict], tmp_path: Path) -> None:
    """Write a collected data file (e.g. '2026-05-15.json')."""
    collected = tmp_path / "intelligence" / "collected"
    collected.mkdir(parents=True, exist_ok=True)
    path = collected / filename
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_action_queue(queue: dict, tmp_path: Path) -> None:
    """Write action_queue.json."""
    path = tmp_path / "intelligence" / "action_queue.json"
    path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_topic(topic_id: str = "test-topic", query: str = "test query",
                category: str = "general", priority: str = "medium",
                enabled: bool = True) -> dict:
    """Create a minimal topic dict."""
    return {
        "id": topic_id,
        "query": query,
        "category": category,
        "priority": priority,
        "action_routes": [],
        "frequency_minutes": 60,
        "created_at": im.now_iso(),
        "expires_at": None,
        "enabled": enabled,
        "notes": "",
    }


# ── Topics CRUD Tests (~10) ────────────────────────────────────────


class TestMakeId:
    """Tests for make_id() kebab-case generation."""

    def test_simple_words(self):
        assert im.make_id("hello world test") == "hello-world-test"

    def test_four_words_truncated(self):
        assert im.make_id("one two three four five") == "one-two-three-four"

    def test_special_characters_stripped(self):
        assert im.make_id("Python 3.12 release!") == "python-312-release"

    def test_empty_string(self):
        assert im.make_id("") == "unnamed"

    def test_single_word(self):
        assert im.make_id("kubernetes") == "kubernetes"


class TestTopicsList:

    def test_empty_topics(self, tmp_path, capsys):
        _write_topics([], tmp_path)
        args = argparse.Namespace()
        im.topics_list(args)
        captured = capsys.readouterr()
        assert "No topics registered." in captured.out

    def test_with_multiple_topics(self, tmp_path, capsys):
        topics = [
            _make_topic("alpha", "alpha query", "tech_trend"),
            _make_topic("beta", "beta query", "security"),
        ]
        _write_topics(topics, tmp_path)
        args = argparse.Namespace()
        im.topics_list(args)
        captured = capsys.readouterr()
        assert "alpha" in captured.out
        assert "beta" in captured.out
        assert "tech_trend" in captured.out
        assert "security" in captured.out


class TestTopicsAdd:

    def test_creates_correct_entry(self, tmp_path, capsys):
        _write_topics([], tmp_path)
        args = argparse.Namespace(
            query="Claude Code MCP server",
            category="tech_trend",
            priority="high",
            routes="discord,log",
            frequency=30,
            notes="test note",
        )
        im.topics_add(args)
        captured = capsys.readouterr()
        assert "Added topic:" in captured.out

        data = json.loads((tmp_path / "intelligence" / "topics.json").read_text())
        assert len(data["topics"]) == 1
        t = data["topics"][0]
        assert t["id"] == "claude-code-mcp-server"
        assert t["category"] == "tech_trend"
        assert t["priority"] == "high"
        assert t["action_routes"] == ["discord", "log"]
        assert t["frequency_minutes"] == 30
        assert t["enabled"] is True

    def test_duplicate_id_gets_counter_suffix(self, tmp_path, capsys):
        existing = _make_topic("hello-world-test", "hello world test")
        _write_topics([existing], tmp_path)
        args = argparse.Namespace(
            query="hello world test",
            category=None,
            priority=None,
            routes="",
            frequency=None,
            notes=None,
        )
        im.topics_add(args)
        data = json.loads((tmp_path / "intelligence" / "topics.json").read_text())
        ids = [t["id"] for t in data["topics"]]
        assert "hello-world-test" in ids
        assert "hello-world-test-2" in ids


class TestTopicsModify:

    def test_changes_specified_fields_only(self, tmp_path, capsys):
        topic = _make_topic("my-topic", "original query", "general", "low")
        _write_topics([topic], tmp_path)
        args = argparse.Namespace(
            id="my-topic",
            query=None,
            priority="high",
            category=None,
            enabled=None,
            routes=None,
            frequency=None,
            notes=None,
        )
        im.topics_modify(args)
        data = json.loads((tmp_path / "intelligence" / "topics.json").read_text())
        t = data["topics"][0]
        assert t["priority"] == "high"
        assert t["query"] == "original query"  # unchanged
        assert t["category"] == "general"  # unchanged

    def test_nonexistent_id_exits_with_error(self, tmp_path):
        _write_topics([], tmp_path)
        args = argparse.Namespace(
            id="nonexistent",
            query=None, priority=None, category=None,
            enabled=None, routes=None, frequency=None, notes=None,
        )
        with pytest.raises(SystemExit):
            im.topics_modify(args)


class TestTopicsDelete:

    def test_removes_correct_topic(self, tmp_path, capsys):
        topics = [
            _make_topic("keep-me", "keep query"),
            _make_topic("delete-me", "delete query"),
        ]
        _write_topics(topics, tmp_path)
        args = argparse.Namespace(id="delete-me")
        im.topics_delete(args)
        data = json.loads((tmp_path / "intelligence" / "topics.json").read_text())
        ids = [t["id"] for t in data["topics"]]
        assert "delete-me" not in ids
        assert "keep-me" in ids

    def test_nonexistent_id_exits_with_error(self, tmp_path):
        _write_topics([], tmp_path)
        args = argparse.Namespace(id="ghost")
        with pytest.raises(SystemExit):
            im.topics_delete(args)


class TestTopicsDisable:

    def test_sets_enabled_false(self, tmp_path, capsys):
        topic = _make_topic("disable-me", "some query", enabled=True)
        _write_topics([topic], tmp_path)
        args = argparse.Namespace(id="disable-me")
        im.topics_disable(args)
        data = json.loads((tmp_path / "intelligence" / "topics.json").read_text())
        t = data["topics"][0]
        assert t["enabled"] is False


# ── Review Tests (~8) ──────────────────────────────────────────────


class TestReview:

    def _make_review_args(self, days: int = 7) -> argparse.Namespace:
        return argparse.Namespace(days=days)

    def test_no_collected_data_produces_empty_report(self, tmp_path, capsys):
        _write_topics([], tmp_path)
        im.cmd_review(self._make_review_args())
        captured = capsys.readouterr()
        assert "Intelligence Review Report" in captured.out
        assert "no new items" in captured.out

    def test_high_score_entries_auto_queued(self, tmp_path, capsys):
        topic = _make_topic("high-score", "high score topic", "tech_trend", "high")
        _write_topics([topic], tmp_path)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entries = [
            {
                "topic": "high score",
                "topic_id": "high-score",
                "summary": "Critical finding about high score topic",
                "relevance_score": 9,
                "source_url": "https://example.com/1",
            },
        ]
        _write_collected(f"{today}.json", entries, tmp_path)
        im.cmd_review(self._make_review_args())
        captured = capsys.readouterr()
        assert "items added to action_queue.json" in captured.out

        queue_path = tmp_path / "intelligence" / "action_queue.json"
        queue = json.loads(queue_path.read_text())
        assert len(queue["pending"]) >= 1

    def test_no_duplicate_queue_items(self, tmp_path, capsys):
        topic = _make_topic("dup-check", "duplicate check", "general", "medium")
        _write_topics([topic], tmp_path)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entries = [
            {
                "topic": "dup check",
                "topic_id": "dup-check",
                "summary": "Same finding repeated",
                "relevance_score": 9,
                "source_url": "https://example.com/dup",
            },
        ]
        _write_collected(f"{today}.json", entries, tmp_path)

        # Pre-populate action queue with the same entry
        queue = im._default_action_queue()
        queue["pending"].append({
            "id": "existing",
            "source": "review_auto",
            "finding": {
                "source_url": "https://example.com/dup",
                "summary": "Same finding repeated",
            },
            "action_type": "investigate",
            "priority": "medium",
            "queued_at": im.now_iso(),
            "status": "pending",
            "notes": "",
        })
        _write_action_queue(queue, tmp_path)

        im.cmd_review(self._make_review_args())
        captured = capsys.readouterr()
        assert "no new items" in captured.out

    def test_identifies_low_yield_topics(self, tmp_path, capsys):
        topic = _make_topic("low-yield", "low yield query", "general", "medium")
        _write_topics([topic], tmp_path)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # 1 entry over 7 days with low score = low yield
        entries = [
            {
                "topic": "low yield",
                "topic_id": "low-yield",
                "summary": "Barely relevant",
                "relevance_score": 3,
            },
        ]
        _write_collected(f"{today}.json", entries, tmp_path)

        im.cmd_review(self._make_review_args(days=7))
        captured = capsys.readouterr()
        assert "low yield" in captured.out.lower() or "low_yield" in captured.out

    def test_identifies_no_data_topics(self, tmp_path, capsys):
        topic = _make_topic("empty-topic", "nothing here", "general", "medium")
        _write_topics([topic], tmp_path)
        im.cmd_review(self._make_review_args())
        captured = capsys.readouterr()
        assert "no data" in captured.out.lower() or "no_data" in captured.out

    def test_detects_duplicates(self, tmp_path, capsys):
        topic = _make_topic("dup-detect", "duplicate detection", "general")
        _write_topics([topic], tmp_path)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Same summary prefix repeated
        entries = [
            {"topic": "dup detect", "topic_id": "dup-detect",
             "summary": "Exactly the same summary prefix here which is long enough to match"},
            {"topic": "dup detect", "topic_id": "dup-detect",
             "summary": "Exactly the same summary prefix here which is long enough to match"},
            {"topic": "dup detect", "topic_id": "dup-detect",
             "summary": "Exactly the same summary prefix here which is long enough to match"},
        ]
        _write_collected(f"{today}.json", entries, tmp_path)

        im.cmd_review(self._make_review_args())
        captured = capsys.readouterr()
        assert "duplicate" in captured.out.lower() or "%" in captured.out

    def test_security_category_gets_security_review(self, tmp_path, capsys):
        topic = _make_topic("sec-topic", "security vuln check", "security", "critical")
        _write_topics([topic], tmp_path)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entries = [
            {
                "topic": "sec topic",
                "topic_id": "sec-topic",
                "summary": "Critical vulnerability found in dependency",
                "relevance_score": 9,
                "source_url": "https://example.com/cve",
            },
        ]
        _write_collected(f"{today}.json", entries, tmp_path)

        im.cmd_review(self._make_review_args())
        queue_path = tmp_path / "intelligence" / "action_queue.json"
        queue = json.loads(queue_path.read_text())
        sec_items = [i for i in queue["pending"] if i["action_type"] == "security_review"]
        assert len(sec_items) >= 1

    def test_brand_category_gets_respond(self, tmp_path, capsys):
        topic = _make_topic("brand-topic", "brand mentions", "brand", "high")
        _write_topics([topic], tmp_path)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entries = [
            {
                "topic": "brand topic",
                "topic_id": "brand-topic",
                "summary": "Someone mentioned the brand in a positive light",
                "relevance_score": 9,
                "source_url": "https://example.com/brand",
            },
        ]
        _write_collected(f"{today}.json", entries, tmp_path)

        im.cmd_review(self._make_review_args())
        queue_path = tmp_path / "intelligence" / "action_queue.json"
        queue = json.loads(queue_path.read_text())
        respond_items = [i for i in queue["pending"] if i["action_type"] == "respond"]
        assert len(respond_items) >= 1


# ── Queue Management Tests (~5) ───────────────────────────────────


class TestQueue:

    def test_list_empty_queue(self, tmp_path, capsys):
        args = argparse.Namespace(queue_action="list")
        im.cmd_queue(args)
        captured = capsys.readouterr()
        assert "No pending actions." in captured.out

    def test_list_with_pending_items(self, tmp_path, capsys):
        queue = im._default_action_queue()
        queue["pending"].append({
            "id": "abc123",
            "priority": "high",
            "action_type": "investigate",
            "finding": {"summary": "Important finding", "relevance_score": 8},
            "queued_at": im.now_iso(),
            "status": "pending",
            "notes": "",
        })
        _write_action_queue(queue, tmp_path)

        args = argparse.Namespace(queue_action="list")
        im.cmd_queue(args)
        captured = capsys.readouterr()
        assert "abc123" in captured.out
        assert "1 pending action(s)" in captured.out

    def test_complete_moves_to_completed(self, tmp_path, capsys):
        queue = im._default_action_queue()
        queue["pending"].append({
            "id": "move-me",
            "priority": "medium",
            "action_type": "investigate",
            "finding": {"summary": "Test"},
            "queued_at": im.now_iso(),
            "status": "pending",
            "notes": "",
        })
        _write_action_queue(queue, tmp_path)

        args = argparse.Namespace(queue_action="complete", id="move-me")
        im.cmd_queue(args)
        captured = capsys.readouterr()
        assert "Moved move-me to completed" in captured.out

        saved = json.loads((tmp_path / "intelligence" / "action_queue.json").read_text())
        assert len(saved["pending"]) == 0
        assert len(saved["completed"]) == 1
        assert saved["completed"][0]["status"] == "completed"

    def test_dismiss_moves_to_dismissed(self, tmp_path, capsys):
        queue = im._default_action_queue()
        queue["pending"].append({
            "id": "dismiss-me",
            "priority": "low",
            "action_type": "investigate",
            "finding": {"summary": "Not important"},
            "queued_at": im.now_iso(),
            "status": "pending",
            "notes": "",
        })
        _write_action_queue(queue, tmp_path)

        args = argparse.Namespace(queue_action="dismiss", id="dismiss-me")
        im.cmd_queue(args)
        captured = capsys.readouterr()
        assert "Moved dismiss-me to dismissed" in captured.out

        saved = json.loads((tmp_path / "intelligence" / "action_queue.json").read_text())
        assert len(saved["pending"]) == 0
        assert len(saved["dismissed"]) == 1

    def test_clear_completed_removes_old_items(self, tmp_path, capsys):
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        queue = im._default_action_queue()
        queue["completed"] = [
            {"id": "old-item", "completed_at": old_date, "status": "completed",
             "finding": {}},
            {"id": "recent-item", "completed_at": recent_date, "status": "completed",
             "finding": {}},
        ]
        _write_action_queue(queue, tmp_path)

        args = argparse.Namespace(queue_action="clear-completed")
        im.cmd_queue(args)
        captured = capsys.readouterr()
        assert "Cleared 1 completed items" in captured.out
        assert "1 remaining" in captured.out

        saved = json.loads((tmp_path / "intelligence" / "action_queue.json").read_text())
        assert len(saved["completed"]) == 1
        assert saved["completed"][0]["id"] == "recent-item"


# ── Cleanup Tests (~4) ────────────────────────────────────────────


class TestCleanup:

    def _make_cleanup_args(self) -> argparse.Namespace:
        return argparse.Namespace()

    def test_archives_old_files(self, tmp_path, capsys):
        _write_routing(
            routes={"default": {"retain_days": 10}},
            cleanup_policy={"delete_archive_after_days": 365},
            tmp_path=tmp_path,
        )
        # Create a collected file older than retain_days
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        _write_collected(f"{old_date}.json", [{"data": "old"}], tmp_path)

        im.cmd_cleanup(self._make_cleanup_args())
        captured = capsys.readouterr()
        assert "1 archived" in captured.out

        archive_dir = tmp_path / "intelligence" / "archive"
        assert (archive_dir / f"{old_date}.json").exists()
        assert not (tmp_path / "intelligence" / "collected" / f"{old_date}.json").exists()

    def test_deletes_old_archived_files(self, tmp_path, capsys):
        _write_routing(
            routes={"default": {"retain_days": 10}},
            cleanup_policy={"delete_archive_after_days": 30},
            tmp_path=tmp_path,
        )
        # Create an archived file older than delete_archive_after_days
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        archive_dir = tmp_path / "intelligence" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / f"{old_date}.json"
        archive_file.write_text(json.dumps([{"data": "archived"}]))

        im.cmd_cleanup(self._make_cleanup_args())
        captured = capsys.readouterr()
        assert "1 deleted" in captured.out
        assert not archive_file.exists()

    def test_skips_recent_files(self, tmp_path, capsys):
        _write_routing(
            routes={"default": {"retain_days": 30}},
            cleanup_policy={"delete_archive_after_days": 180},
            tmp_path=tmp_path,
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _write_collected(f"{today}.json", [{"data": "fresh"}], tmp_path)

        im.cmd_cleanup(self._make_cleanup_args())
        captured = capsys.readouterr()
        assert "0 archived" in captured.out
        assert "0 deleted" in captured.out
        # File still exists
        assert (tmp_path / "intelligence" / "collected" / f"{today}.json").exists()

    def test_no_files_does_nothing(self, tmp_path, capsys):
        _write_routing(
            routes={"default": {"retain_days": 30}},
            cleanup_policy={},
            tmp_path=tmp_path,
        )
        im.cmd_cleanup(self._make_cleanup_args())
        captured = capsys.readouterr()
        assert "0 archived" in captured.out
        assert "0 deleted" in captured.out


# ── Summary Tests (~3) ────────────────────────────────────────────


class TestSummary:

    def test_no_data(self, tmp_path, capsys):
        args = argparse.Namespace(period="weekly")
        im.cmd_summary(args)
        captured = capsys.readouterr()
        assert "No collected data" in captured.out

    def test_generates_correct_json(self, tmp_path, capsys):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entries = [
            {"topic": "alpha", "category": "tech", "summary": "Alpha finding",
             "relevance_score": 9},
            {"topic": "alpha", "category": "tech", "summary": "Another alpha",
             "relevance_score": 7},
            {"topic": "beta", "category": "security", "summary": "Beta finding",
             "relevance_score": 5},
        ]
        _write_collected(f"{today}.json", entries, tmp_path)

        args = argparse.Namespace(period="weekly")
        im.cmd_summary(args)
        captured = capsys.readouterr()
        assert "3 entries" in captured.out

        # Check generated summary file
        summaries_dir = tmp_path / "intelligence" / "summaries"
        summary_files = list(summaries_dir.glob("summary_weekly_*.json"))
        assert len(summary_files) == 1
        summary = json.loads(summary_files[0].read_text())
        assert summary["total_entries"] == 3
        assert len(summary["top_by_score"]) <= 10
        assert summary["top_by_score"][0]["score"] == 9
        assert "tech" in summary["count_by_category"]
        assert "security" in summary["count_by_category"]

    def test_respects_period_filter(self, tmp_path, capsys):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        old_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        _write_collected(f"{today}.json",
                         [{"topic": "today", "summary": "Today entry"}], tmp_path)
        _write_collected(f"{old_date}.json",
                         [{"topic": "old", "summary": "Old entry"}], tmp_path)

        # daily should only include today
        args = argparse.Namespace(period="daily")
        im.cmd_summary(args)
        captured = capsys.readouterr()
        assert "1 entries" in captured.out or "1 entry" in captured.out

        # weekly should include both
        args = argparse.Namespace(period="weekly")
        im.cmd_summary(args)
        captured2 = capsys.readouterr()
        assert "2 entries" in captured2.out


# ── Status Tests (~2) ─────────────────────────────────────────────


class TestStatus:

    def test_empty_data(self, tmp_path, capsys):
        _write_topics([], tmp_path)
        args = argparse.Namespace()
        im.cmd_status(args)
        captured = capsys.readouterr()
        assert "Intel Manager Status" in captured.out
        assert "0 total" in captured.out
        assert "0 files" in captured.out
        assert "(never)" in captured.out

    def test_with_collected_data(self, tmp_path, capsys):
        topics = [
            _make_topic("active", "active query", enabled=True),
            _make_topic("inactive", "inactive query", enabled=False),
        ]
        _write_topics(topics, tmp_path)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _write_collected(f"{today}.json", [
            {"topic": "active", "summary": "Entry 1"},
            {"topic": "active", "summary": "Entry 2"},
        ], tmp_path)

        args = argparse.Namespace()
        im.cmd_status(args)
        captured = capsys.readouterr()
        assert "2 total (1 enabled, 1 disabled)" in captured.out
        assert "1 files" in captured.out
        assert "2 entries" in captured.out


# ── Migrate Tests (~3) ────────────────────────────────────────────


class TestMigrate:

    def _make_migrate_args(self) -> argparse.Namespace:
        return argparse.Namespace()

    def test_moves_and_renames_files(self, tmp_path, capsys):
        x_monitor = tmp_path / "x_monitor"
        x_monitor.mkdir()
        src_file = x_monitor / "x_monitor_2026-05-15.json"
        src_file.write_text(json.dumps([{"data": "migrated"}]))

        im.cmd_migrate(self._make_migrate_args())
        captured = capsys.readouterr()
        assert "1 migrated" in captured.out

        dest = tmp_path / "intelligence" / "collected" / "2026-05-15.json"
        assert dest.exists()
        assert not src_file.exists()

    def test_merges_into_existing(self, tmp_path, capsys):
        x_monitor = tmp_path / "x_monitor"
        x_monitor.mkdir()
        src_file = x_monitor / "x_monitor_2026-05-15.json"
        src_file.write_text(json.dumps([{"data": "incoming"}]))

        _write_collected("2026-05-15.json", [{"data": "existing"}], tmp_path)

        im.cmd_migrate(self._make_migrate_args())
        captured = capsys.readouterr()
        assert "1 migrated" in captured.out

        dest = tmp_path / "intelligence" / "collected" / "2026-05-15.json"
        merged = json.loads(dest.read_text())
        assert len(merged) == 2
        assert {"data": "existing"} in merged
        assert {"data": "incoming"} in merged

    def test_no_source_directory(self, tmp_path, capsys):
        # X_MONITOR_DIR does not exist
        im.cmd_migrate(self._make_migrate_args())
        captured = capsys.readouterr()
        assert "Source directory not found" in captured.out
