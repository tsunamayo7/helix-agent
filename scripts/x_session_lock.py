"""X運用セッションロック — 複数Claude CLIが起動しても1つだけがX運用を実行する."""

import json
import os
import time
from pathlib import Path

LOCK_DIR = Path.home() / ".helix-agent" / "x_monitor"
LOCK_FILE = LOCK_DIR / "session.lock"
LOCK_TIMEOUT = 600  # 10分間更新がなければロック切れとみなす


def acquire_lock(session_id: str = "") -> bool:
    """ロックを取得。既に別セッションがロック中ならFalse."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)

    if LOCK_FILE.exists():
        try:
            data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            elapsed = time.time() - data.get("timestamp", 0)
            if elapsed < LOCK_TIMEOUT and data.get("session_id") != session_id:
                return False  # 別セッションがアクティブ
        except (json.JSONDecodeError, OSError):
            pass  # 壊れたロックファイルは上書き

    # ロック取得
    LOCK_FILE.write_text(
        json.dumps({
            "session_id": session_id or str(os.getpid()),
            "timestamp": time.time(),
            "pid": os.getpid(),
        }),
        encoding="utf-8",
    )
    return True


def refresh_lock(session_id: str = "") -> None:
    """ロックのタイムスタンプを更新（セッション生存確認用）."""
    if LOCK_FILE.exists():
        try:
            data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            data["timestamp"] = time.time()
            LOCK_FILE.write_text(json.dumps(data), encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            acquire_lock(session_id)


def release_lock() -> None:
    """ロックを解放."""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def is_locked_by_other(session_id: str = "") -> bool:
    """別セッションがロック中か."""
    if not LOCK_FILE.exists():
        return False
    try:
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        elapsed = time.time() - data.get("timestamp", 0)
        if elapsed >= LOCK_TIMEOUT:
            return False  # タイムアウト
        return data.get("session_id") != (session_id or str(os.getpid()))
    except (json.JSONDecodeError, OSError):
        return False


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        if LOCK_FILE.exists():
            data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            elapsed = time.time() - data.get("timestamp", 0)
            print(f"Lock: session={data.get('session_id')}, age={elapsed:.0f}s, pid={data.get('pid')}")
            print(f"Status: {'ACTIVE' if elapsed < LOCK_TIMEOUT else 'EXPIRED'}")
        else:
            print("No lock file")
    elif len(sys.argv) > 1 and sys.argv[1] == "release":
        release_lock()
        print("Lock released")
    else:
        print("Usage: x_session_lock.py [status|release]")
