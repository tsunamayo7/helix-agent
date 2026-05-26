"""Mac CMEM Bridge — 操作ログ -> Qdrant 同期 (スタブ).

5分毎に launchd から起動。
将来的には $CMEM (claude-mem) の操作ログを
リモート Qdrant (tsunamayo-1:6333) に同期する。

現在はスタブ実装:
  - $CMEM DB の存在と基本統計を確認
  - Qdrant 接続性を確認
  - ハートビートを記録

使い方:
    python3 scripts/cmem_bridge_mac.py           # 同期実行 (スタブ)
    python3 scripts/cmem_bridge_mac.py status    # 状態表示
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

HELIX_DIR = Path.home() / ".helix-agent"
HEARTBEAT_DIR = HELIX_DIR / "heartbeats"
STATE_FILE = HELIX_DIR / "cmem_bridge_mac" / "state.json"
LOG_DIR = Path.home() / ".claude" / "logs"

CMEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
QDRANT_URL = os.environ.get("QDRANT_URL", "http://tsunamayo-1:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
COLLECTION = "mem0_shared"


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs():
    for d in [HEARTBEAT_DIR, STATE_FILE.parent, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def write_heartbeat():
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    hb = {
        "daemon": "cmem_bridge_mac",
        "pid": os.getpid(),
        "timestamp": now_iso(),
        "status": "alive",
    }
    (HEARTBEAT_DIR / "cmem_bridge_mac.json").write_text(
        json.dumps(hb, ensure_ascii=False), encoding="utf-8",
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_run": None, "cmem_count": 0, "qdrant_reachable": False, "sync_count": 0}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = now_iso()
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# チェック関数
# ---------------------------------------------------------------------------

def check_cmem() -> dict:
    """$CMEM DB の基本統計を取得."""
    result = {"exists": False, "observation_count": 0, "db_size_mb": 0}

    if not CMEM_DB.exists():
        return result

    result["exists"] = True
    result["db_size_mb"] = round(CMEM_DB.stat().st_size / 1024 / 1024, 1)

    try:
        conn = sqlite3.connect(str(CMEM_DB), timeout=5)
        try:
            count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
            result["observation_count"] = count[0] if count else 0
        except sqlite3.OperationalError:
            pass
        conn.close()
    except Exception:
        pass

    return result


def check_qdrant() -> dict:
    """リモート Qdrant の接続性とコレクション情報を確認."""
    result = {"reachable": False, "points_count": 0}

    headers = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY

    try:
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{COLLECTION}",
            headers=headers,
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        info = data.get("result", {})

        result["reachable"] = True
        result["points_count"] = info.get("points_count", 0)
        result["status"] = info.get("status", "unknown")
    except Exception as e:
        result["error"] = str(e)[:100]

    return result


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def run_sync():
    """同期実行 (現在はスタブ: 統計収集とハートビートのみ)."""
    ensure_dirs()
    write_heartbeat()
    state = load_state()

    print(f"[{now_iso()[:19]}] CMEM Bridge Mac 実行開始")

    # 1. $CMEM 確認
    cmem = check_cmem()
    state["cmem_exists"] = cmem["exists"]
    state["cmem_count"] = cmem["observation_count"]
    state["cmem_size_mb"] = cmem["db_size_mb"]

    if cmem["exists"]:
        print(f"  [OK] $CMEM: {cmem['observation_count']} observations, {cmem['db_size_mb']} MB")
    else:
        print(f"  [INFO] $CMEM DB なし ({CMEM_DB})")

    # 2. Qdrant 確認
    qdrant = check_qdrant()
    state["qdrant_reachable"] = qdrant["reachable"]
    state["qdrant_points"] = qdrant["points_count"]

    if qdrant["reachable"]:
        print(f"  [OK] Qdrant: {qdrant['points_count']} points ({qdrant.get('status', '?')})")
    else:
        print(f"  [NG] Qdrant 接続失敗: {qdrant.get('error', 'unknown')}")

    # 3. スタブ: 同期は未実装
    # TODO: $CMEM の新規 observations を Qdrant に同期
    # - type=feature/bugfix/discovery を対象
    # - content_hash で重複排除
    # - Ollama embedding で埋め込み生成
    print(f"  [STUB] 同期ロジック未実装 - 統計収集のみ")

    save_state(state)
    print(f"完了")


def show_status():
    state = load_state()
    print("=== CMEM Bridge Mac Status ===")
    print(f"  最終実行: {state.get('last_run', 'なし')}")
    print(f"  $CMEM 存在: {'はい' if state.get('cmem_exists') else 'いいえ'}")
    print(f"  $CMEM observations: {state.get('cmem_count', 0)}")
    print(f"  Qdrant 到達: {'はい' if state.get('qdrant_reachable') else 'いいえ'}")
    print(f"  Qdrant points: {state.get('qdrant_points', 0)}")
    print(f"  同期回数: {state.get('sync_count', 0)}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    else:
        run_sync()
