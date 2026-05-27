"""ヘルスチェックHTTPサーバー — 他デバイスからの死活監視用.

メインPC/サブPCで常時起動し、Raspberry Pi等から監視される。
タスクスケジューラで起動し、バックグラウンドで常駐。

ポート:
  メインPC: 8800
  サブPC:   8801

使い方:
    python scripts/health_server.py                  # デフォルト(8800)で起動
    python scripts/health_server.py --port 8801      # サブPC用
    python scripts/health_server.py --check 192.168.x.x:8800  # リモートチェック

エンドポイント:
    GET /health       → 全体ステータス (JSON)
    GET /heartbeats   → デーモン別ハートビート
    GET /queue        → タスクキュー状態
    GET /hw           → ハードウェア状態
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HELIX_DIR = Path.home() / ".helix-agent"


class HealthHandler(BaseHTTPRequestHandler):
    """ヘルスチェック用HTTPハンドラ."""

    def do_GET(self):
        if self.path == "/health":
            self._respond_json(self._get_health())
        elif self.path == "/heartbeats":
            self._respond_json(self._get_heartbeats())
        elif self.path == "/queue":
            self._respond_json(self._get_queue())
        elif self.path == "/hw":
            self._respond_json(self._get_hw())
        else:
            self._respond_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/heartbeat":
            self._handle_remote_heartbeat()
        else:
            self._respond_json({"error": "not found"}, 404)

    def _handle_remote_heartbeat(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            daemon = body.get("daemon", "")
            if not daemon or not all(c.isalnum() or c == "_" for c in daemon):
                self._respond_json({"error": "invalid daemon name"}, 400)
                return
            hb_dir = HELIX_DIR / "heartbeats"
            hb_dir.mkdir(parents=True, exist_ok=True)
            body.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            body["remote"] = True
            (hb_dir / f"{daemon}.json").write_text(
                json.dumps(body, ensure_ascii=False), encoding="utf-8",
            )
            self._respond_json({"ok": True, "daemon": daemon})
        except (json.JSONDecodeError, ValueError) as e:
            self._respond_json({"error": str(e)}, 400)

    def _respond_json(self, data: dict, code: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # ログ抑制

    def _get_health(self) -> dict:
        """全体ヘルスチェック."""
        hb = self._get_heartbeats()
        hw = self._get_hw()
        queue = self._get_queue()

        # 全デーモンの最新ハートビート時刻から生死判定
        all_alive = all(
            d.get("age_min", 999) < 15
            for d in hb.get("daemons", {}).values()
        )

        return {
            "status": "healthy" if all_alive else "degraded",
            "hostname": socket.gethostname(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "daemons_alive": sum(1 for d in hb.get("daemons", {}).values() if d.get("age_min", 999) < 15),
            "daemons_total": len(hb.get("daemons", {})),
            "hw_alerts": hw.get("alert_count", 0),
            "queue_pending": queue.get("pending", 0),
            "gpu_temp": hw.get("gpu_temp"),
            "vram_usage_pct": hw.get("vram_usage_pct"),
        }

    def _get_heartbeats(self) -> dict:
        """デーモン別ハートビート."""
        hb_dir = HELIX_DIR / "heartbeats"
        daemons = {}
        if hb_dir.exists():
            now = datetime.now(timezone.utc)
            for f in hb_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    ts = datetime.fromisoformat(data.get("timestamp", ""))
                    age_min = (now - ts).total_seconds() / 60
                    daemons[f.stem] = {
                        "alive": age_min < 15,
                        "age_min": round(age_min, 1),
                        "pid": data.get("pid"),
                        "timestamp": data.get("timestamp"),
                    }
                except (json.JSONDecodeError, ValueError, OSError):
                    daemons[f.stem] = {"alive": False, "error": "parse_error"}
        return {"daemons": daemons}

    def _get_queue(self) -> dict:
        """タスクキュー状態."""
        queue_file = HELIX_DIR / "assistant" / "queue.json"
        if not queue_file.exists():
            return {"total": 0, "pending": 0}
        try:
            tasks = json.loads(queue_file.read_text(encoding="utf-8"))
            pending = sum(1 for t in tasks if t.get("status") == "pending")
            return {
                "total": len(tasks),
                "pending": pending,
                "by_target": {
                    target: sum(1 for t in tasks if t.get("target") == target and t.get("status") == "pending")
                    for target in ["gemma4", "sonnet", "opus"]
                },
            }
        except (json.JSONDecodeError, OSError):
            return {"error": "parse_error"}

    def _get_hw(self) -> dict:
        """ハードウェア状態."""
        hw_file = HELIX_DIR / "hw_monitor" / "hw_status.json"
        if not hw_file.exists():
            return {"status": "no_data"}
        try:
            status = json.loads(hw_file.read_text(encoding="utf-8"))
            gpus = status.get("gpus", [])
            return {
                "timestamp": status.get("timestamp"),
                "alert_count": status.get("alert_count", 0),
                "gpu_temp": gpus[0].get("temp_c") if gpus else None,
                "vram_usage_pct": gpus[0].get("vram_usage_pct") if gpus else None,
                "cpu_usage_pct": status.get("cpu", {}).get("usage_pct"),
                "ram_pct": status.get("cpu", {}).get("ram_pct"),
            }
        except (json.JSONDecodeError, OSError):
            return {"error": "parse_error"}


def check_remote(address: str) -> dict | None:
    """リモートPCのヘルスをチェック."""
    try:
        url = f"http://{address}/health"
        resp = urllib.request.urlopen(url, timeout=5)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e), "address": address}


def main():
    parser = argparse.ArgumentParser(description="ヘルスチェックHTTPサーバー")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--check", type=str, default=None, help="リモートPCをチェック (host:port)")
    args = parser.parse_args()

    if args.check:
        result = check_remote(args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    server = HTTPServer(("0.0.0.0", args.port), HealthHandler)
    print(f"ヘルスチェックサーバー起動: http://0.0.0.0:{args.port}/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")


if __name__ == "__main__":
    main()
