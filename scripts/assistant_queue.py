"""タスクキュー管理 — 3層アシスタント間のタスク受け渡し.

タスクの追加・取得・完了を管理するシンプルなファイルベースキュー。
gemma4 / Sonnet / Opus の3層でタスクを振り分ける。

使い方:
    from scripts.assistant_queue import TaskQueue, Task

    q = TaskQueue()
    q.add("スケジュール確認", priority="low", target="gemma4")
    q.add("コードレビュー", priority="medium", target="sonnet", payload={"file": "src/main.py"})
    q.add("アーキテクチャ設計", priority="high", target="opus")

    tasks = q.get_pending(target="gemma4")
    q.complete(task_id, result="処理結果")
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUEUE_DIR = Path.home() / ".helix-agent" / "assistant"
QUEUE_FILE = QUEUE_DIR / "queue.json"
HISTORY_FILE = QUEUE_DIR / "history.jsonl"


class Task:
    """タスク1件を表すデータクラス."""

    def __init__(
        self,
        description: str,
        priority: str = "medium",
        target: str = "gemma4",
        category: str = "general",
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        status: str = "pending",
        created_at: str | None = None,
        result: str | None = None,
        completed_at: str | None = None,
        source: str = "manual",
    ):
        self.id = task_id or str(uuid.uuid4())[:8]
        self.description = description
        self.priority = priority          # low / medium / high / critical
        self.target = target              # gemma4 / sonnet / opus
        self.category = category          # general / schedule / x_monitor / hw / review / etc.
        self.payload = payload or {}
        self.status = status              # pending / in_progress / completed / failed / escalated
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()
        self.result = result
        self.completed_at = completed_at
        self.source = source              # manual / daemon / opus / watchdog / x_monitor

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "priority": self.priority,
            "target": self.target,
            "category": self.category,
            "payload": self.payload,
            "status": self.status,
            "created_at": self.created_at,
            "result": self.result,
            "completed_at": self.completed_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        return cls(
            description=d["description"],
            priority=d.get("priority", "medium"),
            target=d.get("target", "gemma4"),
            category=d.get("category", "general"),
            payload=d.get("payload", {}),
            task_id=d.get("id"),
            status=d.get("status", "pending"),
            created_at=d.get("created_at"),
            result=d.get("result"),
            completed_at=d.get("completed_at"),
            source=d.get("source", "manual"),
        )


class TaskQueue:
    """ファイルベースのタスクキュー."""

    def __init__(self, queue_dir: Path = QUEUE_DIR):
        self.queue_dir = queue_dir
        self.queue_file = queue_dir / "queue.json"
        self.history_file = queue_dir / "history.jsonl"
        self.queue_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict]:
        if self.queue_file.exists():
            try:
                return json.loads(self.queue_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save(self, tasks: list[dict]) -> None:
        self.queue_file.write_text(
            json.dumps(tasks, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(
        self,
        description: str,
        priority: str = "medium",
        target: str = "gemma4",
        category: str = "general",
        payload: dict[str, Any] | None = None,
        source: str = "manual",
    ) -> Task:
        """タスクをキューに追加."""
        task = Task(
            description=description,
            priority=priority,
            target=target,
            category=category,
            payload=payload,
            source=source,
        )
        tasks = self._load()
        tasks.append(task.to_dict())
        self._save(tasks)
        return task

    def get_pending(self, target: str | None = None) -> list[Task]:
        """未処理タスクを取得. target指定で絞り込み."""
        tasks = self._load()
        pending = [Task.from_dict(t) for t in tasks if t["status"] == "pending"]
        if target:
            pending = [t for t in pending if t.target == target]
        # priority順: critical > high > medium > low
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        pending.sort(key=lambda t: priority_order.get(t.priority, 2))
        return pending

    def get_all(self) -> list[Task]:
        """全タスクを取得."""
        return [Task.from_dict(t) for t in self._load()]

    def update_status(self, task_id: str, status: str, result: str | None = None) -> bool:
        """タスクのステータスを更新."""
        tasks = self._load()
        for t in tasks:
            if t["id"] == task_id:
                t["status"] = status
                if result:
                    t["result"] = result
                if status in ("completed", "failed"):
                    t["completed_at"] = datetime.now(timezone.utc).isoformat()
                self._save(tasks)
                return True
        return False

    def complete(self, task_id: str, result: str = "") -> bool:
        """タスクを完了にして履歴に移動."""
        tasks = self._load()
        completed_task = None
        remaining = []
        for t in tasks:
            if t["id"] == task_id:
                t["status"] = "completed"
                t["result"] = result
                t["completed_at"] = datetime.now(timezone.utc).isoformat()
                completed_task = t
            else:
                remaining.append(t)

        if completed_task:
            self._save(remaining)
            # 履歴に追記
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(completed_task, ensure_ascii=False) + "\n")
            return True
        return False

    def escalate(self, task_id: str, new_target: str, reason: str = "") -> bool:
        """タスクをより上位の層にエスカレート."""
        tasks = self._load()
        for t in tasks:
            if t["id"] == task_id:
                old_target = t["target"]
                t["target"] = new_target
                t["status"] = "pending"
                t.setdefault("payload", {})["escalated_from"] = old_target
                t["payload"]["escalation_reason"] = reason
                self._save(tasks)
                return True
        return False

    def cleanup(self, max_age_hours: int = 24) -> int:
        """古い完了/失敗タスクをキューから除去."""
        tasks = self._load()
        now = datetime.now(timezone.utc)
        kept = []
        removed = 0
        for t in tasks:
            if t["status"] in ("completed", "failed") and t.get("completed_at"):
                try:
                    completed = datetime.fromisoformat(t["completed_at"])
                    age_hours = (now - completed).total_seconds() / 3600
                    if age_hours > max_age_hours:
                        # 履歴に移動
                        with open(self.history_file, "a", encoding="utf-8") as f:
                            f.write(json.dumps(t, ensure_ascii=False) + "\n")
                        removed += 1
                        continue
                except (ValueError, TypeError):
                    pass
            kept.append(t)
        self._save(kept)
        return removed

    def stats(self) -> dict:
        """キューの統計情報."""
        tasks = self._load()
        stats = {
            "total": len(tasks),
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
            "by_target": {"gemma4": 0, "sonnet": 0, "opus": 0},
        }
        for t in tasks:
            status = t.get("status", "pending")
            if status in stats:
                stats[status] += 1
            target = t.get("target", "gemma4")
            if target in stats["by_target"]:
                stats["by_target"][target] += 1
        return stats


if __name__ == "__main__":
    import sys
    q = TaskQueue()

    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        s = q.stats()
        print(f"キュー: 計{s['total']}件 (待機{s['pending']} / 処理中{s['in_progress']} / 完了{s['completed']})")
        print(f"  gemma4: {s['by_target']['gemma4']} / sonnet: {s['by_target']['sonnet']} / opus: {s['by_target']['opus']}")
    elif len(sys.argv) > 1 and sys.argv[1] == "list":
        for t in q.get_all():
            print(f"  [{t.status}] {t.id} ({t.target}/{t.priority}) {t.description[:60]}")
    elif len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        removed = q.cleanup()
        print(f"{removed}件のタスクを履歴に移動しました。")
    else:
        print("使い方: assistant_queue.py [stats|list|cleanup]")
