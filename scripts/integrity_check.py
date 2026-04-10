"""Integrity Check — memory/Qdrant/$CMEM/バックアップの整合性検証.

日次バックアップ後、またはセッション開始時に実行。
異常検出時はDiscord Webhook通知。

使い方:
    python scripts/integrity_check.py           # 全チェック実行
    python scripts/integrity_check.py --quick    # 軽量チェックのみ
    python scripts/integrity_check.py status     # 前回結果表示
"""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Windows cp932対策
if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory"
CMEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
QDRANT_URL = "http://localhost:6333"
COLLECTION = "mem0_shared"
LIGHTRAG_URL = "http://localhost:9621"
STATE_DIR = Path.home() / ".helix-agent" / "integrity"
STATE_FILE = STATE_DIR / "state.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# 閾値
FILE_DROP_THRESHOLD = 0.8       # memory/ ファイル数が前回の80%未満で警告
QDRANT_DROP_THRESHOLD = 0.9     # Qdrantポイント数が前回の90%未満で警告
CMEM_DROP_THRESHOLD = 0.9       # $CMEM行数が前回の90%未満で警告


def load_state() -> dict:
    """前回の検証状態を読み込み."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    """検証状態を保存."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def send_alert(message: str) -> bool:
    """Discord Webhookで警告を送信."""
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
        return {"status": "error", "details": {"message": "memory/ ディレクトリが存在しない"}}

    md_files = list(MEMORY_DIR.glob("*.md"))
    result["details"]["file_count"] = len(md_files)

    # MEMORY.md存在チェック
    memory_index = MEMORY_DIR / "MEMORY.md"
    result["details"]["index_exists"] = memory_index.exists()
    if not memory_index.exists():
        result["status"] = "warning"
        result["details"]["message"] = "MEMORY.md が存在しない"

    # frontmatter検証 (type/name/descriptionの存在)
    broken_files = []
    for f in md_files:
        if f.name in ("MEMORY.md", "memory-dashboard.base"):
            continue
        try:
            content = f.read_text(encoding="utf-8")
            if content.startswith("---"):
                # frontmatter部分を抽出
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    fm = parts[1]
                    has_name = "name:" in fm
                    has_type = "type:" in fm
                    if not (has_name and has_type):
                        broken_files.append(f.name)
                else:
                    broken_files.append(f.name)
        except (OSError, UnicodeDecodeError):
            broken_files.append(f.name)

    if broken_files:
        result["details"]["broken_frontmatter"] = broken_files
        if len(broken_files) > 3:
            result["status"] = "warning"

    # ファイルサイズゼロチェック
    empty_files = [f.name for f in md_files if f.stat().st_size == 0]
    if empty_files:
        result["details"]["empty_files"] = empty_files
        result["status"] = "warning"

    return result


def check_qdrant() -> dict:
    """Qdrantコレクションの健全性チェック."""
    result = {"status": "ok", "details": {}}

    try:
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{COLLECTION}",
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        info = data.get("result", {})

        point_count = info.get("points_count", 0)
        status = info.get("status", "unknown")

        result["details"]["points_count"] = point_count
        result["details"]["collection_status"] = status

        if status != "green":
            result["status"] = "warning"
            result["details"]["message"] = f"コレクション状態: {status}"

        if point_count == 0:
            result["status"] = "error"
            result["details"]["message"] = "Qdrantポイント数が0"

    except Exception as e:
        result["status"] = "error"
        result["details"]["message"] = f"Qdrant接続失敗: {e}"

    return result


def check_cmem() -> dict:
    """$CMEM SQLiteデータベースの整合性チェック."""
    result = {"status": "ok", "details": {}}

    if not CMEM_DB.exists():
        return {"status": "warning", "details": {"message": "$CMEM DBが存在しない"}}

    try:
        conn = sqlite3.connect(str(CMEM_DB), timeout=5)

        # PRAGMA integrity_check
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        result["details"]["integrity"] = integrity[0] if integrity else "unknown"
        if integrity and integrity[0] != "ok":
            result["status"] = "error"
            result["details"]["message"] = f"SQLite整合性エラー: {integrity[0]}"

        # レコード数
        try:
            count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
            result["details"]["observation_count"] = count[0] if count else 0
        except sqlite3.OperationalError:
            result["details"]["observation_count"] = "table not found"

        # DBサイズ
        result["details"]["db_size_mb"] = round(CMEM_DB.stat().st_size / 1024 / 1024, 1)

        conn.close()
    except Exception as e:
        result["status"] = "error"
        result["details"]["message"] = f"$CMEM接続失敗: {e}"

    return result


