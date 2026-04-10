"""Escalation — 3段階エスカレーション+締切管理.

Level 1: 単一サービスダウン → Discord Webhook
Level 2: 全AIダウン → Discord + LINE Notify
Level 3: 全AIダウン + 24h以内の締切 → Discord + LINE + Email

締切管理: deadlines.json で管理。
failover_orchestrator.py と連携して自動実行。

使い方:
    python scripts/escalation.py              # チェック+エスカレーション
    python scripts/escalation.py status       # 状態表示
    python scripts/escalation.py deadlines    # 締切一覧
    python scripts/escalation.py add "名前" "2026-04-15"  # 締切追加
"""

import io
import json
import os
import smtplib
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

STATE_DIR = Path.home() / ".helix-agent" / "escalation"
STATE_FILE = STATE_DIR / "state.json"
DEADLINES_FILE = STATE_DIR / "deadlines.json"
CONFIG_FILE = STATE_DIR / "config.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"
FAILOVER_STATE = Path.home() / ".helix-agent" / "failover" / "state.json"

OLLAMA_URL = "http://localhost:11434"
QDRANT_URL = "http://localhost:6333"

# デフォルト設定
DEFAULT_CONFIG = {
    "line_notify_token": "",        # LINE Notify トークン（設定後有効化）
    "email": {
        "enabled": False,
        "smtp_server": "",
        "smtp_port": 587,
        "from_addr": "",
        "to_addr": "",
        "password": "",             # アプリパスワード
    },
    "cooldown_minutes": {
        "level1": 30,
        "level2": 15,
        "level3": 5,
    },
}

# エスカレーションレベル定義
LEVELS = [
    {
        "level": 1,
        "name": "単一サービス異常",
        "condition": "single_service_down",
        "channels": ["discord"],
    },
    {
        "level": 2,
        "name": "全AIダウン",
        "condition": "all_ai_down",
        "channels": ["discord", "line"],
    },
    {
        "level": 3,
        "name": "全AIダウン+締切接近",
        "condition": "all_ai_down_deadline",
        "channels": ["discord", "line", "email"],
    },
]


