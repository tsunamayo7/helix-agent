"""Critical Files Guard — 重要な規定/設定ファイルの変更追跡と喪失防止.

保護対象: CLAUDE.md, settings.json, MEMORY.md, 部門RAGスキル, 重要hooksなど

機能:
  1. 定期的にSHA-256ハッシュを記録 (snapshot履歴保存)
  2. 変更を検知したらdiffをMEMORY変更ログに追記
  3. 削除/空化された場合はDiscord即通知 + 最新スナップショットから自動復元
  4. 異常な大量変更 (ファイルサイズ50%減等) をアラート

使い方:
    python scripts/critical_files_guard.py              # スナップショット取得+変更検知
    python scripts/critical_files_guard.py status       # 現在の保護状態表示
    python scripts/critical_files_guard.py restore PATH # 指定ファイルを最新スナップショットから復元
    python scripts/critical_files_guard.py history PATH # 指定ファイルの変更履歴表示

タスクスケジューラで30分毎推奨。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

STATE_DIR = Path.home() / ".helix-agent" / "critical_guard"
SNAPSHOT_DIR = STATE_DIR / "snapshots"
STATE_FILE = STATE_DIR / "state.json"
CHANGE_LOG = STATE_DIR / "change_log.jsonl"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# スナップショット保持数
MAX_SNAPSHOTS_PER_FILE = 30  # 各ファイル最大30世代

# 保護対象ファイル
CRITICAL_FILES = [
    # Claude設定
    Path.home() / ".claude" / "CLAUDE.md",
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "statusline.py",
    # メモリ
    Path.home() / ".claude" / "projects" / "C--Development" / "memory" / "MEMORY.md",
    # フック
    Path.home() / ".claude" / "hooks" / "pretool_security.py",
    Path.home() / ".claude" / "hooks" / "smart_approval.py",
    Path.home() / ".claude" / "hooks" / "timeout_auto_approve.py",
    Path.home() / ".claude" / "hooks" / "session_checkpoint.py",
    Path.home() / ".claude" / "hooks" / "persist_task.py",
    Path.home() / ".claude" / "hooks" / "failure_learner.py",
    Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py",
    # 起動
    Path("C:/Development/start/manual/start_claude.bat"),
    Path("C:/Development/start/auto/start_all_services.bat"),
    Path("C:/Development/start/manual/run_bg.vbs"),
    Path("C:/Development/start/qdrant_memory_server.py"),
    # 環境マニフェスト
    Path("C:/Development/tools/helix-agent/config/environment_manifest.yaml"),
]

# 保護対象ディレクトリ (Skillsフォルダ全体など)
CRITICAL_DIRS = [
    Path.home() / ".claude" / "skills" / "corp-review",
    Path.home() / ".claude" / "skills" / "corp-implement",
    Path.home() / ".claude" / "skills" / "corp-investigate",
    Path.home() / ".claude" / "skills" / "corp-request-info",
    Path.home() / ".claude" / "skills" / "dept-status",
]

# アラート閾値
SIZE_DROP_THRESHOLD = 0.5  # ファイルサイズが50%未満に減ったら警告
CONTENT_HASH_ALGO = "sha256"


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_hash(path: Path) -> str:
    """ファイルのSHA-256ハッシュ."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"files": {}, "last_run": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"files": {}, "last_run": None}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["last_run"] = now_iso()
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def append_change_log(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry["timestamp"] = now_iso()
    try:
        with open(CHANGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def snapshot_key(path: Path) -> str:
    """パスからスナップショット用キー (パス区切りを_に)."""
    s = str(path).replace("\\", "/").replace(":", "").replace("/", "__")
    return s


def save_snapshot(path: Path) -> str | None:
    """ファイルをスナップショットディレクトリに保存. 戻り値はスナップショットファイル名."""
    if not path.exists() or not path.is_file():
        return None
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    key = snapshot_key(path)
    file_dir = SNAPSHOT_DIR / key
    file_dir.mkdir(exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    ext = path.suffix
    snap_name = f"{ts}{ext}"
    snap_path = file_dir / snap_name
    try:
        shutil.copy2(path, snap_path)
    except Exception as e:
        append_change_log({"event": "snapshot_failed", "path": str(path), "error": str(e)})
        return None

    # 古いスナップショット削除
    try:
        snaps = sorted(file_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in snaps[MAX_SNAPSHOTS_PER_FILE:]:
            old.unlink()
    except Exception:
        pass

    return snap_name


def latest_snapshot(path: Path) -> Path | None:
    """最新のスナップショットパスを返す."""
    key = snapshot_key(path)
    file_dir = SNAPSHOT_DIR / key
    if not file_dir.exists():
        return None
    snaps = sorted(file_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return snaps[0] if snaps else None


def notify_discord(msg: str) -> None:
    try:
        subprocess.run(
            ["python", str(WEBHOOK_SCRIPT), msg],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 異常検知ロジック
# ---------------------------------------------------------------------------


def check_file(path: Path, state: dict) -> list[dict]:
    """1ファイルをチェック. 異常があれば findings を返す."""
    findings = []
    key = str(path).replace("\\", "/")
    prev = state["files"].get(key, {})

    if not path.exists():
        # ファイル削除検知
        if prev.get("hash"):
            # 以前は存在していた → 緊急事態
            latest = latest_snapshot(path)
            findings.append({
                "severity": "CRITICAL",
                "type": "file_deleted",
                "path": key,
                "previous_hash": prev.get("hash", "")[:16],
                "previous_size": prev.get("size", 0),
                "snapshot_available": str(latest) if latest else None,
                "message": f"重要ファイルが削除されました: {path.name}",
            })
        return findings

    try:
        stat = path.stat()
        current_size = stat.st_size
    except Exception:
        return findings

    current_hash = file_hash(path)
    if not current_hash:
        return findings

    # 初回記録
    if not prev:
        state["files"][key] = {
            "hash": current_hash,
            "size": current_size,
            "first_seen": now_iso(),
            "last_change": now_iso(),
            "change_count": 0,
        }
        snap_name = save_snapshot(path)
        append_change_log({
            "event": "first_seen",
            "path": key,
            "size": current_size,
            "snapshot": snap_name,
        })
        return findings

    # 変更なし
    if prev["hash"] == current_hash:
        return findings

    # 変更あり
    prev_size = prev.get("size", 0)
    size_ratio = current_size / prev_size if prev_size else 1.0

    # ファイル空化 or 大幅減少
    if current_size == 0:
        findings.append({
            "severity": "CRITICAL",
            "type": "file_empty",
            "path": key,
            "message": f"ファイルが空になりました: {path.name}",
        })
    elif size_ratio < SIZE_DROP_THRESHOLD:
        findings.append({
            "severity": "HIGH",
            "type": "size_drop",
            "path": key,
            "previous_size": prev_size,
            "current_size": current_size,
            "drop_pct": (1 - size_ratio) * 100,
            "message": f"サイズが{int((1 - size_ratio) * 100)}%減少: {path.name}",
        })

    # 変更記録
    snap_name = save_snapshot(path)
    state["files"][key] = {
        "hash": current_hash,
        "size": current_size,
        "first_seen": prev.get("first_seen", now_iso()),
        "last_change": now_iso(),
        "change_count": prev.get("change_count", 0) + 1,
    }
    append_change_log({
        "event": "changed",
        "path": key,
        "previous_hash": prev["hash"][:16],
        "current_hash": current_hash[:16],
        "previous_size": prev_size,
        "current_size": current_size,
        "snapshot": snap_name,
    })

    return findings


def check_directory(dir_path: Path, state: dict) -> list[dict]:
    """ディレクトリ配下の全ファイルをチェック."""
    findings = []
    if not dir_path.exists():
        # ディレクトリごと消えている
        key = str(dir_path).replace("\\", "/")
        if state["files"].get(f"{key}/*"):
            findings.append({
                "severity": "CRITICAL",
                "type": "directory_deleted",
                "path": key,
                "message": f"重要ディレクトリが削除されました: {dir_path.name}",
            })
        return findings

    # ディレクトリ配下のファイルリストを記録
    key_dir = f"{str(dir_path).replace(chr(92), '/')}/*"
    current_files = sorted([str(p) for p in dir_path.rglob("*") if p.is_file()])
    prev_list = state["files"].get(key_dir, {}).get("files", [])

    # ファイル減少検知
    if prev_list and len(current_files) < len(prev_list) * 0.5:
        findings.append({
            "severity": "HIGH",
            "type": "dir_files_decreased",
            "path": str(dir_path),
            "previous_count": len(prev_list),
            "current_count": len(current_files),
            "message": f"ディレクトリ内ファイルが大幅減少: {dir_path.name}",
        })

    state["files"][key_dir] = {
        "files": current_files,
        "count": len(current_files),
        "last_check": now_iso(),
    }

    # 各ファイルチェック
    for f in current_files:
        fp = Path(f)
        findings.extend(check_file(fp, state))

    return findings


# ---------------------------------------------------------------------------
# 自動復元
# ---------------------------------------------------------------------------


def auto_restore(path: Path) -> bool:
    """削除/空化されたファイルを最新スナップショットから復元."""
    latest = latest_snapshot(path)
    if not latest:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(latest, path)
        append_change_log({
            "event": "auto_restored",
            "path": str(path),
            "from_snapshot": latest.name,
        })
        return True
    except Exception as e:
        append_change_log({
            "event": "auto_restore_failed",
            "path": str(path),
            "error": str(e),
        })
        return False


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def run_guard():
    state = load_state()
    all_findings = []

    for f in CRITICAL_FILES:
        all_findings.extend(check_file(f, state))

    for d in CRITICAL_DIRS:
        all_findings.extend(check_directory(d, state))

    save_state(state)

    # CRITICALは自動復元を試みる
    auto_restored = []
    for finding in all_findings:
        if finding["severity"] == "CRITICAL" and finding["type"] in ("file_deleted", "file_empty"):
            path = Path(finding["path"])
            if auto_restore(path):
                finding["auto_restored"] = True
                auto_restored.append(finding["path"])

    # Discord通知
    critical = [f for f in all_findings if f["severity"] in ("CRITICAL", "HIGH")]
    if critical:
        lines = [f"🛡️ **Critical Files Guard** - {len(critical)}件の異常"]
        if auto_restored:
            lines.append(f"✅ 自動復元: {len(auto_restored)}件")
        for f in critical[:5]:
            restored = " [復元済]" if f.get("auto_restored") else ""
            lines.append(f"  [{f['severity']}] {f['message']}{restored}")
        notify_discord("\n".join(lines))

    # レポート表示
    print(f"=== Critical Files Guard ===")
    print(f"保護対象: {len(CRITICAL_FILES)}ファイル + {len(CRITICAL_DIRS)}ディレクトリ")
    print(f"検出: {len(all_findings)}件 (CRITICAL: {sum(1 for f in all_findings if f['severity'] == 'CRITICAL')}, HIGH: {sum(1 for f in all_findings if f['severity'] == 'HIGH')})")
    if auto_restored:
        print(f"自動復元: {len(auto_restored)}件")
    for f in all_findings:
        mark = "!!" if f["severity"] == "CRITICAL" else "!"
        print(f"  {mark} [{f['severity']}] {f['message']}")

    return 0 if not critical else 1


def show_status():
    state = load_state()
    print("=== Critical Files Guard Status ===")
    print(f"Last run: {state.get('last_run', 'never')}")
    print(f"Tracked files: {len(state.get('files', {}))}件")
    print(f"Snapshots dir: {SNAPSHOT_DIR}")
    if SNAPSHOT_DIR.exists():
        total_snaps = sum(
            len(list(d.iterdir())) for d in SNAPSHOT_DIR.iterdir() if d.is_dir()
        )
        print(f"Total snapshots: {total_snaps}件")


def show_history(path_str: str):
    target = Path(path_str)
    if not CHANGE_LOG.exists():
        print("変更ログなし")
        return
    print(f"=== Change History for {target.name} ===")
    target_key = str(target).replace("\\", "/").lower()
    with CHANGE_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("path", "").lower() == target_key:
                    ts = entry.get("timestamp", "")[:19]
                    print(f"  [{ts}] {entry.get('event')}: size {entry.get('previous_size', '?')}→{entry.get('current_size', '?')}")
            except Exception:
                pass


def manual_restore(path_str: str):
    target = Path(path_str)
    if auto_restore(target):
        print(f"復元完了: {target}")
    else:
        print(f"復元失敗 (スナップショットなし): {target}")


def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "status":
            show_status()
        elif cmd == "history" and len(sys.argv) > 2:
            show_history(sys.argv[2])
        elif cmd == "restore" and len(sys.argv) > 2:
            manual_restore(sys.argv[2])
        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python critical_files_guard.py [status|history PATH|restore PATH]")
            sys.exit(1)
    else:
        sys.exit(run_guard())


if __name__ == "__main__":
    main()
