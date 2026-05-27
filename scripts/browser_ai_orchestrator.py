"""Browser AI Orchestrator — 外部AIサービスへのタスクキュー管理.

GPT/Grok/Gemini/Ollama へのタスクをJSONLベースのキューで管理する。
実際のCDP操作はこのスクリプトでは行わない（Sonnet Agent + chrome-devtools MCPが担当）。
このスクリプトはキューの追加・取得・完了管理の責務に限定する。

制約 (CLAUDE.md):
  - CDP並行操作禁止: 1サービス=1エージェントで直列実行
  - Grok操作時はChatGPTタブを閉じる
  - Gemini 2垢並行: ページ2(tsuna.konomiya) + ページ9(tomotomlo777)

使い方:
    # タスク追加
    python3 scripts/browser_ai_orchestrator.py enqueue --target gpt --task "Corp構造を分析して"
    python3 scripts/browser_ai_orchestrator.py enqueue --target grok --task "最新のAIニュース" -p 1

    # 一覧
    python3 scripts/browser_ai_orchestrator.py list
    python3 scripts/browser_ai_orchestrator.py list --target gpt

    # 次のタスクを取得 (ワーカー用)
    python3 scripts/browser_ai_orchestrator.py dequeue --target gpt

    # 完了
    python3 scripts/browser_ai_orchestrator.py complete --id <uuid> --result "分析結果..."

    # 結果取得
    python3 scripts/browser_ai_orchestrator.py result --id <uuid>

    # 統計
    python3 scripts/browser_ai_orchestrator.py stats
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

VALID_TARGETS = ("gpt", "grok", "gemini", "ollama")
DEFAULT_PRIORITY = 5

QUEUE_PATH = Path.home() / ".claude" / "external_ai_queue.jsonl"
RESULTS_PATH = Path.home() / ".claude" / "external_ai_results.jsonl"


class BrowserAIOrchestrator:
    """外部AIサービスへのタスクキュー管理."""

    def __init__(
        self,
        queue_path: Path | None = None,
        results_path: Path | None = None,
    ):
        self.queue_path = queue_path or QUEUE_PATH
        self.results_path = results_path or RESULTS_PATH
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.queue_path.with_suffix(".lock")

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def enqueue(
        self,
        target: str,
        task: str,
        context: str = "",
        priority: int = DEFAULT_PRIORITY,
    ) -> str:
        """タスクをキューに追加し、UUIDを返す.

        Args:
            target: gpt | grok | gemini | ollama
            task: タスクの説明
            context: 追加コンテキスト (任意)
            priority: 1(最高) - 10(最低), デフォルト5

        Returns:
            生成されたタスクID (UUID)
        """
        if target not in VALID_TARGETS:
            raise ValueError(f"無効なtarget: {target} (有効: {', '.join(VALID_TARGETS)})")
        if not task.strip():
            raise ValueError("taskは空にできません")

        task_id = str(uuid.uuid4())
        entry = {
            "id": task_id,
            "target": target,
            "task": task,
            "priority": max(1, min(10, priority)),
            "status": "pending",
            "context": context,
            "created_at": _now_iso(),
        }
        with _file_lock(self._lock_path):
            _append_jsonl(self.queue_path, entry)
        return task_id

    def dequeue(self, target: str | None = None) -> dict | None:
        """次のpendingタスクを取得し、statusをrunningに変更.

        priority昇順(1が最優先)、同一priorityではcreated_at昇順。

        Args:
            target: 特定ターゲットに限定 (Noneで全て)

        Returns:
            タスクdict、なければNone
        """
        with _file_lock(self._lock_path):
            entries = _read_jsonl(self.queue_path)
            pending = [
                e for e in entries
                if e.get("status") == "pending"
                and (target is None or e.get("target") == target)
            ]
            if not pending:
                return None

            # priority昇順 → created_at昇順
            pending.sort(key=lambda e: (e.get("priority", DEFAULT_PRIORITY), e.get("created_at", "")))
            chosen = pending[0]

            # statusをrunningに更新
            for e in entries:
                if e["id"] == chosen["id"]:
                    e["status"] = "running"
                    e["started_at"] = _now_iso()
                    break

            _write_jsonl(self.queue_path, entries)
        return chosen

    def complete(
        self,
        task_id: str,
        result: str,
        status: str = "completed",
    ) -> bool:
        """タスクを完了マークし、結果をresultsファイルに保存.

        キュー更新とresults書き込みを同一ロック内で実行し、
        片方だけ成功する中間状態を防止する (P2修正)。

        Args:
            task_id: タスクのUUID
            result: 実行結果テキスト
            status: completed | failed

        Returns:
            更新できたらTrue
        """
        if status not in ("completed", "failed"):
            raise ValueError(f"無効なstatus: {status} (有効: completed, failed)")

        with _file_lock(self._lock_path):
            entries = _read_jsonl(self.queue_path)
            found = False
            for e in entries:
                if e["id"] == task_id:
                    e["status"] = status
                    e["completed_at"] = _now_iso()
                    found = True
                    break

            if not found:
                return False

            # 結果を別ファイルに保存 (キュー更新と同一ロック内)
            result_entry = {
                "id": task_id,
                "status": status,
                "result": result,
                "completed_at": _now_iso(),
            }
            _append_jsonl(self.results_path, result_entry)
            _write_jsonl(self.queue_path, entries)
        return True

    def cancel(self, task_id: str) -> bool:
        """タスクをキャンセル.

        pending および running 状態のタスクをキャンセルできる。
        外部ワーカーへの通知は行わない — ワーカーは dequeue 時に
        status を確認し、cancelled であれば処理をスキップすること。

        Returns:
            キャンセルできたらTrue
        """
        with _file_lock(self._lock_path):
            entries = _read_jsonl(self.queue_path)
            for e in entries:
                if e["id"] == task_id and e["status"] in ("pending", "running"):
                    e["status"] = "cancelled"
                    now = _now_iso()
                    e["completed_at"] = now
                    e["cancelled_at"] = now
                    _write_jsonl(self.queue_path, entries)
                    return True
        return False

    def list_pending(self, target: str | None = None) -> list[dict]:
        """未処理 (pending + running) タスクを一覧.

        Args:
            target: 特定ターゲットに限定 (Noneで全て)
        """
        entries = _read_jsonl(self.queue_path)
        result = [
            e for e in entries
            if e.get("status") in ("pending", "running")
            and (target is None or e.get("target") == target)
        ]
        result.sort(key=lambda e: (e.get("priority", DEFAULT_PRIORITY), e.get("created_at", "")))
        return result

    def get_result(self, task_id: str) -> dict | None:
        """完了結果を取得."""
        for entry in _read_jsonl(self.results_path):
            if entry.get("id") == task_id:
                return entry
        return None

    def stats(self) -> dict:
        """キューの統計情報."""
        entries = _read_jsonl(self.queue_path)
        by_status: dict[str, int] = {}
        by_target: dict[str, dict[str, int]] = {}

        for e in entries:
            status = e.get("status", "unknown")
            target = e.get("target", "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            if target not in by_target:
                by_target[target] = {}
            by_target[target][status] = by_target[target].get(status, 0) + 1

        return {
            "total": len(entries),
            "by_status": by_status,
            "by_target": by_target,
        }

    def cleanup(self, keep_last: int = 100) -> int:
        """完了・キャンセル済みタスクを古い順に削除し、最新keep_last件だけ残す.

        Returns:
            削除した件数
        """
        with _file_lock(self._lock_path):
            entries = _read_jsonl(self.queue_path)
            active = [e for e in entries if e.get("status") in ("pending", "running")]
            done = [e for e in entries if e.get("status") not in ("pending", "running")]

            if len(done) <= keep_last:
                return 0

            # 古い順にソートして末尾keep_last件を残す
            done.sort(key=lambda e: e.get("completed_at", e.get("created_at", "")))
            removed = len(done) - keep_last
            kept_done = done[removed:]

            _write_jsonl(self.queue_path, active + kept_done)
        return removed


# ------------------------------------------------------------------
# File I/O helpers
# ------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _file_lock(lock_path: Path):
    """fcntl.flock ベースの排他ロック (macOS).

    同一ファイルシステム上の複数プロセスが同じ lock_path を
    指定することで、read-modify-write の競合を防止する。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def _append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    """全件上書き (アトミック: tmp書き込み → rename).

    同一ファイルシステム上の rename は POSIX でアトミック。
    書き込み中にクラッシュしても元ファイルは無傷。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(path)


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def _format_task(t: dict) -> str:
    """タスクを1行表示用に整形."""
    pri = t.get("priority", DEFAULT_PRIORITY)
    status = t.get("status", "?")
    target = t.get("target", "?")
    task_id = t.get("id", "?")[:8]
    task_text = t.get("task", "")
    if len(task_text) > 60:
        task_text = task_text[:57] + "..."
    return f"[P{pri}] {status:<10} {target:<7} {task_id}  {task_text}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Browser AI Orchestrator — 外部AIタスクキュー管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # enqueue
    p_enq = sub.add_parser("enqueue", help="タスクをキューに追加")
    p_enq.add_argument("--target", "-t", required=True, choices=VALID_TARGETS,
                        help="ターゲットAI (gpt/grok/gemini/ollama)")
    p_enq.add_argument("--task", required=True, help="タスクの説明")
    p_enq.add_argument("--context", "-c", default="", help="追加コンテキスト")
    p_enq.add_argument("--priority", "-p", type=int, default=DEFAULT_PRIORITY,
                        help="優先度 1(最高)-10(最低), デフォルト5")

    # dequeue
    p_deq = sub.add_parser("dequeue", help="次のpendingタスクを取得")
    p_deq.add_argument("--target", "-t", choices=VALID_TARGETS,
                        help="ターゲットAI限定 (省略で全て)")

    # complete
    p_comp = sub.add_parser("complete", help="タスクを完了マーク")
    p_comp.add_argument("--id", required=True, help="タスクID")
    p_comp.add_argument("--result", "-r", required=True, help="結果テキスト")
    p_comp.add_argument("--status", "-s", default="completed",
                         choices=["completed", "failed"], help="完了ステータス")

    # cancel
    p_cancel = sub.add_parser("cancel", help="タスクをキャンセル")
    p_cancel.add_argument("--id", required=True, help="タスクID")

    # list
    p_list = sub.add_parser("list", help="未処理タスク一覧")
    p_list.add_argument("--target", "-t", choices=VALID_TARGETS,
                         help="ターゲットAI限定")
    p_list.add_argument("--all", "-a", action="store_true",
                         help="完了・キャンセル済みも表示")

    # result
    p_res = sub.add_parser("result", help="完了結果を取得")
    p_res.add_argument("--id", required=True, help="タスクID")

    # stats
    sub.add_parser("stats", help="キュー統計")

    # cleanup
    p_clean = sub.add_parser("cleanup", help="古い完了タスクを削除")
    p_clean.add_argument("--keep", type=int, default=100,
                          help="残す完了タスク数 (デフォルト100)")

    args = parser.parse_args()
    orch = BrowserAIOrchestrator()

    if args.command == "enqueue":
        task_id = orch.enqueue(
            target=args.target,
            task=args.task,
            context=args.context,
            priority=args.priority,
        )
        print(f"enqueued: {task_id}")

    elif args.command == "dequeue":
        task = orch.dequeue(target=args.target)
        if task:
            print(json.dumps(task, ensure_ascii=False, indent=2))
        else:
            target_msg = f" ({args.target})" if args.target else ""
            print(f"pendingタスクなし{target_msg}")
            sys.exit(1)

    elif args.command == "complete":
        ok = orch.complete(
            task_id=args.id,
            result=args.result,
            status=args.status,
        )
        if ok:
            print(f"{args.status}: {args.id}")
        else:
            print(f"タスクが見つかりません: {args.id}")
            sys.exit(1)

    elif args.command == "cancel":
        ok = orch.cancel(task_id=args.id)
        if ok:
            print(f"cancelled: {args.id}")
        else:
            print(f"キャンセルできません (存在しない or 既に完了): {args.id}")
            sys.exit(1)

    elif args.command == "list":
        if args.all:
            entries = _read_jsonl(orch.queue_path)
            if args.target:
                entries = [e for e in entries if e.get("target") == args.target]
            entries.sort(key=lambda e: (e.get("priority", DEFAULT_PRIORITY), e.get("created_at", "")))
        else:
            entries = orch.list_pending(target=args.target)

        if not entries:
            print("タスクなし")
        else:
            for t in entries:
                print(_format_task(t))

    elif args.command == "result":
        r = orch.get_result(task_id=args.id)
        if r:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print(f"結果が見つかりません: {args.id}")
            sys.exit(1)

    elif args.command == "stats":
        s = orch.stats()
        print(f"合計: {s['total']}")
        print()
        if s["by_status"]:
            print("ステータス別:")
            for status, count in sorted(s["by_status"].items()):
                print(f"  {status}: {count}")
        print()
        if s["by_target"]:
            print("ターゲット別:")
            for target, statuses in sorted(s["by_target"].items()):
                parts = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
                print(f"  {target}: {parts}")

    elif args.command == "cleanup":
        removed = orch.cleanup(keep_last=args.keep)
        print(f"削除: {removed}件")


if __name__ == "__main__":
    main()
