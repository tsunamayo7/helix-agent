"""Claude使用率スクレイパー — claude.ai/settings/usage からリアルタイム使用率を取得.

Claude Codeセッション中にclaude-in-chrome MCP経由でテキスト取得したデータを受け取り、
パース・保存・閾値チェックを行う。

使い方（Claude Codeセッション内で呼ばれる）:
    python scripts/claude_usage_scraper.py save "テキストデータ"   # パース+保存
    python scripts/claude_usage_scraper.py status                   # 最新データ表示
    python scripts/claude_usage_scraper.py check                    # 閾値チェック
    python scripts/claude_usage_scraper.py --json status            # JSON出力
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
DATA_DIR = Path.home() / ".helix-agent" / "claude_usage"
LATEST_FILE = DATA_DIR / "latest.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"
CODEX_FILE = DATA_DIR / "codex_latest.json"

# 閾値
WARN_PCT = 70
CRITICAL_PCT = 85


def parse_usage(text: str) -> dict | None:
    """使用率テキストをパースする."""
    result = {
        "timestamp": datetime.now(JST).isoformat(),
        "session": {},
        "weekly_all": {},
        "sonnet_only": {},
        "extra_usage": {},
    }

    # セッション: "XX% 使用済み" パターンを順番に取得
    # テキスト構造: ... 現在のセッション ... XX% 使用済み ... 週間制限 ... XX% 使用済み ... Sonnetのみ ... XX% 使用済み
    pct_matches = re.findall(r'(\d+)\s*%\s*使用済み', text)
    reset_matches = re.findall(r'(.+)にリセット', text)

    if len(pct_matches) >= 3:
        result["session"]["percent"] = int(pct_matches[0])
        result["weekly_all"]["percent"] = int(pct_matches[1])
        result["sonnet_only"]["percent"] = int(pct_matches[2])
    elif len(pct_matches) >= 2:
        result["weekly_all"]["percent"] = int(pct_matches[0])
        result["sonnet_only"]["percent"] = int(pct_matches[1])
    else:
        return None

    # リセット時刻
    for i, match in enumerate(reset_matches):
        match = match.strip()
        if i == 0:
            result["session"]["reset"] = match
        elif i == 1:
            result["weekly_all"]["reset"] = match
        elif i == 2:
            result["sonnet_only"]["reset"] = match

    # 追加使用量
    extra_match = re.search(r'\$(\d+(?:\.\d+)?)\s*使用', text)
    if extra_match:
        result["extra_usage"]["used_usd"] = float(extra_match.group(1))

    extra_pct = re.findall(r'(\d+)%\s*使用', text)
    # 最後のは追加使用量のパーセント
    if len(extra_pct) >= 4:
        result["extra_usage"]["percent"] = int(extra_pct[3])

    return result


def parse_codex_usage(text: str) -> dict | None:
    """Codex使用率テキストをパースする."""
    result = {
        "timestamp": datetime.now(JST).isoformat(),
        "service": "codex",
        "limits": {},
    }

    # "XX% 残り" パターンを抽出（Codexは「残り」表示）
    # テキスト構造: ... 5時間の使用制限 100% 残り ... 週あたりの使用制限 100% 残り ...
    lines = text.split("\n")
    current_label = None
    pending_pct = None
    for line in lines:
        line = line.strip()
        if "使用制限" in line or "コードレビュー" in line:
            current_label = line
            pending_pct = None
        elif current_label and re.match(r'^\d+\s*%$', line):
            pending_pct = int(re.search(r'(\d+)', line).group(1))
        elif "残り" in line and current_label:
            pct_match = re.search(r'(\d+)\s*%', line)
            remaining = int(pct_match.group(1)) if pct_match else pending_pct
            if remaining is not None:
                used = 100 - remaining
                key = current_label.replace(" ", "_").replace("の", "_")
                result["limits"][key] = {
                    "remaining_percent": remaining,
                    "used_percent": used,
                    "label": current_label,
                }
            current_label = None
            pending_pct = None

    if not result["limits"]:
        return None
    return result


def save_codex_usage(data: dict):
    """Codex使用率データを保存."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CODEX_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 保存・通知
# ---------------------------------------------------------------------------