def load_config() -> dict:
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in user.items():
                if isinstance(config.get(k), dict) and isinstance(v, dict):
                    config[k].update(v)
                else:
                    config[k] = v
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_escalation": {}, "history": []}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_deadlines() -> list[dict]:
    if DEADLINES_FILE.exists():
        try:
            return json.loads(DEADLINES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    # デフォルト締切
    return [
        {"name": "claude_max_20x", "date": "2026-06-30", "description": "Claude Max 20x OSS無料（5000+ stars）"},
    ]


def save_deadlines(deadlines: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DEADLINES_FILE.write_text(json.dumps(deadlines, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 通知チャネル
# ---------------------------------------------------------------------------

def send_discord(message: str) -> bool:
    if WEBHOOK_SCRIPT.exists():
        try:
            result = subprocess.run(
                [sys.executable, str(WEBHOOK_SCRIPT), message],
                capture_output=True, text=True, timeout=30,
            )
            return result.returncode == 0
        except Exception:
            pass
    return False


def send_line(message: str, token: str) -> bool:
    """LINE Notify送信."""
    if not token:
        return False
    try:
        data = f"message={message}".encode("utf-8")
        req = urllib.request.Request(
            "https://notify-api.line.me/api/notify",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"  LINE送信失敗: {e}")
        return False


def send_email(message: str, config: dict) -> bool:
    """Email送信."""
    email_cfg = config.get("email", {})
    if not email_cfg.get("enabled") or not email_cfg.get("smtp_server"):
        return False
    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = "[HELIX ALERT] AIサービス異常"
        msg["From"] = email_cfg["from_addr"]
        msg["To"] = email_cfg["to_addr"]

        with smtplib.SMTP(email_cfg["smtp_server"], email_cfg["smtp_port"]) as server:
            server.starttls()
            server.login(email_cfg["from_addr"], email_cfg["password"])
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"  Email送信失敗: {e}")
        return False


# ---------------------------------------------------------------------------
# エスカレーション判定
# ---------------------------------------------------------------------------

def check_ai_services() -> dict:
    """AIサービスの状態を取得（failoverの状態を参照）."""
    result = {
        "claude_code": False,
        "codex": False,
        "ollama": False,
        "any_alive": False,
        "degraded": False,
        "details": [],
    }

    # failover_orchestratorの状態を読む
    if FAILOVER_STATE.exists():
        try:
            fo = json.loads(FAILOVER_STATE.read_text(encoding="utf-8"))
            services = fo.get("services", {})
            result["claude_code"] = services.get("claude_code", False)
            result["codex"] = services.get("codex", False)
            result["ollama"] = services.get("ollama", False)
        except (json.JSONDecodeError, OSError):
            pass

    # 直接チェック（failoverの状態が古い場合のフォールバック）
    if not any([result["claude_code"], result["codex"], result["ollama"]]):
        try:
            urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3)
            result["ollama"] = True
        except Exception:
            pass

    result["any_alive"] = any([result["claude_code"], result["codex"], result["ollama"]])
    result["degraded"] = not result["claude_code"] and result["any_alive"]

    if not result["claude_code"]:
        result["details"].append("Claude Code停止")
    if not result["codex"]:
        result["details"].append("Codex停止")
    if not result["ollama"]:
        result["details"].append("Ollama停止")

    return result


def get_upcoming_deadlines(hours: int = 24) -> list[dict]:
    """指定時間以内の締切を取得."""
    deadlines = load_deadlines()
    now = datetime.now(timezone.utc)
    upcoming = []

    for dl in deadlines:
        try:
            # 日付パース（JSTを想定）
            dl_date = datetime.strptime(dl["date"], "%Y-%m-%d").replace(
                tzinfo=timezone(timedelta(hours=9))
            )
            remaining = dl_date - now
            if timedelta(0) < remaining <= timedelta(hours=hours):
                upcoming.append({
                    **dl,
                    "remaining_hours": round(remaining.total_seconds() / 3600, 1),
                })
        except (ValueError, KeyError):
            continue

    return upcoming


def should_escalate(state: dict, level: int, config: dict) -> bool:
    """クールダウン期間内かチェック."""
    key = f"level{level}"
    last = state.get("last_escalation", {}).get(key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        cooldown = config.get("cooldown_minutes", {}).get(key, 30)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() > cooldown * 60
    except (ValueError, TypeError):
        return True


def determine_level(ai_status: dict, upcoming_deadlines: list[dict]) -> int:
    """エスカレーションレベルを判定."""
    if not ai_status["any_alive"] and upcoming_deadlines:
        return 3
    elif not ai_status["any_alive"]:
        return 2
    elif ai_status["degraded"]:
        return 1
    return 0


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def run_check() -> dict:
    """エスカレーションチェック実行."""
    config = load_config()
    state = load_state()
    now_str = datetime.now(timezone.utc).isoformat()

    # 初回は設定ファイル生成
    if not CONFIG_FILE.exists():
        save_config(config)
    if not DEADLINES_FILE.exists():
        save_deadlines(load_deadlines())

    print("=== Escalation Check ===")
    print()

    # AIサービス状態
    ai_status = check_ai_services()
    print("[AIサービス]")
    print(f"  Claude Code: {'✅' if ai_status['claude_code'] else '❌'}")
    print(f"  Codex: {'✅' if ai_status['codex'] else '❌'}")
    print(f"  Ollama: {'✅' if ai_status['ollama'] else '❌'}")

    # 締切チェック
    upcoming = get_upcoming_deadlines(hours=24)
    all_deadlines = load_deadlines()
    print(f"\n[締切] 登録: {len(all_deadlines)}件, 24h以内: {len(upcoming)}件")
    for dl in upcoming:
        print(f"  ⏰ {dl['name']}: {dl['date']} (残り{dl['remaining_hours']}時間)")

    # レベル判定
    level = determine_level(ai_status, upcoming)
    print(f"\n[エスカレーション] レベル: {level}")

    if level == 0:
        print("  → 正常。エスカレーション不要。")
        save_state(state)
        return {"level": 0, "status": "ok"}

    level_def = LEVELS[level - 1]
    print(f"  → {level_def['name']}")

    # クールダウンチェック
    if not should_escalate(state, level, config):
        print("  → クールダウン中。送信スキップ。")
        return {"level": level, "status": "cooldown"}

    # メッセージ構築
    msg_parts = [f"🚨 **Escalation Level {level}: {level_def['name']}**"]
    msg_parts.append(f"サービス: {', '.join(ai_status['details'])}")
    if upcoming:
        for dl in upcoming:
            msg_parts.append(f"⏰ 締切接近: {dl['name']} ({dl['date']}, 残り{dl['remaining_hours']}h)")
    message = "\n".join(msg_parts)

    # 送信
    sent = {}
    for channel in level_def["channels"]:
        if channel == "discord":
            sent["discord"] = send_discord(message)
        elif channel == "line":
            token = config.get("line_notify_token", "")
            sent["line"] = send_line(message, token)
        elif channel == "email":
            sent["email"] = send_email(message, config)

    print(f"  送信結果: {sent}")

    # 状態更新
    state.setdefault("last_escalation", {})[f"level{level}"] = now_str
    state.setdefault("history", []).append({
        "time": now_str,
        "level": level,
        "sent": sent,
        "ai_status": {k: v for k, v in ai_status.items() if k != "details"},
    })
    state["history"] = state["history"][-100:]
    save_state(state)

    return {"level": level, "status": "sent", "sent": sent}


def show_status():
    state = load_state()
    print("=== Escalation Status ===")
    last = state.get("last_escalation", {})
    for k, v in last.items():
        print(f"  {k}: {v}")

    history = state.get("history", [])
    if history:
        print(f"\n履歴 ({len(history)}件):")
        for h in history[-5:]:
            print(f"  [{h['time'][:19]}] Level {h['level']}: {h.get('sent', {})}")


def show_deadlines():
    deadlines = load_deadlines()
    now = datetime.now(timezone.utc)
    print("=== Deadlines ===")
    for dl in sorted(deadlines, key=lambda x: x.get("date", "")):
        try:
            dl_date = datetime.strptime(dl["date"], "%Y-%m-%d").replace(
                tzinfo=timezone(timedelta(hours=9))
            )
            remaining = dl_date - now
            days = remaining.days
            status = f"残り{days}日" if days >= 0 else f"超過{-days}日"
        except ValueError:
            status = "日付不明"
        desc = dl.get("description", "")
        print(f"  {dl['name']}: {dl['date']} ({status}) {desc}")


def add_deadline(name: str, date: str, description: str = ""):
    deadlines = load_deadlines()
    deadlines.append({"name": name, "date": date, "description": description})
    save_deadlines(deadlines)
    print(f"締切追加: {name} ({date})")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif len(sys.argv) > 1 and sys.argv[1] == "deadlines":
        show_deadlines()
    elif len(sys.argv) > 2 and sys.argv[1] == "add":
        name = sys.argv[2]
        date = sys.argv[3] if len(sys.argv) > 3 else ""
        desc = sys.argv[4] if len(sys.argv) > 4 else ""
        add_deadline(name, date, desc)
    else:
        run_check()
