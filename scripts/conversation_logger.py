"""Conversation Logger — Claude Code会話ログの可読テキスト変換+自動ローテーション.

Claude Codeが自動保存するJSONLトランスクリプトを
人間が読める Markdown 形式に変換する。

トークン消費: 完全にゼロ（Pure Python、API呼び出しなし）

機能:
  1. JSONL → 可読 Markdown 変換
  2. セッション別ファイル出力
  3. 自動ローテーション（日数 or サイズ上限）
  4. 差分変換（前回変換位置から追記）

使い方:
    python scripts/conversation_logger.py                 # 変換+ローテーション
    python scripts/conversation_logger.py status          # 状態表示
    python scripts/conversation_logger.py convert         # 変換のみ
    python scripts/conversation_logger.py rotate          # ローテーションのみ
    python scripts/conversation_logger.py convert --all   # 全セッション再変換
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# JSONL ソースディレクトリ
JSONL_DIR = Path.home() / ".claude" / "projects" / "C--Development"

# 出力先
OUTPUT_DIR = Path.home() / ".helix-agent" / "conversation_logs"

# 変換状態ファイル
STATE_FILE = OUTPUT_DIR / "converter_state.json"

# ローテーション設定
MAX_RETENTION_DAYS = 7       # 7日分保持
MAX_TOTAL_SIZE_MB = 100      # 合計100MB上限
MIN_KEEP_FILES = 3           # 最低3ファイルは保持

# ---------------------------------------------------------------------------
# JSONL パーサー
# ---------------------------------------------------------------------------


def parse_jsonl_to_messages(jsonl_path: Path) -> list[dict]:
    """JSONLファイルから会話メッセージを抽出."""
    messages = []

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type", "")
                timestamp = entry.get("timestamp", "")

                if entry_type == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        messages.append({
                            "type": "user",
                            "timestamp": timestamp,
                            "content": content.strip(),
                            "line": line_num,
                        })

                elif entry_type == "assistant":
                    msg = entry.get("message", {})
                    content_blocks = msg.get("content", [])
                    model = msg.get("model", "unknown")
                    usage = msg.get("usage", {})

                    text_parts = []
                    tool_calls = []
                    thinking_parts = []

                    if isinstance(content_blocks, list):
                        for block in content_blocks:
                            if not isinstance(block, dict):
                                continue
                            block_type = block.get("type", "")

                            if block_type == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    text_parts.append(text)

                            elif block_type == "tool_use":
                                tool_name = block.get("name", "unknown")
                                tool_input = block.get("input", {})
                                tool_summary = _summarize_tool_call(tool_name, tool_input)
                                tool_calls.append(tool_summary)

                            elif block_type == "thinking":
                                thinking = block.get("thinking", "").strip()
                                if thinking:
                                    # 思考は最初の100文字だけ記録
                                    thinking_parts.append(thinking[:100] + ("..." if len(thinking) > 100 else ""))

                    if text_parts or tool_calls:
                        messages.append({
                            "type": "assistant",
                            "timestamp": timestamp,
                            "model": model,
                            "text": "\n".join(text_parts),
                            "tools": tool_calls,
                            "thinking_preview": thinking_parts[:2],  # 最大2つ
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                            "line": line_num,
                        })

                elif entry_type == "system":
                    subtype = entry.get("subtype", "")
                    if subtype == "turn_duration":
                        duration_ms = entry.get("durationMs", 0)
                        msg_count = entry.get("messageCount", 0)
                        if duration_ms > 0:
                            messages.append({
                                "type": "system_duration",
                                "timestamp": timestamp,
                                "duration_sec": round(duration_ms / 1000, 1),
                                "message_count": msg_count,
                                "line": line_num,
                            })

    except (OSError, UnicodeDecodeError) as e:
        print(f"  読み込みエラー: {jsonl_path.name}: {e}")

    return messages


def _summarize_tool_call(tool_name: str, tool_input: dict) -> str:
    """ツールコールを1行要約."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"Bash: `{cmd}`"

    elif tool_name == "Read":
        path = tool_input.get("file_path", "")
        # パスの末尾部分だけ表示
        short = _shorten_path(path)
        offset = tool_input.get("offset", "")
        limit = tool_input.get("limit", "")
        suffix = ""
        if offset or limit:
            suffix = f" (L{offset}-{(offset or 0) + (limit or 0)})"
        return f"Read: {short}{suffix}"

    elif tool_name == "Edit":
        path = tool_input.get("file_path", "")
        short = _shorten_path(path)
        return f"Edit: {short}"

    elif tool_name == "Write":
        path = tool_input.get("file_path", "")
        short = _shorten_path(path)
        return f"Write: {short}"

    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"Grep: `{pattern}`"

    elif tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        return f"Glob: `{pattern}`"

    elif tool_name == "Agent":
        desc = tool_input.get("description", "")
        subtype = tool_input.get("subagent_type", "")
        return f"Agent({subtype}): {desc}" if subtype else f"Agent: {desc}"

    elif tool_name.startswith("mcp__"):
        # MCP tools - short name
        parts = tool_name.split("__")
        short_name = "__".join(parts[-2:]) if len(parts) > 2 else tool_name
        return f"MCP: {short_name}"

    else:
        return f"{tool_name}"


