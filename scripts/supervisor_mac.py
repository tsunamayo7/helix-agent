"""Mac Supervisor — リモートサービス監視 (CEO Node).

3分毎に launchd から起動。
リモート GPU Server 上の Qdrant / Ollama / Health Server の
到達性をチェックし、失敗時はログ記録 + Discord 通知。

Mac はサービスを再起動する権限を持たない (Windows 側が自律起動)。
CEO Node の責務は「検知 + 記録 + 通知」。

使い方:
    python3 scripts/supervisor_mac.py           # 監視実行
    python3 scripts/supervisor_mac.py status    # 全体ステータス
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

HELIX_DIR = Path.home() / ".helix-agent"
HEARTBEAT_DIR = HELIX_DIR / "heartbeats"
STATE_FILE = HELIX_DIR / "supervisor_mac" / "state.json"
LOG_DIR = Path.home() / ".claude" / "logs"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# 環境変数 (plist で定義)
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
OLLAMA_HOST = os.environ.get("HELIX_OLLAMA_HOST", "http://localhost:11434")
HEALTH_URL = os.environ.get("HELIX_HEALTH_URL", "http://localhost:8800")

# Mac ローカル Ollama
LOCAL_OLLAMA = "http://localhost:11434"

# 監視対象リモートサービス
REMOTE_SERVICES = {
    "qdrant": {
        "url": f"{QDRANT_URL}/collections",
        "description": "Qdrant Vector DB",
        "critical": True,
        "timeout": 5,
    },
    "ollama_remote": {
        "url": f"{OLLAMA_HOST}/api/tags",
        "description": "Ollama Remote",
        "critical": True,
        "timeout": 5,
    },
    "health_server": {
        "url": f"{HEALTH_URL}/health",
        "description": "Health Server",
        "critical": False,
        "timeout": 5,
    },
    "qdrant_memory": {
        "url": os.environ.get("HELIX_QDRANT_MEMORY_URL", "http://localhost:8080") + "/health",
        "description": "Qdrant Memory HTTP API",
        "critical": False,
        "timeout": 5,
    },
    "translate": {
        "url": os.environ.get("HELIX_TRANSLATE_URL", "http://localhost:8787") + "/health",
        "description": "Translate API",
        "critical": False,
        "timeout": 5,
    },
}

# Mac ローカルサービス
LOCAL_SERVICES = {
    "ollama_local": {
        "url": f"{LOCAL_OLLAMA}/api/tags",
        "description": "Ollama Local (localhost:11434)",
        "critical": False,
        "timeout": 3,
    },
}

ALERT_COOLDOWN_MIN = 15
MAX_HISTORY = 200


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs():
    """必要なディレクトリを作成."""
    for d in [HEARTBEAT_DIR, STATE_FILE.parent, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def write_heartbeat(name: str, extra: dict | None = None):
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    hb = {
        "daemon": name,
        "pid": os.getpid(),
        "timestamp": now_iso(),
        "status": "alive",
    }
    if extra:
        hb.update(extra)
    (HEARTBEAT_DIR / f"{name}.json").write_text(
        json.dumps(hb, ensure_ascii=False), encoding="utf-8",
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "last_run": None,
        "consecutive_failures": {},
        "alert_history": [],
        "last_alert": {},
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = now_iso()
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def should_alert(state: dict, key: str) -> bool:
    last = state.get("last_alert", {}).get(key)
    if not last:
        return True
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
        return elapsed >= ALERT_COOLDOWN_MIN
    except (ValueError, TypeError):
        return True


def notify(message: str) -> bool:
    if not WEBHOOK_SCRIPT.exists():
        print(f"  [notify] webhook script not found: {WEBHOOK_SCRIPT}")
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(WEBHOOK_SCRIPT), message],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"  [notify] failed: {e}")
        return False


# ---------------------------------------------------------------------------
# ヘルスチェック
# ---------------------------------------------------------------------------

def check_service(name: str, config: dict) -> dict:
    """サービスの到達性チェック."""
    result = {"name": name, "description": config["description"], "status": "unknown"}
    url = config["url"]
    timeout = config.get("timeout", 5)

    # Qdrant は API key ヘッダーが必要
    headers = {}
    if "qdrant" in name and QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY

    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=timeout)
        if resp.status == 200:
            result["status"] = "ok"
            # Qdrant の場合はコレクション情報を取得
            if name == "qdrant":
                try:
                    data = json.loads(resp.read().decode("utf-8"))
                    collections = data.get("result", {}).get("collections", [])
                    result["collections"] = len(collections)
                except Exception:
                    pass
            # Ollama の場合はモデル数を取得
            elif "ollama" in name:
                try:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = data.get("models", [])
                    result["model_count"] = len(models)
                except Exception:
                    pass
        else:
            result["status"] = "error"
            result["http_status"] = resp.status
    except urllib.error.URLError as e:
        result["status"] = "unreachable"
        result["error"] = str(e.reason)[:100]
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:100]

    return result


# ---------------------------------------------------------------------------
# メイン監視
# ---------------------------------------------------------------------------

def run_supervision() -> dict:
    ensure_dirs()
    state = load_state()
    write_heartbeat("supervisor_mac")

    results = {"checked": 0, "healthy": 0, "failed": 0, "alerts": []}

    all_services = {**REMOTE_SERVICES, **LOCAL_SERVICES}

    for name, config in all_services.items():
        results["checked"] += 1
        check = check_service(name, config)

        if check["status"] == "ok":
            results["healthy"] += 1
            state["consecutive_failures"][name] = 0
            print(f"  [OK] {config['description']}")
        else:
            results["failed"] += 1
            prev_fails = state.get("consecutive_failures", {}).get(name, 0)
            state.setdefault("consecutive_failures", {})[name] = prev_fails + 1
            err = check.get("error", check["status"])
            print(f"  [NG] {config['description']} - {err}")

            # 2回連続失敗でアラート (一時的なネットワーク揺れを除外)
            if prev_fails + 1 >= 2 and config.get("critical"):
                if should_alert(state, f"svc_{name}"):
                    msg = (
                        f"[Mac Supervisor] {config['description']} が"
                        f"{prev_fails + 1}回連続で応答なし。\n"
                        f"エラー: {err}"
                    )
                    results["alerts"].append(msg)
                    state.setdefault("last_alert", {})[f"svc_{name}"] = now_iso()

    # アラート送信
    for alert in results["alerts"]:
        notify(alert)

    # 履歴記録
    state.setdefault("alert_history", []).extend([
        {"time": now_iso(), "message": a[:200]} for a in results["alerts"]
    ])
    state["alert_history"] = state["alert_history"][-MAX_HISTORY:]

    save_state(state)
    return results


def show_status():
    ensure_dirs()
    state = load_state()
    print("=== Mac Supervisor Status ===")
    print(f"  最終実行: {state.get('last_run', 'なし')}")

    # ハートビート
    hb_file = HEARTBEAT_DIR / "supervisor_mac.json"
    if hb_file.exists():
        try:
            hb = json.loads(hb_file.read_text(encoding="utf-8"))
            print(f"  ハートビート: {hb.get('timestamp', 'なし')[:19]}")
        except Exception:
            pass

    # 現在のサービス状態をライブチェック
    print("\n=== リモートサービス ===")
    for name, config in REMOTE_SERVICES.items():
        check = check_service(name, config)
        status = "[OK]" if check["status"] == "ok" else f"[NG] {check.get('error', check['status'])}"
        extra = ""
        if "collections" in check:
            extra = f" ({check['collections']} collections)"
        elif "model_count" in check:
            extra = f" ({check['model_count']} models)"
        print(f"  {config['description']}: {status}{extra}")

    print("\n=== ローカルサービス (Mac) ===")
    for name, config in LOCAL_SERVICES.items():
        check = check_service(name, config)
        status = "[OK]" if check["status"] == "ok" else f"[NG] {check.get('error', check['status'])}"
        extra = ""
        if "model_count" in check:
            extra = f" ({check['model_count']} models)"
        print(f"  {config['description']}: {status}{extra}")

    # 連続失敗カウント
    failures = {k: v for k, v in state.get("consecutive_failures", {}).items() if v > 0}
    if failures:
        print(f"\n=== 連続失敗中 ===")
        for name, count in failures.items():
            print(f"  {name}: {count}回連続")

    # 直近アラート
    history = state.get("alert_history", [])
    if history:
        print(f"\n=== 直近アラート ({len(history)}件) ===")
        for h in history[-5:]:
            print(f"  [{h['time'][:19]}] {h['message'][:80]}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    else:
        print(f"[{now_iso()[:19]}] Mac Supervisor 実行開始")
        results = run_supervision()
        print(
            f"監視完了: {results['checked']}件チェック / "
            f"{results['healthy']}件正常 / {results['failed']}件失敗"
        )
