"""統合スーパーバイザー — 全デーモンの相互監視・自動復旧・記憶共有.

タスクスケジューラから3分間隔で起動。
全デーモンのハートビートを監視し、停止を検出したら自動再起動。
自身もハートビートを出すので、他のデーモンからも監視される。

アーキテクチャ:
    supervisor.py (3分) ─── 全プロセス監視・再起動
        ├── assistant_daemon.py (5分) ─── タスクキュー処理 (gemma4:e4b)
        ├── watchdog.py (5分) ─── Claude CLI監視
        ├── hw_monitor.py (5分) ─── ハードウェア監視
        └── x_monitor.py (30分) ─── X情報収集

共有記憶: Qdrant mem0_shared (全層で同一コレクション)
ハートビート: ~/.helix-agent/heartbeats/*.json

使い方:
    python scripts/supervisor.py              # 監視実行
    python scripts/supervisor.py status       # 全体ステータス
    python scripts/supervisor.py restart all  # 全デーモン再起動
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

HELIX_DIR = Path.home() / ".helix-agent"
HEARTBEAT_DIR = HELIX_DIR / "heartbeats"
SUPERVISOR_STATE = HELIX_DIR / "supervisor" / "state.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"
SCRIPTS_DIR = Path(__file__).resolve().parent

# 監視対象デーモン定義
DAEMONS = {
    "assistant_daemon": {
        "script": SCRIPTS_DIR / "assistant_daemon.py",
        "interval_min": 5,
        "stale_threshold_min": 12,   # 12分更新なしで異常判定
        "description": "タスクキュー処理",
        "critical": True,
    },
    "watchdog": {
        "script": SCRIPTS_DIR / "watchdog.py",
        "interval_min": 5,
        "stale_threshold_min": 12,
        "description": "CLI監視",
        "critical": True,
    },
    "hw_monitor": {
        "script": SCRIPTS_DIR / "hw_monitor.py",
        "interval_min": 5,
        "stale_threshold_min": 12,
        "description": "ハードウェア監視",
        "critical": True,
    },
    "usage_monitor": {
        "script": SCRIPTS_DIR / "usage_monitor.py",
        "interval_min": 15,
        "stale_threshold_min": 25,
        "description": "Claude使用量監視",
        "critical": True,
    },
    "x_monitor": {
        "script": SCRIPTS_DIR / "x_monitor.py",
        "interval_min": 30,
        "stale_threshold_min": 45,   # 30分間隔なので45分まで許容
        "description": "X情報収集",
        "critical": False,
    },
}

# gemma4 軽量モデル設定（常時運用用）
DAEMON_MODEL_PREFERENCE = ["gemma4:e4b", "gemma4:e2b"]  # 軽量優先
HEAVY_MODEL_PREFERENCE = ["gemma4:31b", "gemma4:26b"]    # 必要時のみ

# 再起動制限
MAX_RESTARTS_PER_HOUR = 3
ALERT_COOLDOWN_MIN = 15


# ---------------------------------------------------------------------------
# ハートビート
# ---------------------------------------------------------------------------

def write_heartbeat(daemon_name: str, extra: dict | None = None) -> None:
    """ハートビートを書き込み."""
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    hb = {
        "daemon": daemon_name,
        "pid": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "alive",
    }
    if extra:
        hb.update(extra)
    (HEARTBEAT_DIR / f"{daemon_name}.json").write_text(
        json.dumps(hb, ensure_ascii=False), encoding="utf-8"
    )


def read_heartbeat(daemon_name: str) -> dict | None:
    """ハートビートを読み取り."""
    hb_file = HEARTBEAT_DIR / f"{daemon_name}.json"
    if not hb_file.exists():
        return None
    try:
        return json.loads(hb_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def heartbeat_age_min(daemon_name: str) -> float | None:
    """ハートビートの経過時間(分)."""
    hb = read_heartbeat(daemon_name)
    if not hb or "timestamp" not in hb:
        return None
    try:
        ts = datetime.fromisoformat(hb["timestamp"])
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 状態管理
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if SUPERVISOR_STATE.exists():
        try:
            return json.loads(SUPERVISOR_STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "last_run": None,
        "restart_history": [],
        "last_alert": {},
        "total_restarts": 0,
    }


def save_state(state: dict) -> None:
    SUPERVISOR_STATE.parent.mkdir(parents=True, exist_ok=True)
    SUPERVISOR_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 再起動
# ---------------------------------------------------------------------------

def restart_daemon(daemon_name: str, config: dict, state: dict) -> bool:
    """デーモンを再起動（タスクスケジューラ経由で即実行）."""
    # 再起動制限チェック
    now = datetime.now(timezone.utc)
    recent_restarts = [
        r for r in state.get("restart_history", [])
        if r.get("daemon") == daemon_name
    ]
    hour_ago = now.timestamp() - 3600
    recent_count = sum(
        1 for r in recent_restarts
        if datetime.fromisoformat(r["time"]).timestamp() > hour_ago
    )
    if recent_count >= MAX_RESTARTS_PER_HOUR:
        return False

    script = config["script"]
    if not script.exists():
        return False

    try:
        # サブプロセスとして直接起動（バックグラウンド）
        subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(script.parent.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        # 再起動履歴記録
        state.setdefault("restart_history", []).append({
            "daemon": daemon_name,
            "time": now.isoformat(),
            "reason": "heartbeat_stale",
        })
        # 履歴は最新100件まで
        state["restart_history"] = state["restart_history"][-100:]
        state["total_restarts"] = state.get("total_restarts", 0) + 1
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Discord通知
# ---------------------------------------------------------------------------

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


def should_alert(state: dict, key: str) -> bool:
    last = state.get("last_alert", {}).get(key)
    if not last:
        return True
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
        return elapsed >= ALERT_COOLDOWN_MIN
    except (ValueError, TypeError):
        return True


# ---------------------------------------------------------------------------
# VRAM最適化: 軽量モデル選択
# ---------------------------------------------------------------------------

def get_optimal_daemon_model() -> str | None:
    """常時運用に適した軽量モデルを選択."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            free_mb = max(float(x.strip()) for x in result.stdout.strip().split("\n"))
            # 軽量モデル優先（常時運用はVRAM節約が重要）
            model_vram = {"gemma4:e4b": 6000, "gemma4:e2b": 4000}
            for model in DAEMON_MODEL_PREFERENCE:
                if model_vram.get(model, 99999) < free_mb * 0.7:  # 70%以下で余裕を持つ
                    return model
    except Exception:
        pass
    return "gemma4:e2b"  # 最小フォールバック