def save_usage(data: dict):
    """使用率データを保存."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 履歴にも追記
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def check_alerts(data: dict) -> list[str]:
    """閾値チェック."""
    alerts = []
    weekly = data.get("weekly_all", {}).get("percent", 0)
    sonnet = data.get("sonnet_only", {}).get("percent", 0)
    session = data.get("session", {}).get("percent", 0)

    if weekly >= CRITICAL_PCT:
        alerts.append(f"CRITICAL: 週間全モデル {weekly}% (リセット: {data['weekly_all'].get('reset', '?')})")
    elif weekly >= WARN_PCT:
        alerts.append(f"WARNING: 週間全モデル {weekly}% (リセット: {data['weekly_all'].get('reset', '?')})")

    if sonnet >= CRITICAL_PCT:
        alerts.append(f"CRITICAL: Sonnet {sonnet}% (リセット: {data['sonnet_only'].get('reset', '?')})")
    elif sonnet >= WARN_PCT:
        alerts.append(f"WARNING: Sonnet {sonnet}% (リセット: {data['sonnet_only'].get('reset', '?')})")

    return alerts


def send_discord(message: str):
    """Discord Webhook送信."""
    if WEBHOOK_SCRIPT.exists():
        try:
            subprocess.run(
                ["python", str(WEBHOOK_SCRIPT), message],
                timeout=15, capture_output=True,
            )
        except Exception:
            pass


def show_status():
    """最新の使用率を表示."""
    if not LATEST_FILE.exists():
        print("データなし。先に取得を実行してください。")
        return

    data = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
    ts = data.get("timestamp", "?")
    session = data.get("session", {})
    weekly = data.get("weekly_all", {})
    sonnet = data.get("sonnet_only", {})

    print(f"=== Claude Usage ({ts}) ===")
    if session:
        print(f"  セッション:    {session.get('percent', '?')}%  (リセット: {session.get('reset', '?')})")
    print(f"  週間(全モデル): {weekly.get('percent', '?')}%  (リセット: {weekly.get('reset', '?')})")
    print(f"  Sonnetのみ:    {sonnet.get('percent', '?')}%  (リセット: {sonnet.get('reset', '?')})")

    alerts = check_alerts(data)
    if alerts:
        print()
        for a in alerts:
            print(f"  ⚠ {a}")

    # Codex使用率
    if CODEX_FILE.exists():
        cdata = json.loads(CODEX_FILE.read_text(encoding="utf-8"))
        print(f"\n=== Codex Usage ({cdata.get('timestamp', '?')[:19]}) ===")
        for key, info in cdata.get("limits", {}).items():
            remaining = info.get("remaining_percent", "?")
            print(f"  {info.get('label', key):30s} 残り{remaining}%")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Claude使用率スクレイパー")
    parser.add_argument("command", nargs="?", default="status",
                        choices=["save", "save_codex", "status", "check"],
                        help="save=Claude保存, save_codex=Codex保存, status=最新表示, check=閾値チェック")
    parser.add_argument("text", nargs="?", default=None,
                        help="saveコマンド用: get_page_textの出力テキスト")
    parser.add_argument("--json", action="store_true", help="JSON出力")
    parser.add_argument("--alert", action="store_true", help="Discord通知を有効化")
    args = parser.parse_args()

    if args.command == "status":
        if args.json and LATEST_FILE.exists():
            print(LATEST_FILE.read_text(encoding="utf-8"))
        else:
            show_status()
        return

    if args.command == "check":
        if not LATEST_FILE.exists():
            print("データなし", file=sys.stderr)
            sys.exit(1)
        data = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
        alerts = check_alerts(data)
        if alerts:
            if args.alert:
                msg = "**Claude使用率アラート**\n" + "\n".join(f"- {a}" for a in alerts)
                send_discord(msg)
            for a in alerts:
                print(f"⚠ {a}")
        else:
            print("閾値内")
        return

    if args.command == "save_codex":
        text = args.text
        if not text:
            text = sys.stdin.read()
        if not text:
            print("テキストが必要です", file=sys.stderr)
            sys.exit(1)
        data = parse_codex_usage(text)
        if not data:
            print("Codexパース失敗", file=sys.stderr)
            sys.exit(1)
        save_codex_usage(data)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"Codex使用率保存完了: {len(data['limits'])}項目")
            for key, info in data["limits"].items():
                print(f"  {info['label']}: 残り{info['remaining_percent']}% (使用{info['used_percent']}%)")
        return

    if args.command == "save":
        text = args.text
        if not text:
            text = sys.stdin.read()
        if not text:
            print("テキストが必要です", file=sys.stderr)
            sys.exit(1)

        data = parse_usage(text)
        if not data:
            print("パース失敗", file=sys.stderr)
            sys.exit(1)

        save_usage(data)

        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            show_status()

        # 閾値チェック
        if args.alert:
            alerts = check_alerts(data)
            if alerts:
                msg = "**Claude使用率アラート**\n" + "\n".join(f"- {a}" for a in alerts)
                send_discord(msg)


if __name__ == "__main__":
    main()
