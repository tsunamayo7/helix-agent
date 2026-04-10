"""Anomaly Dispatcher — system_auditorの異常を検知したらOpusにタスクとして通知.

system_auditorが異常を検出したときに、以下を実行:
1. 重大度HIGH/CRITICALのみを抽出
2. Opus向けのタスクリクエスト(anomaly_queue.json)に追加
3. Discord通知(mention付き、即対応要請)
4. 次回Claude Codeセッション開始時に、このキューが空になるまでプロンプトに異常が注入される

使い方:
    python scripts/anomaly_dispatcher.py             # 最新監査レポートから異常を処理
    python scripts/anomaly_dispatcher.py status      # キュー状態確認
    python scripts/anomaly_dispatcher.py clear       # キュークリア(対応済みマーク)

タスクスケジューラで Helix-SystemAudit の後に実行するのが推奨。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HELIX_DIR = Path.home() / ".helix-agent"
LATEST_REPORT = HELIX_DIR / "audit_reports" / "latest.json"
QUEUE_FILE = HELIX_DIR / "anomaly_queue.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"


def load_latest_report() -> dict:
    if not LATEST_REPORT.exists():
        return {}
    try:
        return json.loads(LATEST_REPORT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_queue() -> dict:
    if not QUEUE_FILE.exists():
        return {"pending": [], "resolved": [], "last_updated": None}
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"pending": [], "resolved": [], "last_updated": None}


def save_queue(queue: dict) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    queue["last_updated"] = datetime.now(timezone.utc).isoformat()
    QUEUE_FILE.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def extract_critical_findings(report: dict) -> list[dict]:
    """レポートから対応が必要な異常のみ抽出."""
    findings = report.get("findings", [])
    critical = []
    for f in findings:
        sev = f.get("severity", "").upper()
        if sev in ("HIGH", "CRITICAL"):
            critical.append({
                "severity": sev,
                "category": f.get("category", ""),
                "component": f.get("component", ""),
                "message": f.get("message", ""),
                "fix": f.get("fix", ""),
                "detected_at": report.get("timestamp", ""),
            })
    return critical


def make_fingerprint(finding: dict) -> str:
    """同一異常の重複判定用フィンガープリント."""
    return f"{finding.get('component', '')}|{finding.get('message', '')[:60]}"


def notify_discord(new_count: int, findings: list[dict]) -> None:
    """Discord通知 (Opus向け即対応要請)."""
    if not findings:
        return
    lines = [
        f"🚨 **異常検知** ({new_count}件追加)",
        "",
        "Opus起動時に自動処理されます。緊急の場合はセッション開始を推奨。",
        "",
    ]
    for f in findings[:8]:
        sev = f.get("severity", "?")
        comp = f.get("component", "?")
        msg = f.get("message", "")[:80]
        fix = f.get("fix", "")
        lines.append(f"**[{sev}]** `{comp}`")
        lines.append(f"  → {msg}")
        if fix:
            lines.append(f"  修正案: {fix[:60]}")
        lines.append("")
    msg = "\n".join(lines)
    try:
        subprocess.run(
            ["python", str(WEBHOOK_SCRIPT), msg],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        print(f"Discord通知失敗: {e}", file=sys.stderr)


def dispatch():
    report = load_latest_report()
    if not report:
        print("監査レポートなし")
        return

    critical = extract_critical_findings(report)
    if not critical:
        print("対応必要な異常なし")
        return

    queue = load_queue()
    existing_fps = {make_fingerprint(f) for f in queue["pending"]}
    new_findings = []
    for f in critical:
        fp = make_fingerprint(f)
        if fp not in existing_fps:
            queue["pending"].append(f)
            new_findings.append(f)

    if not new_findings:
        print(f"新規異常なし (既存pending: {len(queue['pending'])}件)")
        return

    save_queue(queue)
    print(f"新規異常 {len(new_findings)}件をキューに追加")
    for f in new_findings:
        print(f"  [{f['severity']}] {f['component']}: {f['message'][:80]}")

    # Discord通知
    notify_discord(len(new_findings), new_findings)


def status():
    queue = load_queue()
    print("=== Anomaly Queue Status ===")
    print(f"Pending: {len(queue['pending'])}件")
    for f in queue["pending"]:
        print(f"  [{f.get('severity', '?')}] {f.get('component', '?')}: {f.get('message', '')[:80]}")
    print(f"Resolved: {len(queue['resolved'])}件 (累計)")
    print(f"Last updated: {queue.get('last_updated', 'never')}")


def clear():
    """全pendingをresolvedに移動 (Opusが対応完了した際に呼ぶ)."""
    queue = load_queue()
    for f in queue["pending"]:
        f["resolved_at"] = datetime.now(timezone.utc).isoformat()
        queue["resolved"].append(f)
    count = len(queue["pending"])
    queue["pending"] = []
    # resolvedは過去100件のみ保持
    queue["resolved"] = queue["resolved"][-100:]
    save_queue(queue)
    print(f"{count}件をresolvedに移動")


def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "status":
            status()
        elif cmd == "clear":
            clear()
        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)
    else:
        dispatch()


if __name__ == "__main__":
    main()