def check_lightrag() -> dict:
    """LightRAGサーバーの健全性チェック."""
    result = {"status": "ok", "details": {}}

    try:
        req = urllib.request.Request(f"{LIGHTRAG_URL}/health")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        result["details"]["health"] = data
    except Exception as e:
        result["status"] = "warning"
        result["details"]["message"] = f"LightRAG接続失敗 (停止中の可能性): {e}"

    return result


def check_backup_freshness() -> dict:
    """ローカルバックアップの鮮度チェック."""
    result = {"status": "ok", "details": {}}

    backup_dirs = sorted(MEMORY_DIR.glob("_backup_*"))
    if not backup_dirs:
        result["status"] = "info"
        result["details"]["message"] = "ローカルバックアップなし"
        return result

    latest = backup_dirs[-1]
    try:
        mtime = latest.stat().st_mtime
        age_hours = (time.time() - mtime) / 3600
        result["details"]["latest_backup"] = latest.name
        result["details"]["age_hours"] = round(age_hours, 1)

        if age_hours > 168:  # 7日超
            result["status"] = "warning"
            result["details"]["message"] = f"最新バックアップが{int(age_hours/24)}日前"
    except OSError:
        pass

    result["details"]["backup_count"] = len(backup_dirs)
    return result


def check_checksums(backup_dir: Path) -> dict:
    """バックアップのチェックサム検証."""
    result = {"status": "ok", "details": {}}
    checksum_file = backup_dir / "checksums.sha256"

    if not checksum_file.exists():
        result["status"] = "info"
        result["details"]["message"] = "チェックサムファイルなし"
        return result

    errors = []
    verified = 0
    try:
        for line in checksum_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split("  ", 1)
            if len(parts) != 2:
                continue
            expected_hash, filepath = parts
            target = backup_dir / filepath
            if target.exists():
                actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                if actual_hash != expected_hash:
                    errors.append(filepath)
                else:
                    verified += 1
            else:
                errors.append(f"{filepath} (missing)")
    except Exception as e:
        result["status"] = "error"
        result["details"]["message"] = f"チェックサム検証失敗: {e}"
        return result

    result["details"]["verified"] = verified
    if errors:
        result["status"] = "error"
        result["details"]["failed"] = errors
    return result


# ---------------------------------------------------------------------------
# 差分検出（前回比較）
# ---------------------------------------------------------------------------

