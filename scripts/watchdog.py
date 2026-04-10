"""Claude Code Watchdog — CLIプロセス監視・異常時Discord通知.

Claude CLIがクラッシュやエラーで停止した場合に、
独立してDiscord Webhookで通知を送る監視機構。

Windowsタスクスケジューラから5分間隔で実行される想定。

使い方:
    python scripts/watchdog.py           # 1回チェック
    python scripts/watchdog.py status    # 現在の監視状態表示
    python scripts/watchdog.py reset     # アラート状態リセット
"""

import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Windows cp932対策
if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

WATCHDOG_DIR = Path.home() / ".helix-agent" / "watchdog"
STATE_FILE = WATCHDOG_DIR / "state.json"
HW_STATUS_FILE = Path.home() / ".helix-agent" / "hw_monitor" / "hw_status.json"

# Discord Webhook（環境変数 or fallbackスクリプト経由）
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# 閾値
CLI_ABSENCE_WARN_MIN = 15      # CLIプロセスが15分不在で警告
HW_STALE_WARN_MIN = 15         # hw_monitorの更新が15分以上前で警告
ALERT_COOLDOWN_MIN = 30        # 同一アラートの再通知間隔


def load_state() -> dict:
    """監視状態を読み込み."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "last_check": None,
        "cli_running": False,
        "last_cli_seen": None,
        "last_alert_time": {},
        "alert_history": [],
    }


def save_state(state: dict) -> None:
    """監視状態を保存."""
    WATCHDOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_claude_cli_running() -> bool:
    """Claude CLIプロセスが動作中か確認."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        if "claude.exe" in result.stdout.lower():
            return True
    except Exception:
        pass

    # Node.jsベースのClaude Codeも確認
    try:
        no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process -Name node -ErrorAction SilentlyContinue | "
             "Where-Object { $_.CommandLine -match 'claude' } | "
             "Select-Object -First 1 -ExpandProperty Id"],
            capture_output=True, text=True, timeout=10,
            creationflags=no_window,
        )
        if result.stdout.strip():
            return True
    except Exception:
        pass

    return False


def check_hw_monitor_freshness() -> tuple[bool, float | None]:
    """hw_monitorの最終更新からの経過時間(分)を確認."""
    if not HW_STATUS_FILE.exists():
        return False, None
    try:
        mtime = HW_STATUS_FILE.stat().st_mtime
        age_min = (time.time() - mtime) / 60
        return True, age_min
    except OSError:
        return False, None


