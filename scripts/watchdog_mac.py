"""Mac Watchdog — Claude CLI プロセス生死監視.

5分毎に launchd から起動。
pgrep で Claude CLI の存在を確認し、長時間不在なら通知。
Mac は CEO Node なので CLI 停止 = Corp 全体の判断機能停止。

使い方:
    python3 scripts/watchdog_mac.py           # 1回チェック
    python3 scripts/watchdog_mac.py status    # 監視状態表示
    python3 scripts/watchdog_mac.py reset     # アラート状態リセット
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

HELIX_DIR = Path.home() / ".helix-agent"
HEARTBEAT_DIR = HELIX_DIR / "heartbeats"
STATE_DIR = HELIX_DIR / "watchdog_mac"
STATE_FILE = STATE_DIR / "state.json"
LOG_DIR = Path.home() / ".claude" / "logs"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# 閾値
CLI_ABSENCE_WARN_MIN = 15       # 15分不在で警告
ALERT_COOLDOWN_MIN = 30         # 同一アラートの再通知間隔


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs():
    for d in [HEARTBEAT_DIR, STATE_DIR, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def write_heartbeat():
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    hb = {
        "daemon": "watchdog_mac",
        "pid": os.getpid(),
        "timestamp": now_iso(),
        "status": "alive",
    }
    (HEARTBEAT_DIR / "watchdog_mac.json").write_text(
        json.dumps(hb, ensure_ascii=False), encoding="utf-8",
    )


def load_state() -> dict:
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


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def should_alert(state: dict, key: str) -> bool:
    last = state.get("last_alert_time", {}).get(key)
    if not last:
        return True
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
        return elapsed >= ALERT_COOLDOWN_MIN
    except (ValueError, TypeError):
        return True


def notify(message: str) -> bool:
    if not WEBHOOK_SCRIPT.exists():
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(WEBHOOK_SCRIPT), message],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# プロセスチェック (macOS)
# ---------------------------------------------------------------------------

def is_claude_cli_running() -> dict:
    """Claude CLI プロセスが動作中か確認 (macOS).

    Returns:
        {"running": bool, "pids": list[int], "method": str}
    """
    result = {"running": False, "pids": [], "method": ""}

    # 1. pgrep claude (ネイティブバイナリ)
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "claude"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            pids = [int(p) for p in proc.stdout.strip().split("\n") if p.strip().isdigit()]
            if pids:
                result["running"] = True
                result["pids"] = pids
                result["method"] = "pgrep"
                return result
    except Exception:
        pass

    # 2. node ベースの Claude Code 確認
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "node.*claude"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            pids = [int(p) for p in proc.stdout.strip().split("\n") if p.strip().isdigit()]
            if pids:
                result["running"] = True
                result["pids"] = pids
                result["method"] = "node+claude"
                return result
    except Exception:
        pass

    return result


def check_launchd_jobs() -> dict:
    """他の helix launchd ジョブの状態確認."""
    jobs = {}
    try:
        proc = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            for line in proc.stdout.strip().split("\n"):
                if "com.helix." in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        label = parts[2]
                        pid = parts[0]
                        status = parts[1]
                        jobs[label] = {
                            "pid": pid if pid != "-" else None,
                            "status": int(status) if status != "-" else None,
                        }
    except Exception:
        pass
    return jobs


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def run_check() -> list[str]:
    ensure_dirs()
    write_heartbeat()
    state = load_state()
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()
    alerts = []

    # 1. Claude CLI プロセスチェック
    cli_status = is_claude_cli_running()
    state["cli_running"] = cli_status["running"]
    state["last_check"] = now_str

    if cli_status["running"]:
        state["last_cli_seen"] = now_str
        state["cli_pids"] = cli_status["pids"]
        state["cli_method"] = cli_status["method"]
        print(f"  [OK] Claude CLI 稼働中 (PID: {cli_status['pids']}, method: {cli_status['method']})")
    else:
        last_seen = state.get("last_cli_seen")
        if last_seen:
            try:
                last_dt = datetime.fromisoformat(last_seen)
                absence_min = (now - last_dt).total_seconds() / 60
                if absence_min >= CLI_ABSENCE_WARN_MIN:
                    if should_alert(state, "cli_absent"):
                        alerts.append(
                            f"[Mac Watchdog] Claude CLI が{int(absence_min)}分間停止中。\n"
                            f"最終検出: {last_seen[:19]}\n"
                            f"CEO Node の判断機能が停止しています。"
                        )
                        state.setdefault("last_alert_time", {})["cli_absent"] = now_str
                print(f"  [WARN] Claude CLI 不在 ({int(absence_min)}分)")
            except (ValueError, TypeError):
                print(f"  [NG] Claude CLI 不在 (経過時間不明)")
        else:
            print(f"  [INFO] Claude CLI 未検出 (初回)")

    # 2. supervisor_mac のハートビートチェック
    sup_hb_file = HEARTBEAT_DIR / "supervisor_mac.json"
    if sup_hb_file.exists():
        try:
            hb = json.loads(sup_hb_file.read_text(encoding="utf-8"))
            hb_time = datetime.fromisoformat(hb["timestamp"])
            age_min = (now - hb_time).total_seconds() / 60
            if age_min > 10:  # supervisor は 3分間隔なので 10分で異常
                print(f"  [WARN] supervisor_mac のハートビートが {int(age_min)}分前")
                if should_alert(state, "supervisor_stale"):
                    alerts.append(
                        f"[Mac Watchdog] supervisor_mac が{int(age_min)}分間応答なし。"
                    )
                    state.setdefault("last_alert_time", {})["supervisor_stale"] = now_str
            else:
                print(f"  [OK] supervisor_mac: {int(age_min)}分前")
        except Exception:
            print(f"  [WARN] supervisor_mac ハートビート読取失敗")
    else:
        print(f"  [INFO] supervisor_mac ハートビートファイルなし")

    # 3. launchd ジョブ状態
    jobs = check_launchd_jobs()
    if jobs:
        print(f"  [INFO] helix launchd ジョブ: {len(jobs)}件登録")
        failed = {k: v for k, v in jobs.items() if v.get("status") and v["status"] != 0}
        if failed:
            print(f"  [WARN] 異常終了ジョブ: {list(failed.keys())}")

    # アラート履歴記録
    for alert in alerts:
        state.setdefault("alert_history", []).append({
            "time": now_str,
            "message": alert[:200],
        })
    state["alert_history"] = state["alert_history"][-50:]

    save_state(state)
    return alerts


def show_status():
    ensure_dirs()
    state = load_state()
    print("=== Mac Watchdog Status ===")
    print(f"  最終チェック: {state.get('last_check', 'なし')}")
    print(f"  CLI稼働中: {'はい' if state.get('cli_running') else 'いいえ'}")
    print(f"  CLI最終検出: {state.get('last_cli_seen', 'なし')}")
    if state.get("cli_pids"):
        print(f"  CLI PID: {state['cli_pids']}")

    # launchd ジョブ
    jobs = check_launchd_jobs()
    if jobs:
        print(f"\n=== Helix LaunchAgent ジョブ ===")
        for label, info in sorted(jobs.items()):
            pid_str = info["pid"] or "-"
            status_str = f"exit={info['status']}" if info["status"] is not None else "OK"
            print(f"  {label}: pid={pid_str}, {status_str}")

    history = state.get("alert_history", [])
    if history:
        print(f"\n=== 直近アラート ({len(history)}件) ===")
        for h in history[-5:]:
            print(f"  [{h['time'][:19]}] {h['message'][:80]}")


def reset_alerts():
    state = load_state()
    state["last_alert_time"] = {}
    state["alert_history"] = []
    save_state(state)
    print("アラート状態をリセットしました。")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "status":
            show_status()
        elif cmd == "reset":
            reset_alerts()
        else:
            print(f"Usage: python3 watchdog_mac.py [status|reset]")
    else:
        print(f"[{now_iso()[:19]}] Mac Watchdog 実行開始")
        alerts = run_check()
        if alerts:
            for alert in alerts:
                print(f"ALERT: {alert}")
                sent = notify(alert)
                print(f"  -> Discord: {'送信成功' if sent else '送信失敗'}")
        else:
            print("OK - 異常なし")
