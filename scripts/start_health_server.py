"""ヘルスサーバーの起動管理 — 既に起動中なら何もしない.

タスクスケジューラから5分ごとに呼ばれ、
ヘルスサーバーが動いていなければ起動する。
"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

PORT = 8800
SCRIPT = Path(__file__).resolve().parent / "health_server.py"


def is_running() -> bool:
    """ヘルスサーバーが応答するか確認."""
    try:
        resp = urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=3)
        return resp.status == 200
    except Exception:
        return False


def start():
    """ヘルスサーバーをバックグラウンドで起動."""
    subprocess.Popen(
        [sys.executable, str(SCRIPT), "--port", str(PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


if __name__ == "__main__":
    if is_running():
        print(f"ヘルスサーバー稼働中 (port={PORT})")
    else:
        print(f"ヘルスサーバーを起動します (port={PORT})")
        start()
