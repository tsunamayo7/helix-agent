"""Environment Self-Heal — anomaly_queueの問題を自動修復.

system_auditor → anomaly_dispatcher で蓄積された異常のうち、
自動修復可能なものを自律的に修正する。

修復可能なパターン:
  1. task.<name>: 一度も実行されていません → schtasks /run で手動実行トリガー
  2. service.<name>: 応答なし → 対応する起動コマンドを実行
  3. daemon.<name>: 心拍が古い → タスクスケジューラから再実行
  4. file_deleted: critical_files_guardと連携で自動復元
  5. qdrant.<col>: ポイント数急減 → バックアップから復元提案 (手動)

修復不可能なものはanomaly_queueにそのまま残し、Claude起動時に通知。

使い方:
    python scripts/env_self_heal.py              # 全修復試行
    python scripts/env_self_heal.py --dry-run    # 何をするか表示のみ
    python scripts/env_self_heal.py status       # 修復履歴

タスクスケジューラで Helix-SystemAudit → anomaly_dispatcher → env_self_heal
の順で6時間毎実行されるのが理想。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HELIX_DIR = Path.home() / ".helix-agent"
QUEUE_FILE = HELIX_DIR / "anomaly_queue.json"
HEAL_LOG = HELIX_DIR / "self_heal_log.jsonl"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ---------------------------------------------------------------------------
# サービス起動コマンド定義
# ---------------------------------------------------------------------------

SERVICE_START_COMMANDS = {
    "qdrant_memory": [
        "C:\\Program Files\\Python312\\pythonw.exe",
        "C:\\Development\\start\\qdrant_memory_server.py",
    ],
    "clip_bridge": [
        "C:\\Program Files\\Python312\\pythonw.exe",
        "C:\\Development\\tools\\clip-bridge\\clip_server.py",
    ],
    "lightrag": [
        "C:\\Program Files\\Python312\\pythonw.exe",
        "C:\\Development\\tools\\lightrag-server\\start_server.py",
    ],
    "health_server": [
        "C:\\Development\\tools\\helix-agent\\.venv\\Scripts\\pythonw.exe",
        "C:\\Development\\tools\\helix-agent\\scripts\\health_server.py",
        "--port", "8800",
    ],
    "dashboard": [
        "C:\\Program Files\\Python312\\pythonw.exe",
        "C:\\Development\\tools\\helix-agent\\scripts\\dashboard_server.py",
    ],
}

SERVICE_URLS = {
    "qdrant_memory": "http://localhost:8080/health",
    "clip_bridge": "http://localhost:9999/health",
    "lightrag": "http://127.0.0.1:9621/health",
    "health_server": "http://localhost:8800/health",
    "dashboard": "http://localhost:8801/api/status",
}


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_queue() -> dict:
    if not QUEUE_FILE.exists():
        return {"pending": [], "resolved": [], "last_updated": None}
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"pending": [], "resolved": [], "last_updated": None}


def save_queue(queue: dict) -> None:
    queue["last_updated"] = now_iso()
    QUEUE_FILE.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def log_heal(entry: dict) -> None:
    HELIX_DIR.mkdir(parents=True, exist_ok=True)
    entry["timestamp"] = now_iso()
    try:
        with HEAL_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def check_http(url: str, timeout: int = 3) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def notify_discord(msg: str) -> None:
    try:
        subprocess.run(
            ["python", str(WEBHOOK_SCRIPT), msg],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 修復ハンドラ
# ---------------------------------------------------------------------------


def heal_service_down(component: str, dry_run: bool = False) -> dict:
    """service.<name> が DOWN の場合、対応するサービスを起動."""
    # component は "service.xxx" or "xxx" 形式
    name = component.split(".", 1)[-1] if "." in component else component

    if name not in SERVICE_START_COMMANDS:
        return {"action": "skip", "reason": f"No start command for {name}"}

    if dry_run:
        return {"action": "dry_run", "would_run": SERVICE_START_COMMANDS[name]}

    try:
        # バックグラウンドで起動
        subprocess.Popen(
            SERVICE_START_COMMANDS[name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    except Exception as e:
        return {"action": "failed", "error": str(e)}

    # 起動確認 (最大10秒)
    import time
    url = SERVICE_URLS.get(name)
    if url:
        for _ in range(5):
            time.sleep(2)
            if check_http(url):
                return {"action": "restarted", "verified": True}
        return {"action": "started", "verified": False, "note": "Started but health check failed"}

    return {"action": "started", "verified": None}


def heal_task_never_run(component: str, dry_run: bool = False) -> dict:
    """task.<name> が未実行の場合、schtasks /run で手動トリガー."""
    name = component.split(".", 1)[-1] if "." in component else component

    if dry_run:
        return {"action": "dry_run", "would_run": ["schtasks", "/run", "/tn", name]}

    try:
        result = subprocess.run(
            ["schtasks", "/run", "/tn", name],
            capture_output=True, text=True, timeout=10,
            creationflags=NO_WINDOW,
        )
        if result.returncode == 0:
            return {"action": "triggered", "output": result.stdout.strip()[:200]}
        return {"action": "failed", "error": result.stderr.strip()[:200]}
    except Exception as e:
        return {"action": "failed", "error": str(e)}


def heal_daemon_stale(component: str, dry_run: bool = False) -> dict:
    """daemon.<name> の心拍が古い場合、対応するタスクを再実行."""
    name = component.split(".", 1)[-1] if "." in component else component

    # daemon名 → タスク名マッピング
    daemon_to_task = {
        "supervisor": "Helix-Supervisor",
        "assistant_daemon": "Helix-AssistantDaemon",
        "watchdog": "Helix-Watchdog",
        "hw_monitor": "helix-hw-monitor",
        "usage_monitor": "Helix-UsageMonitor",
        "x_monitor": "helix-x-monitor",
        "cmem_bridge": "Helix-CMEMBridge",
    }
    task_name = daemon_to_task.get(name)
    if not task_name:
        return {"action": "skip", "reason": f"Unknown daemon: {name}"}

    return heal_task_never_run(f"task.{task_name}", dry_run=dry_run)


def heal_file_deleted(component: str, finding: dict, dry_run: bool = False) -> dict:
    """file_deleted の場合、critical_files_guardで自動復元を試みる."""
    if dry_run:
        return {"action": "dry_run", "would_restore": component}

    # critical_files_guard.py の manual_restore を呼ぶ
    path_candidate = finding.get("path") or component.split(".", 1)[-1]
    try:
        result = subprocess.run(
            [
                "C:\\Program Files\\Python312\\python.exe",
                "C:\\Development\\tools\\helix-agent\\scripts\\critical_files_guard.py",
                "restore", path_candidate,
            ],
            capture_output=True, text=True, timeout=30,
            creationflags=NO_WINDOW,
        )
        if "復元完了" in result.stdout:
            return {"action": "restored", "from_snapshot": True}
        return {"action": "failed", "output": result.stdout[:200]}
    except Exception as e:
        return {"action": "failed", "error": str(e)}


# ---------------------------------------------------------------------------
# ディスパッチ
# ---------------------------------------------------------------------------


def classify_finding(finding: dict) -> str:
    """findingを修復タイプに分類."""
    component = finding.get("component", "")
    ftype = finding.get("type", "")
    message = finding.get("message", "").lower()

    if component.startswith("service."):
        return "service_down"
    if component.startswith("task."):
        if "一度も実行されていません" in message or "never" in message:
            return "task_never_run"
        if "最終実行から" in message or "fail" in message:
            return "task_stale"
        return "task_unknown"
    if component.startswith("daemon."):
        return "daemon_stale"
    if ftype == "file_deleted" or ftype == "file_empty":
        return "file_deleted"
    if component.startswith("qdrant."):
        return "qdrant_issue"
    return "unknown"


def heal_finding(finding: dict, dry_run: bool = False) -> dict:
    """1件のfindingを修復試行."""
    classification = classify_finding(finding)
    component = finding.get("component", "")
    severity = finding.get("severity", "")

    result: dict = {
        "component": component,
        "severity": severity,
        "classification": classification,
    }

    if classification == "service_down":
        result.update(heal_service_down(component, dry_run=dry_run))
    elif classification == "task_never_run":
        result.update(heal_task_never_run(component, dry_run=dry_run))
    elif classification == "task_stale":
        # Stale taskは無条件再実行はリスクあり → skip (通知のみ)
        result.update({"action": "skip", "reason": "Stale task — manual check needed"})
    elif classification == "daemon_stale":
        result.update(heal_daemon_stale(component, dry_run=dry_run))
    elif classification == "file_deleted":
        result.update(heal_file_deleted(component, finding, dry_run=dry_run))
    elif classification == "qdrant_issue":
        result.update({"action": "skip", "reason": "Manual intervention required for Qdrant"})
    else:
        result.update({"action": "skip", "reason": f"No healer for {classification}"})

    log_heal(result)
    return result


def run_self_heal(dry_run: bool = False) -> dict:
    """全pending異常に対して修復試行."""
    queue = load_queue()
    pending = queue.get("pending", [])

    stats = {
        "total": len(pending),
        "healed": 0,
        "failed": 0,
        "skipped": 0,
        "results": [],
    }

    if not pending:
        print("未対応異常なし")
        return stats

    print(f"=== Self-Heal: {len(pending)}件のpending異常 ===")
    still_pending = []
    healed_items = []

    for finding in pending:
        result = heal_finding(finding, dry_run=dry_run)
        stats["results"].append(result)

        action = result.get("action", "")
        print(f"  [{finding.get('severity', '?')}] {finding.get('component', '?')}: {action}")

        if dry_run:
            still_pending.append(finding)
            continue

        # 修復成功 → resolvedへ
        if action in ("restarted", "triggered", "restored"):
            stats["healed"] += 1
            finding["resolved_at"] = now_iso()
            finding["heal_result"] = result
            healed_items.append(finding)
        elif action in ("failed", "started"):
            stats["failed"] += 1
            still_pending.append(finding)
        else:
            stats["skipped"] += 1
            still_pending.append(finding)

    if not dry_run and healed_items:
        # queue更新
        queue["pending"] = still_pending
        queue["resolved"] = queue.get("resolved", []) + healed_items
        queue["resolved"] = queue["resolved"][-100:]  # 最新100件保持
        save_queue(queue)

        # Discord通知
        lines = [
            f"🔧 **Self-Heal実行** ({stats['healed']}件自動修復)",
            "",
        ]
        for h in healed_items[:8]:
            comp = h.get("component", "?")
            action = h.get("heal_result", {}).get("action", "?")
            lines.append(f"✅ {comp}: {action}")
        if stats["failed"] > 0:
            lines.append(f"\n⚠️ 修復失敗: {stats['failed']}件 (手動対応必要)")
        if stats["skipped"] > 0:
            lines.append(f"⏭️ スキップ: {stats['skipped']}件 (修復対象外)")
        notify_discord("\n".join(lines))

    return stats


def show_status():
    """修復履歴を表示."""
    if not HEAL_LOG.exists():
        print("修復ログなし")
        return

    print("=== Self-Heal 修復履歴 ===")
    try:
        lines = HEAL_LOG.read_text(encoding="utf-8").strip().splitlines()
        for line in lines[-20:]:
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", "")[:19]
                comp = entry.get("component", "?")
                action = entry.get("action", "?")
                print(f"  [{ts}] {comp}: {action}")
            except Exception:
                pass
        print(f"\n総履歴: {len(lines)}件")
    except Exception as e:
        print(f"ログ読み込み失敗: {e}")


def main():
    dry_run = "--dry-run" in sys.argv
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
        return

    stats = run_self_heal(dry_run=dry_run)
    print(f"\n=== サマリー ===")
    print(f"Total: {stats['total']} | Healed: {stats['healed']} | Failed: {stats['failed']} | Skipped: {stats['skipped']}")


if __name__ == "__main__":
    main()