def detect_anomalies(current: dict, prev_state: dict) -> list[str]:
    """前回の結果と比較して異常を検出."""
    anomalies = []

    # memory/ ファイル数の急減
    prev_count = prev_state.get("memory_file_count", 0)
    curr_count = current.get("memory", {}).get("details", {}).get("file_count", 0)
    if prev_count > 0 and curr_count < prev_count * FILE_DROP_THRESHOLD:
        anomalies.append(
            f"memory/ ファイル数急減: {prev_count} → {curr_count} "
            f"({(1 - curr_count/prev_count)*100:.0f}%減少)"
        )

    # Qdrantポイント数の急減
    prev_qdrant = prev_state.get("qdrant_point_count", 0)
    curr_qdrant = current.get("qdrant", {}).get("details", {}).get("points_count", 0)
    if prev_qdrant > 0 and curr_qdrant < prev_qdrant * QDRANT_DROP_THRESHOLD:
        anomalies.append(
            f"Qdrantポイント数急減: {prev_qdrant} → {curr_qdrant}"
        )

    # $CMEM行数の急減
    prev_cmem = prev_state.get("cmem_observation_count", 0)
    curr_cmem = current.get("cmem", {}).get("details", {}).get("observation_count", 0)
    if isinstance(curr_cmem, int) and prev_cmem > 0 and curr_cmem < prev_cmem * CMEM_DROP_THRESHOLD:
        anomalies.append(
            f"$CMEM observation数急減: {prev_cmem} → {curr_cmem}"
        )

    return anomalies


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def run_check(quick: bool = False) -> dict:
    """全整合性チェックを実行."""
    prev_state = load_state()
    results = {}
    alerts = []

    print("=== Integrity Check ===")
    print()

    # 1. memory/
    print("[1/5] memory/ ファイル検証...")
    results["memory"] = check_memory_files()
    mc = results["memory"]["details"].get("file_count", "?")
    print(f"  ファイル数: {mc}, 状態: {results['memory']['status']}")

    # 2. Qdrant
    print("[2/5] Qdrant検証...")
    results["qdrant"] = check_qdrant()
    qc = results["qdrant"]["details"].get("points_count", "?")
    print(f"  ポイント数: {qc}, 状態: {results['qdrant']['status']}")

    # 3. $CMEM
    print("[3/5] $CMEM DB検証...")
    results["cmem"] = check_cmem()
    cc = results["cmem"]["details"].get("observation_count", "?")
    ci = results["cmem"]["details"].get("integrity", "?")
    print(f"  observation数: {cc}, integrity: {ci}, 状態: {results['cmem']['status']}")

    if not quick:
        # 4. LightRAG
        print("[4/5] LightRAG検証...")
        results["lightrag"] = check_lightrag()
        print(f"  状態: {results['lightrag']['status']}")

        # 5. バックアップ鮮度
        print("[5/5] バックアップ鮮度検証...")
        results["backup"] = check_backup_freshness()
        print(f"  状態: {results['backup']['status']}")
    else:
        print("[4/5] LightRAG... スキップ (--quick)")
        print("[5/5] バックアップ... スキップ (--quick)")

    # 差分検出
    anomalies = detect_anomalies(results, prev_state)
    if anomalies:
        print()
        print("⚠️ 異常検出:")
        for a in anomalies:
            print(f"  - {a}")
            alerts.append(a)

    # エラー/警告のサマリ
    errors = [k for k, v in results.items() if v.get("status") == "error"]
    warnings = [k for k, v in results.items() if v.get("status") == "warning"]

    if errors:
        alerts.append(f"エラー検出: {', '.join(errors)}")
    if warnings and not errors:
        # 警告のみの場合は詳細を追加
        for w in warnings:
            msg = results[w].get("details", {}).get("message", "")
            if msg:
                alerts.append(f"{w}: {msg}")

    # 状態保存
    new_state = {
        "last_check": datetime.now(timezone.utc).isoformat(),
        "memory_file_count": results.get("memory", {}).get("details", {}).get("file_count", 0),
        "qdrant_point_count": results.get("qdrant", {}).get("details", {}).get("points_count", 0),
        "cmem_observation_count": results.get("cmem", {}).get("details", {}).get("observation_count", 0),
        "results": {k: v["status"] for k, v in results.items()},
        "anomalies": anomalies,
    }
    save_state(new_state)

    # Discord通知（エラーまたは異常がある場合のみ）
    if alerts:
        msg = "🔍 **Integrity Check**\n" + "\n".join(f"- {a}" for a in alerts)
        send_alert(msg)

    print()
    overall = "ERROR" if errors else ("WARNING" if warnings else "OK")
    print(f"Overall: {overall}")

    return {"overall": overall, "results": results, "anomalies": anomalies, "alerts": alerts}


def show_status():
    """前回の検証結果を表示."""
    state = load_state()
    if not state:
        print("まだ検証が実行されていません。")
        return

    print("=== Last Integrity Check ===")
    print(f"  実行日時: {state.get('last_check', 'unknown')}")
    print(f"  memory/ ファイル数: {state.get('memory_file_count', '?')}")
    print(f"  Qdrant ポイント数: {state.get('qdrant_point_count', '?')}")
    print(f"  $CMEM observation数: {state.get('cmem_observation_count', '?')}")

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
