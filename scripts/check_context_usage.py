"""Claude Code CLIのコンテキスト使用率を推定.

2方式で推定:
  方式A (高精度): セッションJSONLのトークン累計からコンテキストウィンドウ使用率を計算
  方式B (補助): スクリーンショット+Vision OCR（フォールバック）

Opus 4.7 (1M context) のコンテキストウィンドウサイズを基準に計算。
Claudeのコンテキスト管理はキャッシュ+圧縮があるため正確な値ではないが、
compact推奨の判断材料としては十分。

使い方:
    python scripts/check_context_usage.py          # パーセンテージを表示
    python scripts/check_context_usage.py --json    # JSON形式で出力
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Opus 4.7 (1M context) のコンテキストウィンドウ
CONTEXT_WINDOW = 1_000_000

# セッションJSONLのパス
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def find_current_session_jsonl() -> Path | None:
    """最新（現在の）セッションJSONLファイルを特定."""
    # 最も最近更新されたJSONLを探す
    candidates = []
    for jsonl in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        try:
            mtime = jsonl.stat().st_mtime
            candidates.append((mtime, jsonl))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def estimate_context_usage(jsonl_path: Path) -> dict:
    """セッションJSONLからコンテキスト使用率を推定.

    最後のassistantメッセージのusageからinput_tokens(=現在のコンテキストサイズ)を取得。
    cache_read_input_tokensを含むinput_tokensが実質的なコンテキスト消費量。
    """
    last_usage = None
    total_output = 0
    message_count = 0

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = entry.get("message", {})
                usage = msg.get("usage", {})
                if not usage:
                    continue

                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                cache_create = usage.get("cache_creation_input_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)

                if inp > 0 or out > 0:
                    last_usage = {
                        "input_tokens": inp,
                        "output_tokens": out,
                        "cache_creation": cache_create,
                        "cache_read": cache_read,
                        "total_context": inp + cache_create + cache_read,
                    }
                    total_output += out
                    message_count += 1
    except (OSError, PermissionError):
        return {"error": "ファイル読み取り失敗"}

    if not last_usage:
        return {"error": "usage情報なし"}

    # コンテキスト使用率の推定
    # input_tokens + cache_creation + cache_read = Claudeが実際に参照した全トークン数
    # これがコンテキストウィンドウの実質的な使用量
    effective_context = last_usage["input_tokens"] + last_usage["cache_creation"] + last_usage["cache_read"]
    percentage = min(100, round(effective_context / CONTEXT_WINDOW * 100, 1))

    # compaction発生の判定（急にinput_tokensが減った場合）
    return {
        "percentage": percentage,
        "effective_context_tokens": effective_context,
        "context_window": CONTEXT_WINDOW,
        "last_input_tokens": last_usage["input_tokens"],
        "last_cache_creation": last_usage["cache_creation"],
        "last_cache_read": last_usage["cache_read"],
        "total_output_tokens": total_output,
        "message_count": message_count,
        "session_file": str(jsonl_path.name),
        "compact_recommended": percentage >= 70,
        "compact_urgent": percentage >= 85,
    }


def main():
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": "jsonl_analysis",
    }

    jsonl = find_current_session_jsonl()
    if not jsonl:
        result["error"] = "セッションJSONLが見つかりません"
    else:
        usage = estimate_context_usage(jsonl)
        result.update(usage)

    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        pct = result.get("percentage")
        if pct is not None:
            status = ""
            if result.get("compact_urgent"):
                status = " [!!! COMPACT推奨 !!!]"
            elif result.get("compact_recommended"):
                status = " [compact推奨]"
            print(f"Context: ~{pct}% ({result['effective_context_tokens']:,} / {CONTEXT_WINDOW:,} tokens){status}")
            print(f"  セッション: {result.get('message_count', 0)}メッセージ, 出力計{result.get('total_output_tokens', 0):,} tok")
        elif result.get("error"):
            print(f"Error: {result['error']}")


if __name__ == "__main__":
    main()