def _shorten_path(path: str) -> str:
    """パスを短縮表示."""
    if not path:
        return "(unknown)"
    p = Path(path)
    parts = p.parts
    if len(parts) > 3:
        return f".../{'/'.join(parts[-3:])}"
    return str(p)


# ---------------------------------------------------------------------------
# Markdown 変換
# ---------------------------------------------------------------------------


def messages_to_markdown(messages: list[dict], session_info: dict) -> str:
    """メッセージリストを可読Markdownに変換."""
    lines = []

    # ヘッダー
    lines.append(f"# 会話ログ: {session_info.get('slug', 'unknown')}")
    lines.append("")
    lines.append(f"- **セッションID**: `{session_info.get('session_id', 'unknown')}`")
    lines.append(f"- **開始**: {session_info.get('start_time', 'unknown')}")
    lines.append(f"- **変換日時**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"- **メッセージ数**: {len([m for m in messages if m['type'] in ('user', 'assistant')])}")

    total_input = sum(m.get("input_tokens", 0) for m in messages if m["type"] == "assistant")
    total_output = sum(m.get("output_tokens", 0) for m in messages if m["type"] == "assistant")
    if total_input or total_output:
        lines.append(f"- **トークン**: 入力 {total_input:,} / 出力 {total_output:,}")

    lines.append("")
    lines.append("---")
    lines.append("")

    # メッセージ本文
    for msg in messages:
        ts = _format_timestamp(msg.get("timestamp", ""))

        if msg["type"] == "user":
            lines.append(f"## [{ts}] User")
            lines.append("")
            lines.append(msg["content"])
            lines.append("")

        elif msg["type"] == "assistant":
            model_short = msg.get("model", "").replace("claude-", "").replace("-", " ")
            tok_info = ""
            if msg.get("input_tokens") or msg.get("output_tokens"):
                tok_info = f" ({msg['input_tokens']:,}→{msg['output_tokens']:,} tok)"
            lines.append(f"## [{ts}] Assistant ({model_short}){tok_info}")
            lines.append("")

            # 思考プレビュー
            if msg.get("thinking_preview"):
                lines.append("<details><summary>思考</summary>")
                for tp in msg["thinking_preview"]:
                    lines.append(f"  {tp}")
                lines.append("</details>")
                lines.append("")

            # テキスト
            if msg.get("text"):
                lines.append(msg["text"])
                lines.append("")

            # ツールコール
            if msg.get("tools"):
                lines.append("**ツール呼び出し:**")
                for tool in msg["tools"]:
                    lines.append(f"- {tool}")
                lines.append("")

        elif msg["type"] == "system_duration":
            lines.append(f"*[ターン完了: {msg['duration_sec']}秒, {msg['message_count']}メッセージ]*")
            lines.append("")

    return "\n".join(lines)


def _format_timestamp(ts: str) -> str:
    """ISO タイムスタンプをJST表示に変換."""
    if not ts:
        return "??:??"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        jst = dt + timedelta(hours=9)
        return jst.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return ts[:19]


# ---------------------------------------------------------------------------
# 変換メイン
# ---------------------------------------------------------------------------


def load_state() -> dict:
    """変換状態を読み込み."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"converted_sessions": {}, "last_run": None}


def save_state(state: dict) -> None:
    """変換状態を保存."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def find_jsonl_sessions() -> list[dict]:
    """JSONL ファイルを検索し、セッション情報を返す."""
    sessions = []
    if not JSONL_DIR.exists():
        return sessions

    for jsonl_file in JSONL_DIR.glob("*.jsonl"):
        # ファイル名がUUID形式か確認
        stem = jsonl_file.stem
        if len(stem) < 20:  # UUID-like
            continue

        stat = jsonl_file.stat()
        # 最初の行からセッション情報を取得
        session_info = _extract_session_info(jsonl_file)
        session_info["path"] = str(jsonl_file)
        session_info["size"] = stat.st_size
        session_info["mtime"] = stat.st_mtime
        sessions.append(session_info)

    # 更新日時順（新しい順）
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def _extract_session_info(jsonl_path: Path) -> dict:
    """JSONLファイルの先頭からセッション情報を抽出."""
    info = {
        "session_id": jsonl_path.stem,
        "slug": "",
        "start_time": "",
    }
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > 10:  # 最初の10行だけチェック
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if not info["slug"] and entry.get("slug"):
                        info["slug"] = entry["slug"]
                    if not info["start_time"] and entry.get("timestamp"):
                        info["start_time"] = entry["timestamp"]
                    if info["slug"] and info["start_time"]:
                        break
                except json.JSONDecodeError:
                    continue
    except (OSError, UnicodeDecodeError):
        pass
    return info


def convert_sessions(force_all: bool = False) -> list[str]:
    """JSONL セッションを Markdown に変換."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    sessions = find_jsonl_sessions()
    converted = []

    for session in sessions:
        sid = session["session_id"]
        mtime = session["mtime"]

        # 差分チェック: 前回変換時のmtimeと比較
        prev = state.get("converted_sessions", {}).get(sid, {})
        if not force_all and prev.get("mtime") == mtime:
            continue  # 変更なし、スキップ

        print(f"  変換中: {session['slug'] or sid[:12]}...")
        jsonl_path = Path(session["path"])
        messages = parse_jsonl_to_messages(jsonl_path)

        if not messages:
            print(f"    → メッセージなし、スキップ")
            continue

        # 出力ファイル名: 日付_slug.md
        ts = session.get("start_time", "")
        date_str = ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                jst = dt + timedelta(hours=9)
                date_str = jst.strftime("%Y%m%d_%H%M")
            except (ValueError, TypeError):
                pass
        if not date_str:
            date_str = datetime.fromtimestamp(mtime).strftime("%Y%m%d_%H%M")

        slug = session.get("slug", "")
        safe_slug = slug.replace(" ", "_")[:30] if slug else sid[:8]
        output_name = f"{date_str}_{safe_slug}.md"
        output_path = OUTPUT_DIR / output_name

        # Markdown 変換
        md_content = messages_to_markdown(messages, session)
        output_path.write_text(md_content, encoding="utf-8")

        # 状態更新
        state.setdefault("converted_sessions", {})[sid] = {
            "mtime": mtime,
            "output": output_name,
            "message_count": len([m for m in messages if m["type"] in ("user", "assistant")]),
            "converted_at": datetime.now(timezone.utc).isoformat(),
        }
        converted.append(output_name)
        print(f"    → {output_name} ({output_path.stat().st_size / 1024:.1f} KB)")

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    return converted


# ---------------------------------------------------------------------------
# ローテーション
# ---------------------------------------------------------------------------


def rotate_logs() -> list[str]:
    """古いログを自動削除."""
    if not OUTPUT_DIR.exists():
        return []

    # Markdown ログファイルを列挙
    log_files = sorted(OUTPUT_DIR.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    if len(log_files) <= MIN_KEEP_FILES:
        return []

    removed = []
    now = time.time()
    cutoff = now - (MAX_RETENTION_DAYS * 86400)
    total_size = sum(f.stat().st_size for f in log_files)
    max_bytes = MAX_TOTAL_SIZE_MB * 1024 * 1024

    for i, log_file in enumerate(log_files):
        if i < MIN_KEEP_FILES:
            continue  # 最低保持数

        stat = log_file.stat()
        should_remove = False

        # 日数超過
        if stat.st_mtime < cutoff:
            should_remove = True

        # サイズ超過
        if total_size > max_bytes:
            should_remove = True

        if should_remove:
            total_size -= stat.st_size
            log_file.unlink()
            removed.append(log_file.name)
            print(f"  削除: {log_file.name} ({stat.st_size / 1024:.1f} KB)")

    # 状態ファイルからも削除
    if removed:
        state = load_state()
        sessions = state.get("converted_sessions", {})
        for sid in list(sessions.keys()):
            if sessions[sid].get("output") in removed:
                del sessions[sid]
        save_state(state)

    return removed


# ---------------------------------------------------------------------------
# ステータス表示
# ---------------------------------------------------------------------------


def show_status():
    """現在の状態を表示."""
    state = load_state()
    print("=== Conversation Logger Status ===")
    print(f"  JSONL ソース: {JSONL_DIR}")
    print(f"  出力先: {OUTPUT_DIR}")
    print(f"  最終実行: {state.get('last_run', 'なし')}")
    print(f"  ローテーション: {MAX_RETENTION_DAYS}日 / {MAX_TOTAL_SIZE_MB}MB上限")
    print()

    # JSONL ファイル一覧
    sessions = find_jsonl_sessions()
    print(f"[JSONL セッション: {len(sessions)}件]")
    for s in sessions[:5]:
        size_mb = s["size"] / 1024 / 1024
        slug = s.get("slug", "")[:25] or s["session_id"][:12]
        converted = "✅" if s["session_id"] in state.get("converted_sessions", {}) else "⬜"
        print(f"  {converted} {slug} ({size_mb:.1f} MB)")
    if len(sessions) > 5:
        print(f"  ... 他 {len(sessions) - 5}件")

    # 変換済みログ
    if OUTPUT_DIR.exists():
        log_files = sorted(OUTPUT_DIR.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
        total_kb = sum(f.stat().st_size for f in log_files) / 1024
        print(f"\n[変換済みログ: {len(log_files)}件, 合計 {total_kb:.1f} KB]")
        for lf in log_files[:5]:
            size_kb = lf.stat().st_size / 1024
            print(f"  {lf.name} ({size_kb:.1f} KB)")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def main():
    args = sys.argv[1:]

    if not args or args[0] not in ("status", "convert", "rotate"):
        # デフォルト: 変換+ローテーション
        print("=== Conversation Logger ===")
        print()
        print("[変換]")
        converted = convert_sessions()
        if converted:
            print(f"  {len(converted)}件変換完了")
        else:
            print("  変換対象なし（すべて最新）")

        print("\n[ローテーション]")
        removed = rotate_logs()
        if removed:
            print(f"  {len(removed)}件削除")
        else:
            print("  削除対象なし")
        return

    if args[0] == "status":
        show_status()
    elif args[0] == "convert":
        force_all = "--all" in args
        converted = convert_sessions(force_all=force_all)
        print(f"変換完了: {len(converted)}件")
    elif args[0] == "rotate":
        removed = rotate_logs()
        print(f"削除完了: {len(removed)}件")


if __name__ == "__main__":
    main()
