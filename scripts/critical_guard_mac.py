"""Mac Critical Guard — 重要ファイル SHA-256 監視 + 自動復元.

30分毎に launchd から起動。
CLAUDE.md, settings.json, MEMORY.md 等の重要ファイルの
SHA-256 ハッシュを記録し、削除/空化/異常な大幅変更を検知。
CRITICAL 検出時はスナップショットから自動復元 + Discord 通知。

Windows 版 (critical_files_guard.py) との差分:
  - 保護対象は Mac パス (Windows の bat/vbs/yaml は除外)
  - macOS hooks と Mac 固有設定を追加
  - スナップショットは ~/.helix-agent/critical_guard_mac/snapshots/

使い方:
    python3 scripts/critical_guard_mac.py              # スナップショット+変更検知
    python3 scripts/critical_guard_mac.py status       # 保護状態表示
    python3 scripts/critical_guard_mac.py restore PATH # 最新スナップショットから復元
    python3 scripts/critical_guard_mac.py history PATH # 変更履歴表示
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

STATE_DIR = Path.home() / ".helix-agent" / "critical_guard_mac"
SNAPSHOT_DIR = STATE_DIR / "snapshots"
STATE_FILE = STATE_DIR / "state.json"
CHANGE_LOG = STATE_DIR / "change_log.jsonl"
LOG_DIR = Path.home() / ".claude" / "logs"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

MAX_SNAPSHOTS_PER_FILE = 30

# Mac 保護対象ファイル
CRITICAL_FILES = [
    # Claude 設定
    Path.home() / ".claude" / "CLAUDE.md",
    Path.home() / ".claude" / "settings.json",
    # メモリ
    Path.home() / ".claude" / "projects" / "Development" / "memory" / "MEMORY.md",
    Path.home() / ".claude" / "projects" / "Development" / "memory" / "SESSIONS_INDEX.md",
    # Mac hooks
    Path.home() / ".claude" / "hooks" / "pretool_smart_approval.py",
    Path.home() / ".claude" / "hooks" / "pretool_security_mac.py",
    Path.home() / ".claude" / "hooks" / "failure_learner_mac.py",
    Path.home() / ".claude" / "hooks" / "session_qdrant_canary.py",
    Path.home() / ".claude" / "hooks" / "precompact_guard.py",
    # helix-agent コア設定
    Path.home() / "Development" / "tools" / "helix-agent" / "server.py",
]

# 保護対象ディレクトリ (存在するもののみ)
CRITICAL_DIRS: list[Path] = []
for _d in [
    Path.home() / ".claude" / "skills" / "corp-review",
    Path.home() / ".claude" / "skills" / "corp-implement",
    Path.home() / ".claude" / "skills" / "corp-investigate",
    Path.home() / ".claude" / "skills" / "corp-request-info",
    Path.home() / ".claude" / "skills" / "dept-status",
]:
    if _d.exists():
        CRITICAL_DIRS.append(_d)

SIZE_DROP_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs():
    for d in [STATE_DIR, SNAPSHOT_DIR, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"files": {}, "last_run": None}


def save_state(state: dict):
    ensure_dirs()
    state["last_run"] = now_iso()
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def append_change_log(entry: dict):
    ensure_dirs()
    entry["timestamp"] = now_iso()
    try:
        with open(CHANGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def snapshot_key(path: Path) -> str:
    return str(path).replace("/", "__").lstrip("_")


def save_snapshot(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    key = snapshot_key(path)
    file_dir = SNAPSHOT_DIR / key
    file_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snap_name = f"{ts}{path.suffix}"
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
    key = snapshot_key(path)
    file_dir = SNAPSHOT_DIR / key
    if not file_dir.exists():
        return None
    snaps = sorted(file_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return snaps[0] if snaps else None


def notify(msg: str):
    if not WEBHOOK_SCRIPT.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(WEBHOOK_SCRIPT), msg],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 変更検知
# ---------------------------------------------------------------------------

def check_file(path: Path, state: dict) -> list[dict]:
    findings = []
    key = str(path)
    prev = state["files"].get(key, {})

    if not path.exists():
        if prev.get("hash"):
            latest = latest_snapshot(path)
            findings.append({
                "severity": "CRITICAL",
                "type": "file_deleted",
                "path": key,
                "previous_size": prev.get("size", 0),
                "snapshot_available": str(latest) if latest else None,
                "message": f"重要ファイル削除: {path.name}",
            })
        return findings

    try:
        current_size = path.stat().st_size
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
        save_snapshot(path)
        append_change_log({"event": "first_seen", "path": key, "size": current_size})
        return findings

    # 変更なし
    if prev["hash"] == current_hash:
        return findings

    # 変更あり
    prev_size = prev.get("size", 0)
    size_ratio = current_size / prev_size if prev_size else 1.0

    if current_size == 0:
        findings.append({
            "severity": "CRITICAL",
            "type": "file_empty",
            "path": key,
            "message": f"ファイル空化: {path.name}",
        })
    elif size_ratio < SIZE_DROP_THRESHOLD:
        findings.append({
            "severity": "HIGH",
            "type": "size_drop",
            "path": key,
            "previous_size": prev_size,
            "current_size": current_size,
            "drop_pct": (1 - size_ratio) * 100,
            "message": f"サイズ {int((1 - size_ratio) * 100)}%減少: {path.name}",
        })

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
    findings = []
    if not dir_path.exists():
        return findings

    key_dir = f"{str(dir_path)}/*"
    current_files = sorted([str(p) for p in dir_path.rglob("*") if p.is_file()])
    prev_list = state["files"].get(key_dir, {}).get("files", [])

    if prev_list and len(current_files) < len(prev_list) * 0.5:
        findings.append({
            "severity": "HIGH",
            "type": "dir_files_decreased",
            "path": str(dir_path),
            "previous_count": len(prev_list),
            "current_count": len(current_files),
            "message": f"ディレクトリ内ファイル大幅減少: {dir_path.name}",
        })

    state["files"][key_dir] = {
        "files": current_files,
        "count": len(current_files),
        "last_check": now_iso(),
    }

    for f in current_files:
        findings.extend(check_file(Path(f), state))

    return findings


# ---------------------------------------------------------------------------
# 自動復元
# ---------------------------------------------------------------------------

def auto_restore(path: Path) -> bool:
    snap = latest_snapshot(path)
    if not snap:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snap, path)
        append_change_log({
            "event": "auto_restored",
            "path": str(path),
            "from_snapshot": snap.name,
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
    ensure_dirs()
    state = load_state()
    all_findings = []

    for f in CRITICAL_FILES:
        all_findings.extend(check_file(f, state))

    for d in CRITICAL_DIRS:
        all_findings.extend(check_directory(d, state))

    save_state(state)

    # CRITICAL は自動復元
    auto_restored = []
    for finding in all_findings:
        if finding["severity"] == "CRITICAL" and finding["type"] in ("file_deleted", "file_empty"):
            path = Path(finding["path"])
            if auto_restore(path):
                finding["auto_restored"] = True
                auto_restored.append(finding["path"])

    # Discord 通知
    critical = [f for f in all_findings if f["severity"] in ("CRITICAL", "HIGH")]
    if critical:
        lines = [f"[Mac Critical Guard] {len(critical)}件の異常"]
        if auto_restored:
            lines.append(f"自動復元: {len(auto_restored)}件")
        for f in critical[:5]:
            restored = " [復元済]" if f.get("auto_restored") else ""
            lines.append(f"  [{f['severity']}] {f['message']}{restored}")
        notify("\n".join(lines))

    # レポート
    print(f"=== Mac Critical Guard ===")
    print(f"保護対象: {len(CRITICAL_FILES)}ファイル + {len(CRITICAL_DIRS)}ディレクトリ")
    print(f"検出: {len(all_findings)}件 "
          f"(CRITICAL: {sum(1 for f in all_findings if f['severity'] == 'CRITICAL')}, "
          f"HIGH: {sum(1 for f in all_findings if f['severity'] == 'HIGH')})")
    if auto_restored:
        print(f"自動復元: {len(auto_restored)}件")
    for f in all_findings:
        mark = "!!" if f["severity"] == "CRITICAL" else "!"
        print(f"  {mark} [{f['severity']}] {f['message']}")

    return 0 if not critical else 1


def show_status():
    state = load_state()
    print("=== Mac Critical Guard Status ===")
    print(f"Last run: {state.get('last_run', 'never')}")
    print(f"Tracked files: {len(state.get('files', {}))}件")
    if SNAPSHOT_DIR.exists():
        total = sum(
            len(list(d.iterdir())) for d in SNAPSHOT_DIR.iterdir() if d.is_dir()
        )
        print(f"Total snapshots: {total}件")


def show_history(path_str: str):
    target = Path(path_str)
    if not CHANGE_LOG.exists():
        print("変更ログなし")
        return
    print(f"=== Change History for {target.name} ===")
    target_key = str(target)
    with CHANGE_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("path", "") == target_key:
                    ts = entry.get("timestamp", "")[:19]
                    event = entry.get("event", "?")
                    print(f"  [{ts}] {event}: size {entry.get('previous_size', '?')}->{entry.get('current_size', '?')}")
            except Exception:
                pass


def manual_restore(path_str: str):
    target = Path(path_str)
    if auto_restore(target):
        print(f"復元完了: {target}")
    else:
        print(f"復元失敗 (スナップショットなし): {target}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "status":
            show_status()
        elif cmd == "history" and len(sys.argv) > 2:
            show_history(sys.argv[2])
        elif cmd == "restore" and len(sys.argv) > 2:
            manual_restore(sys.argv[2])
        else:
            print(f"Usage: python3 critical_guard_mac.py [status|history PATH|restore PATH]")
    else:
        sys.exit(run_guard())
