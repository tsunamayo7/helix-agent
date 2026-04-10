"""X自動投稿・リプライ候補の選定スクリプト.

セッション開始時にClaudeから呼び出され、以下を実行:
1. x_monitor + x-feed-collector の蓄積データを統合分析
2. 高スコアのリプライ候補を選定
3. x_digest.md に圧縮して蓄積
4. 投稿/リプライ候補をJSON形式で出力

使い方:
    uv run python scripts/x_auto_poster.py              # 分析+候補出力
    uv run python scripts/x_auto_poster.py --digest      # x_digest.md 更新のみ
    uv run python scripts/x_auto_poster.py --json        # JSON形式で出力
    uv run python scripts/x_auto_poster.py --since 2     # 過去2時間分
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows cp932対策
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# パス設定
# ---------------------------------------------------------------------------

X_MONITOR_DIR = Path.home() / ".helix-agent" / "x_monitor"
DIGEST_PATH = Path.home() / ".claude" / "projects" / "C--Development" / "memory" / "content" / "x_digest.md"
X_FEED_COLLECTOR_DIR = Path("C:/Development/tools/x-feed-collector")

# ---------------------------------------------------------------------------
# データ読み込み
# ---------------------------------------------------------------------------


def load_x_monitor_entries(hours: int = 6) -> list[dict]:
    """x_monitorの蓄積データを読み込む."""
    if not X_MONITOR_DIR.exists():
        return []

    cutoff = datetime.now() - timedelta(hours=hours)
    entries = []

    for filepath in sorted(X_MONITOR_DIR.glob("x_monitor_*.json"), reverse=True):
        stem = filepath.stem
        try:
            date_part = stem.replace("x_monitor_", "")
            file_dt = datetime.strptime(date_part, "%Y%m%d_%H%M")
            if file_dt < cutoff:
                break
        except ValueError:
            continue

        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            if isinstance(data, list):
                entries.extend(data)
        except (json.JSONDecodeError, OSError):
            continue

    return entries


def load_feed_collector_recent(hours: int = 6) -> list[dict]:
    """x-feed-collectorのQdrant蓄積から最新データを取得（HTTP API経由）."""
    import urllib.request
    import urllib.error

    try:
        url = "http://localhost:8080/search"
        payload = json.dumps({
            "query": "Claude Code MCP AI agent latest",
            "limit": 20,
            "filters": {"source": "x-feed-collector"},
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("results", [])
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# 分析・選定
# ---------------------------------------------------------------------------


def deduplicate_entries(entries: list[dict]) -> list[dict]:
    """タイトルベースで重複除去."""
    seen = set()
    unique = []
    for entry in entries:
        title = entry.get("title", "").strip().lower()
        if title and title not in seen:
            seen.add(title)
            unique.append(entry)
    return unique


def select_reply_candidates(entries: list[dict], min_score: int = 8) -> list[dict]:
    """リプライ候補を選定."""
    candidates = []
    for entry in entries:
        score = entry.get("relevance_score", 0)
        is_candidate = entry.get("reply_candidate", False)
        if isinstance(score, (int, float)) and score >= min_score and is_candidate:
            candidates.append(entry)

    # スコア降順でソート
    candidates.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return candidates[:10]


def select_post_topics(entries: list[dict], min_score: int = 9) -> list[dict]:
    """独自投稿のトピック候補を選定（スコア9+）."""
    topics = []
    for entry in entries:
        score = entry.get("relevance_score", 0)
        if isinstance(score, (int, float)) and score >= min_score:
            # helix-agent/MCP/Claude Code関連を優先
            topic = entry.get("topic", "").lower()
            title = entry.get("title", "").lower()
            keywords = ["mcp", "claude code", "helix", "ollama", "gemma4", "local llm", "agent"]
            relevance = any(kw in topic or kw in title for kw in keywords)
            entry["_our_relevance"] = relevance
            topics.append(entry)

    # 自プロジェクト関連を優先、その後スコア順
    topics.sort(key=lambda x: (x.get("_our_relevance", False), x.get("relevance_score", 0)), reverse=True)
    return topics[:5]


# ---------------------------------------------------------------------------
# Digest更新
# ---------------------------------------------------------------------------


def update_digest(
    reply_candidates: list[dict],
    post_topics: list[dict],
    all_count: int,
) -> str:
    """x_digest.md を更新."""
    now = datetime.now()
    header = f"# X Digest — {now.strftime('%Y-%m-%d %H:%M')}\n\n"
    header += f"分析対象: {all_count}件\n\n"

    # リプライ候補
    sections = ["## リプライ候補\n\n"]
    for i, c in enumerate(reply_candidates, 1):
        sections.append(
            f"{i}. **[{c.get('source', '?')}]** {c.get('title', '?')} "
            f"(score={c.get('relevance_score', '?')})\n"
            f"   {c.get('summary', '')}\n"
            f"   → 提案: {c.get('suggested_reply', 'なし')}\n\n"
        )

    if not reply_candidates:
        sections.append("（なし）\n\n")

    # 投稿トピック
    sections.append("## 投稿候補トピック\n\n")
    for i, t in enumerate(post_topics, 1):
        rel = "🔥" if t.get("_our_relevance") else ""
        sections.append(
            f"{i}. {rel}**{t.get('title', '?')}** "
            f"(score={t.get('relevance_score', '?')}, {t.get('source', '?')})\n"
            f"   {t.get('summary', '')}\n\n"
        )

    if not post_topics:
        sections.append("（なし）\n\n")

    content = header + "".join(sections)

    # ファイル書き込み（既存は上書き）
    DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    DIGEST_PATH.write_text(content, encoding="utf-8")
    return content


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="X自動投稿候補選定")
    parser.add_argument("--since", type=int, default=6, help="過去N時間分を分析（デフォルト: 6）")
    parser.add_argument("--digest", action="store_true", help="x_digest.md更新のみ")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    parser.add_argument("--min-score", type=int, default=8, help="最小スコア")
    args = parser.parse_args()

    # データ収集
    monitor_entries = load_x_monitor_entries(hours=args.since)
    feed_entries = load_feed_collector_recent(hours=args.since)

    # 統合・重複除去
    all_entries = deduplicate_entries(monitor_entries + feed_entries)

    # 選定
    reply_candidates = select_reply_candidates(all_entries, min_score=args.min_score)
    post_topics = select_post_topics(all_entries, min_score=args.min_score + 1)

    # Digest更新
    digest_content = update_digest(reply_candidates, post_topics, len(all_entries))

    if args.digest:
        print(f"x_digest.md 更新完了 ({len(reply_candidates)}候補, {len(post_topics)}トピック)")
        return

    if args.json:
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_entries": len(all_entries),
            "reply_candidates": reply_candidates,
            "post_topics": post_topics,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(digest_content)

    print(f"\n--- 統計 ---")
    print(f"x_monitor: {len(monitor_entries)}件")
    print(f"x-feed-collector: {len(feed_entries)}件")
    print(f"重複除去後: {len(all_entries)}件")
    print(f"リプライ候補: {len(reply_candidates)}件")
    print(f"投稿候補: {len(post_topics)}件")


if __name__ == "__main__":
    main()