def update_daemon_model_config() -> None:
    """assistant_daemonが使うモデル設定を更新."""
    model = get_optimal_daemon_model()
    config_file = HELIX_DIR / "assistant" / "model_config.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps({
        "daemon_model": model,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "note": "supervisor自動選択。常時運用は軽量モデル優先。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Qdrant共有記憶: イベント記録
# ---------------------------------------------------------------------------

def record_event_to_memory(event_type: str, message: str) -> None:
    """重要イベントをQdrant共有記憶に記録."""
    try:
        import urllib.request
        data = json.dumps({
            "text": f"[supervisor/{event_type}] {message}",
            "user_id": "tsunamayo7",
            "metadata": {
                "source": "supervisor",
                "event_type": event_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://localhost:8080/memory/add",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # 記憶サーバーが落ちていても続行


# ---------------------------------------------------------------------------
# メイン監視
# ---------------------------------------------------------------------------

def run_supervision() -> dict:
    """全デーモンの監視・復旧を実行."""
    state = load_state()
    now = datetime.now(timezone.utc)
    results = {"checked": 0, "healthy": 0, "restarted": 0, "alerts": []}

    # 自身のハートビート
    write_heartbeat("supervisor", {"model_config": get_optimal_daemon_model()})

    # モデル設定更新
    update_daemon_model_config()

    for name, config in DAEMONS.items():
        results["checked"] += 1
        age = heartbeat_age_min(name)

        if age is None:
            # ハートビートなし = 一度も実行されていない or ファイル消失
            msg = f"⚠️ **Supervisor**: {name} ({config['description']}) のハートビートがありません。再起動を試行。"
            results["alerts"].append(msg)
            restarted = restart_daemon(name, config, state)
            if restarted:
                results["restarted"] += 1
                record_event_to_memory("restart", f"{name} を再起動（ハートビートなし）")

        elif age > config["stale_threshold_min"]:
            # ハートビートが古い = 停止の可能性
            msg = (f"⚠️ **Supervisor**: {name} ({config['description']}) が"
                   f"{int(age)}分間応答なし。再起動を試行。")
            results["alerts"].append(msg)
            restarted = restart_daemon(name, config, state)
            if restarted:
                results["restarted"] += 1
                record_event_to_memory("restart", f"{name} を再起動（{int(age)}分間応答なし）")
        else:
            results["healthy"] += 1

    # 通知
    if results["alerts"] and should_alert(state, "supervision"):
        combined = "\n".join(results["alerts"])
        notify(combined)
        state.setdefault("last_alert", {})["supervision"] = now.isoformat()

    # 全員健康な場合は記録のみ
    state["last_run"] = now.isoformat()
    save_state(state)

    return results


def show_status():
    """全体ステータスを表示."""
    state = load_state()
    print("=== Supervisor Status ===")
    print(f"  最終実行: {state.get('last_run', 'なし')}")
    print(f"  総再起動回数: {state.get('total_restarts', 0)}")

    print(f"\n=== デーモン状態 ===")
    # supervisor自身
    sup_age = heartbeat_age_min("supervisor")
    if sup_age is not None:
        print(f"  supervisor: {int(sup_age)}分前 [OK]")
    else:
        print(f"  supervisor: ハートビートなし [NG]")

    for name, config in DAEMONS.items():
        age = heartbeat_age_min(name)
        if age is None:
            status = "[NG] ハートビートなし"
        elif age > config["stale_threshold_min"]:
            status = f"⚠️ {int(age)}分前（閾値{config['stale_threshold_min']}分超過）"
        else:
            status = f"[OK] {int(age)}分前"
        print(f"  {name} ({config['description']}): {status}")

    # モデル設定
    model_config = HELIX_DIR / "assistant" / "model_config.json"
    if model_config.exists():
        try:
            mc = json.loads(model_config.read_text(encoding="utf-8"))
            print(f"\n=== 常時運用モデル ===")
            print(f"  デーモン用: {mc.get('daemon_model', '不明')}")
            print(f"  更新: {mc.get('updated_at', '不明')[:19]}")
        except (json.JSONDecodeError, OSError):
            pass

    # 直近の再起動履歴
    history = state.get("restart_history", [])
    if history:
        print(f"\n=== 直近の再起動 ({len(history)}件) ===")
        for h in history[-5:]:
            print(f"  [{h['time'][:19]}] {h['daemon']} ({h.get('reason', '?')})")

    # 共有記憶状態
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:8080/health", timeout=3)
        if resp.status == 200:
            print(f"\n=== 共有記憶 (Qdrant) ===")
            print(f"  ステータス: [OK] 接続OK")
        else:
            print(f"\n  共有記憶: ⚠️ 応答異常")
    except Exception:
        print(f"\n=== 共有記憶 (Qdrant) ===")
        print(f"  ステータス: [NG] 接続不可")


def restart_all():
    """全デーモンを再起動."""
    state = load_state()
    for name, config in DAEMONS.items():
        print(f"  再起動: {name}...", end=" ")
        if restart_daemon(name, config, state):
            print("[OK]")
        else:
            print("[NG]")
    save_state(state)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif len(sys.argv) > 1 and sys.argv[1] == "restart":
        target = sys.argv[2] if len(sys.argv) > 2 else "all"
        if target == "all":
            restart_all()
        elif target in DAEMONS:
            state = load_state()
            if restart_daemon(target, DAEMONS[target], state):
                save_state(state)
                print(f"{target} を再起動しました。")
            else:
                print(f"{target} の再起動に失敗。")
        else:
            print(f"不明なデーモン: {target}")
    else:
        results = run_supervision()
        print(f"監視完了: {results['checked']}件チェック / "
              f"{results['healthy']}件正常 / {results['restarted']}件再起動")
