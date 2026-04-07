"""アシスタントキュー CLI — Opusセッションや手動からタスクを投入.

使い方:
    python scripts/assistant_cli.py add "スケジュール確認" --target gemma4
    python scripts/assistant_cli.py add "コードレビュー src/main.py" --target sonnet --priority high
    python scripts/assistant_cli.py add "アーキテクチャ設計" --target opus
    python scripts/assistant_cli.py list
    python scripts/assistant_cli.py stats
    python scripts/assistant_cli.py result <task_id>
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from assistant_queue import TaskQueue


def main():
    parser = argparse.ArgumentParser(description="アシスタントキュー CLI")
    sub = parser.add_subparsers(dest="command")

    # add
    add_p = sub.add_parser("add", help="タスク追加")
    add_p.add_argument("description", help="タスクの説明")
    add_p.add_argument("--target", choices=["gemma4", "sonnet", "opus"], default="gemma4")
    add_p.add_argument("--priority", choices=["low", "medium", "high", "critical"], default="medium")
    add_p.add_argument("--category", default="general")
    add_p.add_argument("--payload", type=str, default=None, help="JSON形式の追加情報")

    # list
    sub.add_parser("list", help="待機中タスク一覧")

    # stats
    sub.add_parser("stats", help="統計")

    # result
    res_p = sub.add_parser("result", help="タスク結果表示")
    res_p.add_argument("task_id", help="タスクID")

    # complete
    comp_p = sub.add_parser("complete", help="タスクを手動完了")
    comp_p.add_argument("task_id")
    comp_p.add_argument("--result", default="手動完了")

    args = parser.parse_args()
    q = TaskQueue()

    if args.command == "add":
        payload = None
        if args.payload:
            try:
                payload = json.loads(args.payload)
            except json.JSONDecodeError:
                payload = {"raw": args.payload}
        task = q.add(
            description=args.description,
            target=args.target,
            priority=args.priority,
            category=args.category,
            payload=payload,
            source="cli",
        )
        print(f"追加: {task.id} [{task.target}/{task.priority}] {task.description}")

    elif args.command == "list":
        tasks = q.get_all()
        if not tasks:
            print("キューは空です。")
            return
        for t in tasks:
            status_mark = {"pending": "⏳", "in_progress": "🔄", "completed": "✅", "failed": "❌", "escalated": "⬆️"}.get(t.status, "?")
            print(f"  {status_mark} {t.id} [{t.target}/{t.priority}] {t.description[:60]}")
            if t.result:
                print(f"      → {t.result[:80]}")

    elif args.command == "stats":
        s = q.stats()
        print(f"計{s['total']}件: 待機{s['pending']} / 処理中{s['in_progress']} / 完了{s['completed']} / 失敗{s['failed']}")
        print(f"  gemma4:{s['by_target']['gemma4']} / sonnet:{s['by_target']['sonnet']} / opus:{s['by_target']['opus']}")

    elif args.command == "result":
        for t in q.get_all():
            if t.id == args.task_id:
                print(f"ID: {t.id}")
                print(f"説明: {t.description}")
                print(f"ステータス: {t.status}")
                print(f"ターゲット: {t.target}")
                print(f"結果: {t.result or '(なし)'}")
                return
        print(f"タスク {args.task_id} が見つかりません。")

    elif args.command == "complete":
        if q.complete(args.task_id, args.result):
            print(f"完了: {args.task_id}")
        else:
            print(f"タスク {args.task_id} が見つかりません。")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
