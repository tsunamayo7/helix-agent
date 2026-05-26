"""Mac Integrity Check — memory/ ファイル整合性 + Qdrant 健全性検証.

毎日 3:15 に launchd から起動。
Mac CEO Node 上の memory/ ディレクトリと、
リモート Qdrant (tsunamayo-1) の整合性を検証。

Windows 版 (integrity_check.py) との差分:
  - memory/ パスが macOS パス
  - Qdrant はリモート (tsunamayo-1:6333)
  - LightRAG チェックは省略 (Mac 未搭載)
  - $CMEM は Mac 上に存在すれば検証

使い方:
    python3 scripts/integrity_check_mac.py           # 全チェック
    python3 scripts/integrity_check_mac.py --quick   # 軽量チェック
    python3 scripts/integrity_check_mac.py status    # 前回結果表示
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# Mac 上の memory/ パス
MEMORY_DIR = Path.home() / ".claude" / "projects" / "Development" / "memory"
CMEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"

# リモート Qdrant (環境変数 or デフォルト)
QDRANT_URL = os.environ.get("QDRANT_URL", "http://tsunamayo-1:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
COLLECTION = "mem0_shared"

STATE_DIR = Path.home() / ".helix-agent" / "integrity_mac"
STATE_FILE = STATE_DIR / "state.json"
LOG_DIR = Path.home() / ".claude" / "logs"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# 閾値
FILE_DROP_THRESHOLD = 0.8
QDRANT_DROP_THRESHOLD = 0.9
CMEM_DROP_THRESHOLD = 0.9


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs():
    for d in [STATE_DIR, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict):
    ensure_dirs()
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def send_alert(message: str) -> bool:
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


# ---------------------------------------------------------------------------
# チェック関数
# ---------------------------------------------------------------------------

def check_memory_files() -> dict:
    """memory/ ディレクトリのファイル数と整合性チェック."""
    result = {"status": "ok", "details": {}}

    if not MEMORY_DIR.exists():
        return {"status": "error", "details": {"message": f"memory/ が存在しない: {MEMORY_DIR}"}}

    md_files = list(MEMORY_DIR.glob("*.md"))
    result["details"]["file_count"] = len(md_files)
    result["details"]["path"] = str(MEMORY_DIR)

    # MEMORY.md 存在チェック
    memory_index = MEMORY_DIR / "MEMORY.md"
    result["details"]["index_exists"] = memory_index.exists()
    if not memory_index.exists():
        result["status"] = "warning"
        result["details"]["message"] = "MEMORY.md が存在しない"

    # 空ファイルチェック
    empty_files = [f.name for f in md_files if f.stat().st_size == 0]
    if empty_files:
        result["details"]["empty_files"] = empty_files
        result["status"] = "warning"

    # 合計サイズ
    total_size = sum(f.stat().st_size for f in md_files)
    result["details"]["total_size_kb"] = round(total_size / 1024, 1)

    # archive/ サブディレクトリ
    archive_dir = MEMORY_DIR / "archive"
    if archive_dir.exists():
        archive_count = len(list(archive_dir.glob("*.md")))
        result["details"]["archive_count"] = archive_count

    return result


def check_qdrant() -> dict:
    """リモート Qdrant コレクションの健全性チェック."""
    result = {"status": "ok", "details": {}}

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

        point_count = info.get("points_count", 0)
        status = info.get("status", "unknown")

        result["details"]["points_count"] = point_count
        result["details"]["collection_status"] = status
        result["details"]["qdrant_url"] = QDRANT_URL

        if status != "green":
            result["status"] = "warning"
            result["details"]["message"] = f"コレクション状態: {status}"

        if point_count == 0:
            result["status"] = "error"
            result["details"]["message"] = "Qdrant ポイント数が 0"

    except Exception as e:
        result["status"] = "error"
        result["details"]["message"] = f"Qdrant 接続失敗 ({QDRANT_URL}): {e}"

    return result


def check_cmem() -> dict:
    """$CMEM SQLite データベースの整合性チェック."""
    result = {"status": "ok", "details": {}}

    if not CMEM_DB.exists():
        return {"status": "info", "details": {"message": f"$CMEM DB なし: {CMEM_DB}"}}

    try:
        conn = sqlite3.connect(str(CMEM_DB), timeout=5)

        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        result["details"]["integrity"] = integrity[0] if integrity else "unknown"
        if integrity and integrity[0] != "ok":
            result["status"] = "error"
            result["details"]["message"] = f"SQLite 整合性エラー: {integrity[0]}"

        try:
            count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
            result["details"]["observation_count"] = count[0] if count else 0
        except sqlite3.OperationalError:
            result["details"]["observation_count"] = "table not found"

        result["details"]["db_size_mb"] = round(CMEM_DB.stat().st_size / 1024 / 1024, 1)
        conn.close()
    except Exception as e:
        result["status"] = "error"
        result["details"]["message"] = f"$CMEM 接続失敗: {e}"

    return result


def check_critical_files() -> dict:
    """重要ファイルの存在と SHA-256 確認."""
    result = {"status": "ok", "details": {}}
    critical = [
        Path.home() / ".claude" / "CLAUDE.md",
        Path.home() / ".claude" / "settings.json",
        MEMORY_DIR / "MEMORY.md",
        MEMORY_DIR / "SESSIONS_INDEX.md",
    ]

    missing = []
    empty = []
    checksums = {}

    for f in critical:
        if not f.exists():
            missing.append(str(f))
        elif f.stat().st_size == 0:
            empty.append(str(f))
        else:
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            checksums[f.name] = h[:16]

    result["details"]["checksums"] = checksums
    if missing:
        result["status"] = "error"
        result["details"]["missing"] = missing
    if empty:
        result["status"] = "warning"
        result["details"]["empty"] = empty

    return result


# ---------------------------------------------------------------------------
# 差分検出
# ---------------------------------------------------------------------------

def detect_anomalies(current: dict, prev: dict) -> list[str]:
    anomalies = []

    prev_count = prev.get("memory_file_count", 0)
    curr_count = current.get("memory", {}).get("details", {}).get("file_count", 0)
    if prev_count > 0 and curr_count < prev_count * FILE_DROP_THRESHOLD:
        anomalies.append(
            f"memory/ ファイル数急減: {prev_count} -> {curr_count} "
            f"({(1 - curr_count / prev_count) * 100:.0f}%減少)"
        )

    prev_qdrant = prev.get("qdrant_point_count", 0)
    curr_qdrant = current.get("qdrant", {}).get("details", {}).get("points_count", 0)
    if prev_qdrant > 0 and curr_qdrant < prev_qdrant * QDRANT_DROP_THRESHOLD:
        anomalies.append(
            f"Qdrant ポイント数急減: {prev_qdrant} -> {curr_qdrant}"
        )

    prev_cmem = prev.get("cmem_observation_count", 0)
    curr_cmem = current.get("cmem", {}).get("details", {}).get("observation_count", 0)
    if isinstance(curr_cmem, int) and prev_cmem > 0 and curr_cmem < prev_cmem * CMEM_DROP_THRESHOLD:
        anomalies.append(
            f"$CMEM observation 数急減: {prev_cmem} -> {curr_cmem}"
        )

    return anomalies


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def run_check(quick: bool = False) -> dict:
    ensure_dirs()
    prev_state = load_state()
    results = {}
    alerts = []

    print("=== Mac Integrity Check ===")
    print()

    print("[1/4] memory/ ファイル検証...")
    results["memory"] = check_memory_files()
    mc = results["memory"]["details"].get("file_count", "?")
    ts = results["memory"]["details"].get("total_size_kb", "?")
    print(f"  ファイル数: {mc}, 合計: {ts} KB, 状態: {results['memory']['status']}")

    print("[2/4] Qdrant 検証 (リモート)...")
    results["qdrant"] = check_qdrant()
    qc = results["qdrant"]["details"].get("points_count", "?")
    print(f"  ポイント数: {qc}, 状態: {results['qdrant']['status']}")

    print("[3/4] $CMEM DB 検証...")
    results["cmem"] = check_cmem()
    cc = results["cmem"]["details"].get("observation_count", "?")
    ci = results["cmem"]["details"].get("integrity", "N/A")
    print(f"  observation 数: {cc}, integrity: {ci}, 状態: {results['cmem']['status']}")

    if not quick:
        print("[4/4] 重要ファイル検証...")
        results["critical"] = check_critical_files()
        ck = results["critical"]["details"].get("checksums", {})
        print(f"  チェックサム: {len(ck)}件, 状態: {results['critical']['status']}")
    else:
        print("[4/4] 重要ファイル... スキップ (--quick)")

    anomalies = detect_anomalies(results, prev_state)
    if anomalies:
        print()
        print("*** 異常検出:")
        for a in anomalies:
            print(f"  - {a}")
            alerts.append(a)

    errors = [k for k, v in results.items() if v.get("status") == "error"]
    warnings = [k for k, v in results.items() if v.get("status") == "warning"]

    if errors:
        alerts.append(f"エラー検出: {', '.join(errors)}")
    if warnings and not errors:
        for w in warnings:
            msg = results[w].get("details", {}).get("message", "")
            if msg:
                alerts.append(f"{w}: {msg}")

    new_state = {
        "last_check": now_iso(),
        "memory_file_count": results.get("memory", {}).get("details", {}).get("file_count", 0),
        "qdrant_point_count": results.get("qdrant", {}).get("details", {}).get("points_count", 0),
        "cmem_observation_count": results.get("cmem", {}).get("details", {}).get("observation_count", 0),
        "results": {k: v["status"] for k, v in results.items()},
        "anomalies": anomalies,
    }
    save_state(new_state)

    if alerts:
        msg = "[Mac Integrity Check]\n" + "\n".join(f"- {a}" for a in alerts)
        send_alert(msg)

    print()
    overall = "ERROR" if errors else ("WARNING" if warnings else "OK")
    print(f"Overall: {overall}")

    return {"overall": overall, "results": results, "anomalies": anomalies, "alerts": alerts}


def show_status():
    state = load_state()
    if not state:
        print("まだ検証が実行されていません。")
        return

    print("=== Last Mac Integrity Check ===")
    print(f"  実行日時: {state.get('last_check', 'unknown')}")
    print(f"  memory/ ファイル数: {state.get('memory_file_count', '?')}")
    print(f"  Qdrant ポイント数: {state.get('qdrant_point_count', '?')}")
    print(f"  $CMEM observation 数: {state.get('cmem_observation_count', '?')}")

    results = state.get("results", {})
    if results:
        print(f"  結果: {results}")

    anomalies = state.get("anomalies", [])
    if anomalies:
        print(f"  異常: {anomalies}")
    else:
        print("  異常: なし")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif "--quick" in sys.argv:
        run_check(quick=True)
    else:
        run_check()