def send_discord_alert(message: str) -> bool:
    """Discord Webhookで通知を送信."""
    # まずfallbackスクリプト経由を試行
    if WEBHOOK_SCRIPT.exists():
        try:
            result = subprocess.run(
                [sys.executable, str(WEBHOOK_SCRIPT), message],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    # 環境変数からWebhook URL取得して直接送信
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        # config.yamlから読み取り試行
        config_path = Path("C:/Development/tools/x-feed-collector/config.yaml")
        if config_path.exists():
            try:
                import yaml
                config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                webhook_url = config.get("discord", {}).get("webhook_url")
            except Exception:
                pass

    if webhook_url:
        try:
            data = json.dumps({
                "content": message,
                "username": "Watchdog",
            }).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception:
            pass

    return False


def should_alert(state: dict, alert_type: str) -> bool:
    """クールダウン期間内かどうか確認."""
    last = state.get("last_alert_time", {}).get(alert_type)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        now = datetime.now(timezone.utc)
        return (now - last_dt).total_seconds() > ALERT_COOLDOWN_MIN * 60
    except (ValueError, TypeError):
        return True


def run_check() -> list[str]:
    """全チェックを実行し、アラートメッセージのリストを返す."""
    state = load_state()
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()
    alerts = []

    # ハートビート送信
    try:
        from supervisor import write_heartbeat
        write_heartbeat("watchdog")
    except ImportError:
        pass

    # 1. Claude CLIプロセスチェック
    cli_running = is_claude_cli_running()
    state["cli_running"] = cli_running
    state["last_check"] = now_str

    if cli_running:
        state["last_cli_seen"] = now_str
    else:
        last_seen = state.get("last_cli_seen")
        if last_seen:
            try:
                last_dt = datetime.fromisoformat(last_seen)
                absence_min = (now - last_dt).total_seconds() / 60
                if absence_min >= CLI_ABSENCE_WARN_MIN:
                    if should_alert(state, "cli_absent"):
                        # Crash recovery
                        recovery = save_crash_recovery()
                        restart_msg = ""
                        if recovery.get("auto_restart"):
                            if attempt_auto_restart():
                                restart_msg = "\n自動再起動を実行しました。"
                            else:
                                restart_msg = "\n自動再起動に失敗。手動でstart_claude.batを実行してください。"
                        alerts.append(
                            f"⚠️ **Watchdog**: Claude CLIが{int(absence_min)}分間停止中です。"
                            f"\n最終検出: {last_seen[:19]}"
                            f"\nQdrantスナップショット: {'✅' if recovery.get('qdrant_snapshot') else '❌'}"
                            f"\nCMEMバックアップ: {'✅' if recovery.get('cmem_backup') else '❌'}"
                            f"{restart_msg}"
                        )
                        state.setdefault("last_alert_time", {})["cli_absent"] = now_str
            except (ValueError, TypeError):
                pass

    # 2. supervisor相互監視（supervisorが死んでいたら再起動）
    try:
        hb_file = Path.home() / ".helix-agent" / "heartbeats" / "supervisor.json"
        if hb_file.exists():
            hb = json.loads(hb_file.read_text(encoding="utf-8"))
            hb_time = datetime.fromisoformat(hb["timestamp"])
            age_min = (now - hb_time).total_seconds() / 60
            if age_min > 10:  # supervisorは3分間隔なので10分で異常
                # supervisorを再起動
                supervisor_script = Path(__file__).resolve().parent / "supervisor.py"
                subprocess.Popen(
                    [sys.executable, str(supervisor_script)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if should_alert(state, "supervisor_restart"):
                    alerts.append(
                        f"**Watchdog**: supervisorが{int(age_min)}分間応答なし。再起動しました。"
                    )
                    state.setdefault("last_alert_time", {})["supervisor_restart"] = now_str
        else:
            # ハートビートファイルすらない
            supervisor_script = Path(__file__).resolve().parent / "supervisor.py"
            subprocess.Popen(
                [sys.executable, str(supervisor_script)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass

    # 3. hw_monitorの鮮度チェック
    hw_exists, hw_age_min = check_hw_monitor_freshness()
    if hw_exists and hw_age_min is not None:
        if hw_age_min >= HW_STALE_WARN_MIN:
            if should_alert(state, "hw_stale"):
                alerts.append(
                    f"⚠️ **Watchdog**: HWモニターが{int(hw_age_min)}分間更新されていません。"
                    f"\nタスクスケジューラを確認してください。"
                )
                state.setdefault("last_alert_time", {})["hw_stale"] = now_str

    # 3. hw_monitorのアラート転送
    if HW_STATUS_FILE.exists():
        try:
            hw_status = json.loads(HW_STATUS_FILE.read_text(encoding="utf-8"))
            hw_alerts = hw_status.get("alerts", [])
            critical_alerts = [a for a in hw_alerts if a.get("level") == "CRITICAL"]
            if critical_alerts and should_alert(state, "hw_critical"):
                msg_parts = ["🔴 **Watchdog**: ハードウェア異常検出！"]
                for a in critical_alerts:
                    msg_parts.append(f"  - {a.get('message', 'Unknown')}")
                alerts.append("\n".join(msg_parts))
                state.setdefault("last_alert_time", {})["hw_critical"] = now_str
        except (json.JSONDecodeError, OSError):
            pass

    # アラート履歴記録
    for alert in alerts:
        state.setdefault("alert_history", []).append({
            "time": now_str,
            "message": alert[:200],
        })
        # 履歴は最新50件まで
        state["alert_history"] = state["alert_history"][-50:]

    save_state(state)
    return alerts


def save_crash_recovery():
    """CLI異常終了時のクラッシュリカバリ情報を生成."""
    memory_dir = Path.home() / ".claude" / "projects" / "C--Development" / "memory"
    checkpoint_file = memory_dir / "session_checkpoint.json"
    recovery_file = memory_dir / "crash_recovery.json"

    checkpoint = {}
    if checkpoint_file.exists():
        try:
            checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # Qdrant snapshot
    qdrant_snapshot = False
    try:
        req = urllib.request.Request(
            "http://localhost:6333/collections/mem0_shared/snapshots",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        qdrant_snapshot = True
    except Exception:
        pass

    # $CMEM backup
    cmem_backup = False
    cmem_db = Path.home() / ".claude-mem" / "claude-mem.db"
    if cmem_db.exists():
        try:
            import shutil
            shutil.copy2(str(cmem_db), str(cmem_db.with_suffix(".crash_backup")))
            cmem_backup = True
        except Exception:
            pass

    recovery = {
        "crash_time": datetime.now(timezone.utc).isoformat(),
        "last_checkpoint": checkpoint,
        "qdrant_snapshot": qdrant_snapshot,
        "cmem_backup": cmem_backup,
        "auto_restart": True,
    }
    recovery_file.write_text(json.dumps(recovery, ensure_ascii=False, indent=2), encoding="utf-8")
    return recovery


def attempt_auto_restart():
    """CLIのauto restart（bat経由）.

    安全策: 既にCLIが動いている場合は再起動しない（二重起動防止）。
    """
    # 二重起動防止: もう一度CLIの存在を確認
    if is_claude_cli_running():
        return False  # CLIは生きている → 再起動しない

    bat_path = Path("C:/Development/start/manual/start_claude.bat")
    if not bat_path.exists():
        return False
    try:
        no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen(
            ["cmd", "/c", "start", "", str(bat_path)],
            creationflags=no_window,
        )
        return True
    except Exception:
        return False


def show_status():
    """現在の監視状態を表示."""
    state = load_state()
    print("=== Watchdog Status ===")
    print(f"  最終チェック: {state.get('last_check', 'なし')}")
    print(f"  CLI稼働中: {'はい' if state.get('cli_running') else 'いいえ'}")
    print(f"  CLI最終検出: {state.get('last_cli_seen', 'なし')}")

    hw_exists, hw_age = check_hw_monitor_freshness()
    if hw_exists and hw_age is not None:
        print(f"  HWモニター: {int(hw_age)}分前に更新")
    else:
        print("  HWモニター: ファイルなし")

    history = state.get("alert_history", [])
    if history:
        print(f"\n  直近アラート ({len(history)}件):")
        for h in history[-5:]:
            print(f"    [{h['time'][:19]}] {h['message'][:80]}")
    else:
        print("  アラート履歴: なし")


def reset_alerts():
    """アラート状態をリセット."""
    state = load_state()
    state["last_alert_time"] = {}
    state["alert_history"] = []
    save_state(state)
    print("アラート状態をリセットしました。")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif len(sys.argv) > 1 and sys.argv[1] == "reset":
        reset_alerts()
    else:
        alerts = run_check()
        if alerts:
            for alert in alerts:
                print(f"ALERT: {alert}")
                sent = send_discord_alert(alert)
                if sent:
                    print("  → Discord送信成功")
                else:
                    print("  → Discord送信失敗（Webhook未設定の可能性）")
        else:
            print("OK - 異常なし")
