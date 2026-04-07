"""蓄積されたX監視データのレポート表示スクリプト.

Claude Codeセッションから呼び出して、最新のリプライ候補一覧を確認する。

使い方:
    uv run python scripts/x_monitor_report.py
    uv run python scripts/x_monitor_report.py --days 3
    uv run python scripts/x_monitor_report.py --replies-only
    uv run python scripts/x_monitor_report.py --json
    uv run python scripts/x_monitor_report.py --topic "Claude Code"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".helix-agent" / "x_monitor"


def load_entries(
    data_dir: Path,
    days: int = 7,
    topic_filter: str | None = None,
    replies_only: bool = False,
) -> list[dict]:
    """指定期間のエントリを読み込む."""
    if not data_dir.exists():
        return []

    cutoff = datetime.now() - timedelta(days=days)
    entries = []

    for filepath in sorted(data_dir.glob("x_monitor_*.json")):
        # ファイル名から日時を推定
        stem = filepath.stem  # x_monitor_20260406_1430
        try:
            date_part = stem.replace("x_monitor_", "")
            file_dt = datetime.strptime(date_part, "%Y%m%d_%H%M")
            if file_dt < cutoff:
                continue
        except ValueError:
            continue

        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for entry in data:
                    entry["_file"] = filepath.name
                    entry["_file_date"] = file_dt.isoformat()
                    entries.append(entry)
        except (json.JSONDecodeError, OSError):
            continue

    # フィルタリング
    if replies_only:
        entries = [e for e in entries if e.get("reply_candidate", False)]

    if topic_filter:
        topic_lower = topic_filter.lower()
        entries = [
            e for e in entries
            if topic_lower in e.get("topic", "").lower()
            or topic_lower in e.get("title", "").lower()
            or topic_lower in e.get("summary", "").lower()
        ]

    # スコア降順でソート
    entries.sort(key=lambda e: e.get("relevance_score", 0), reverse=True)
    return entries


def format_report(entries: list[dict]) -> str:
    """人間が読みやすい形式のレポートを生成する."""
    if not entries:
        return "エントリなし。x_monitor.py を実行してデータを収集してください。"

    lines = []
    lines.append(f"=== X Monitor レポート ({len(entries)} 件) ===")
    lines.append("")

    # リプライ候補を先に表示
    reply_entries = [e for e in entries if e.get("reply_candidate", False)]
    other_entries = [e for e in entries if not e.get("reply_candidate", False)]

    if reply_entries:
        lines.append(f"--- リプライ候補 ({len(reply_entries)} 件) ---")
        lines.append("")
        for i, entry in enumerate(reply_entries, 1):
            lines.append(f"  [{i}] {entry.get('title', '(no title)')}")
            lines.append(f"      ソース: {entry.get('source', '?')}  |  スコア: {entry.get('relevance_score', '?')}/10")
            lines.append(f"      トピック: {entry.get('topic', '?')}")
            lines.append(f"      要約: {entry.get('summary', '(no summary)')}")
            suggested = entry.get("suggested_reply", "")
            if suggested:
                lines.append(f"      提案リプライ: {suggested}")
            lines.append(f"      収集日: {entry.get('collected_at', entry.get('_file_date', '?'))}")
            lines.append("")

    if other_entries:
        lines.append(f"--- その他の高スコアエントリ ({len(other_entries)} 件) ---")
        lines.append("")
        for i, entry in enumerate(other_entries, 1):
            lines.append(f"  [{i}] {entry.get('title', '(no title)')}")
            lines.append(f"      ソース: {entry.get('source', '?')}  |  スコア: {entry.get('relevance_score', '?')}/10")
            lines.append(f"      トピック: {entry.get('topic', '?')}")
            lines.append(f"      要約: {entry.get('summary', '(no summary)')}")
            lines.append("")

    # 統計
    lines.append("--- 統計 ---")
    sources = {}
    for e in entries:
        src = e.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1
    for src, count in sorted(sources.items(), key=lambda x: -x[1]):
        lines.append(f"  {src}: {count} 件")

    scores = [e.get("relevance_score", 0) for e in entries if isinstance(e.get("relevance_score"), (int, float))]
    if scores:
        lines.append(f"  平均スコア: {sum(scores) / len(scores):.1f}")
        lines.append(f"  最高スコア: {max(scores)}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="X監視データのレポート表示",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="何日分のデータを表示するか (デフォルト: 7)",
    )
    parser.add_argument(
        "--replies-only",
        action="store_true",
        help="リプライ候補のみ表示",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        help="トピックでフィルタリング",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="JSON形式で出力",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(DEFAULT_DATA_DIR),
        help=f"データディレクトリ (デフォルト: {DEFAULT_DATA_DIR})",
    )
    args = parser.parse_args()

    entries = load_entries(
        data_dir=Path(args.data_dir),
        days=args.days,
        topic_filter=args.topic,
        replies_only=args.replies_only,
    )

    if args.output_json:
        # 内部メタデータを除去
        clean = []
        for e in entries:
            ce = {k: v for k, v in e.items() if not k.startswith("_")}
            clean.append(ce)
        print(json.dumps(clean, ensure_ascii=False, indent=2))
    else:
        print(format_report(entries))


if __name__ == "__main__":
    main()
