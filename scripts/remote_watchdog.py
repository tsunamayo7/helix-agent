"""リモートウォッチドッグ — Raspberry Pi / サブPCで実行.

メインPCの死活監視を行い、停止検出時にフェイルオーバーを実行。
このスクリプトはRaspberry PiまたはサブPCにコピーして使う。

動作:
  1. メインPCの /health エンドポイントを定期ポーリング
  2. 応答なし/異常を検出 → Discord通知
  3. サブPCの場合: ローカルデーモン群を起動（フェイルオーバー）
  4. Pi の場合: Sonnet API で最低限のタスク処理
  5. メインPC復旧を検出 → フェイルバック

使い方 (Raspberry Pi):
    python remote_watchdog.py --role pi --main-pc 192.168.x.x:8800

使い方 (サブPC):
    python remote_watchdog.py --role sub-pc --main-pc 192.168.x.x:8800

使い方 (1回チェック / cron用):
    python remote_watchdog.py --role pi --main-pc 192.168.x.x:8800 --once
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

CHECK_INTERVAL_SEC = 60          # チェック間隔
FAILURE_THRESHOLD = 3            # 連続N回失敗でフェイルオーバー
RECOVERY_THRESHOLD = 3           # 連続N回成功でフェイルバック
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

STATE_DIR = Path.home() / ".helix-agent" / "remote_watchdog"
STATE_FILE = STATE_DIR / "state.json"


# ---------------------------------------------------------------------------
# 状態管理
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "consecutive_failures": 0,
        "consecutive_successes": 0,
        "failover_active": False,
        "last_check": None,
        "last_failover": None,
        "last_failback": None,
    }


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# リモートチェック
# ---------------------------------------------------------------------------

def check_main_pc(address: str) -> dict | None:
    """メインPCのヘルスチェック."""
    try:
        url = f"http://{address}/health"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Discord通知
# ---------------------------------------------------------------------------

def notify_discord(message: str) -> bool:
    """Discord Webhook通知."""
    # 環境変数のWebhook URL
    webhook_url = WEBHOOK_URL
    if not webhook_url:
        # fallbackスクリプト
        fallback = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"
        if fallback.exists():
            try:
                result = subprocess.run(
                    [sys.executable, str(fallback), message],
                    capture_output=True, text=True, timeout=30,
                )
                return result.returncode == 0
            except Exception:
                pass
        return False

    try:
        data = json.dumps({
            "content": message,
            "username": "Remote Watchdog",
        }).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# フェイルオーバー / フェイルバック
# ---------------------------------------------------------------------------

def failover_sub_pc():
    """サブPCでデーモン群を起動."""
    scripts_dir = Path(__file__).resolve().parent
    daemons = ["assistant_daemon.py", "watchdog.py", "hw_monitor.py"]

    for daemon in daemons:
        script = scripts_dir / daemon
        if script.exists():
            try:
                subprocess.Popen(
                    [sys.executable, str(script)],
                    cwd=str(scripts_dir.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass


def failover_pi(task_description: str | None = None):
    """Raspberry Piでの最低限処理（Sonnet API）."""
    if not task_description:
        return
    # Anthropic API呼び出し（環境変数 ANTHROPIC_API_KEY が必要）
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return

    try:
        data = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": task_description}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read().decode("utf-8"))
        return result.get("content", [{}])[0].get("text", "")
    except Exception:
        return None


def failback():
    """フェイルバック（メインPC復旧）."""
    # サブPCの場合はデーモンを停止する必要があるが、
    # タスクスケジューラ管理なので自然に停止する
    pass


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------

def run_once(address: str, role: str) -> dict:
    """1回のチェックを実行."""
    state = load_state()
    now = datetime.now(timezone.utc)
    state["last_check"] = now.isoformat()

    health = check_main_pc(address)

    if health is None or health.get("status") == "error":
        # メインPC応答なし
        state["consecutive_failures"] += 1
        state["consecutive_successes"] = 0

        if state["consecutive_failures"] >= FAILURE_THRESHOLD and not state["failover_active"]:
            # フェイルオーバー発動
            state["failover_active"] = True
            state["last_failover"] = now.isoformat()

            msg = (f"🔴 **Remote Watchdog [{role}]**: メインPC応答なし "
                   f"({state['consecutive_failures']}回連続)。フェイルオーバー発動。")
            notify_discord(msg)

            if role == "sub-pc":
                failover_sub_pc()
            # Pi の場合は通知のみ（タスクがあれば処理）

        elif state["consecutive_failures"] < FAILURE_THRESHOLD:
            pass  # まだ閾値未到達

    else:
        # メインPC正常
        state["consecutive_successes"] += 1
        state["consecutive_failures"] = 0

        if state["failover_active"] and state["consecutive_successes"] >= RECOVERY_THRESHOLD:
            # フェイルバック
            state["failover_active"] = False
            state["last_failback"] = now.isoformat()

            msg = (f"🟢 **Remote Watchdog [{role}]**: メインPC復旧検出。フェイルバック完了。"
                   f"\nステータス: {health.get('status')} / "
                   f"デーモン: {health.get('daemons_alive')}/{health.get('daemons_total')}")
            notify_discord(msg)
            failback()

    save_state(state)
    return {
        "main_pc": "up" if health else "down",
        "failover_active": state["failover_active"],
        "consecutive_failures": state["consecutive_failures"],
    }


def run_loop(address: str, role: str):
    """継続監視ループ."""
    print(f"Remote Watchdog 起動 (role={role}, main-pc={address})")
    while True:
        try:
            result = run_once(address, role)
            status = "UP" if result["main_pc"] == "up" else "DOWN"
            fo = " [FAILOVER ACTIVE]" if result["failover_active"] else ""
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Main: {status}{fo}")
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(CHECK_INTERVAL_SEC)


def show_status():
    """状態表示."""
    state = load_state()
    print("=== Remote Watchdog ===")
    print(f"  最終チェック: {state.get('last_check', 'なし')}")
    print(f"  フェイルオーバー中: {'はい' if state.get('failover_active') else 'いいえ'}")
    print(f"  連続失敗: {state.get('consecutive_failures', 0)}")
    print(f"  連続成功: {state.get('consecutive_successes', 0)}")
    if state.get("last_failover"):
        print(f"  最終フェイルオーバー: {state['last_failover'][:19]}")
    if state.get("last_failback"):
        print(f"  最終フェイルバック: {state['last_failback'][:19]}")


def main():
    parser = argparse.ArgumentParser(description="リモートウォッチドッグ")
    parser.add_argument("--role", choices=["sub-pc", "pi", "mac"], required=True)
    parser.add_argument("--main-pc", type=str, required=True, help="メインPCのアドレス (host:port)")
    parser.add_argument("--once", action="store_true", help="1回だけチェックして終了")
    parser.add_argument("--status", action="store_true", help="状態表示")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.once:
        result = run_once(args.main_pc, args.role)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        run_loop(args.main_pc, args.role)


if __name__ == "__main__":
    main()
