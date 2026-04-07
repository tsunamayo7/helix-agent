"""2時間ごとの定期ヘルスチェック（タスクスケジューラ用）.

実行内容:
1. helix_status.py でダッシュボード確認
2. security_monitor.py でセキュリティスキャン
3. claude_usage_scraper.py で保存済み使用率を読み取り
4. 結果をDiscord Webhookで報告
5. 異常があれば緊急通知

タスクスケジューラ: 2時間間隔で実行
"""

import io
import json
import subprocess
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SCRIPTS_DIR = Path(__file__).parent
HELIX_DIR = Path.home() / ".helix-agent"
CLAUDE_USAGE_FILE = HELIX_DIR / "claude_usage" / "latest.json"
CODEX_USAGE_FILE = HELIX_DIR / "claude_usage" / "codex_latest.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"
JST = timezone(timedelta(hours=9))


def run_script(name: str, args: list[str] | None = None) -> str:
    """scriptsディレクトリ内のスクリプトを実行して出力を返す."""
    cmd = [sys.executable, str(SCRIPTS_DIR / name)]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
            cwd=str(SCRIPTS_DIR.parent),
        )
        return result.stdout.strip() + ("\n" + result.stderr.strip() if result.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        return f"{name}: timeout"
    except Exception as e:
        return f"{name}: error - {e}"


def load_json(path: Path) -> dict | None:
    """JSONファイルを読み込む."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def format_claude_usage(data: dict | None) -> str:
    """Claude使用率をフォーマット."""
    if not data:
        return "Claude: データなし"
    weekly = data.get("weekly_all", {})
    sonnet = data.get("sonnet_only", {})
    session = data.get("session", {})
    return (
        f"セッション: {session.get('percent', '?')}% / "
        f"全モデル: {weekly.get('percent', '?')}% ({weekly.get('reset', '?')}リセット) / "
        f"Sonnet: {sonnet.get('percent', '?')}% ({sonnet.get('reset', '?')}リセット)"
    )


def format_codex_usage(data: dict | None) -> str:
    """Codex使用率をフォーマット."""
    if not data:
        return "Codex: データなし"
    limits = data.get("limits", {})
    if not limits:
        return "Codex: パース失敗"
    parts = []
    for info in limits.values():
        parts.append(f"{info['label']}: {info['remaining_percent']}%残り")
    return " / ".join(parts)


def check_alerts(claude_data: dict | None) -> list[str]:
    """閾値チェック."""
    alerts = []
    if not claude_data:
        return alerts
    weekly = claude_data.get("weekly_all", {}).get("percent", 0)
    sonnet = claude_data.get("sonnet_only", {}).get("percent", 0)
    if weekly >= 85:
        alerts.append(f"🚨 Claude全体 {weekly}% — 緊急")
    elif weekly >= 70:
        alerts.append(f"⚠️ Claude全体 {weekly}% — 警告")
    if sonnet >= 85:
        alerts.append(f"🚨 Sonnet {sonnet}% — 緊急")
    elif sonnet >= 70:
        alerts.append(f"⚠️ Sonnet {sonnet}% — 警告")
    return alerts


def send_discord(message: str):
    """Discord Webhookで送信."""
    if not WEBHOOK_SCRIPT.exists():
        print(f"Webhook script not found: {WEBHOOK_SCRIPT}")
        return
    try:
        subprocess.run(
            [sys.executable, str(WEBHOOK_SCRIPT), message],
            timeout=15, capture_output=True,
        )
    except Exception as e:
        print(f"Discord send error: {e}")


def main():
    now = datetime.now(JST)
    print(f"=== Periodic Health Check: {now.strftime('%Y-%m-%d %H:%M JST')} ===")

    # 1. ダッシュボード（supervisor経由でデーモン再起動も実行）
    dashboard = run_script("helix_status.py")
    print(dashboard)

    # 2. セキュリティスキャン
    security = run_script("security_monitor.py", ["scan"])
    print(f"\nSecurity: {security}")

    # 3. 使用率読み取り（ブラウザ取得はClaude CLI側。ここでは保存済みデータを読む）
    claude_data = load_json(CLAUDE_USAGE_FILE)
    codex_data = load_json(CODEX_USAGE_FILE)
    claude_str = format_claude_usage(claude_data)
    codex_str = format_codex_usage(codex_data)
    print(f"\n{claude_str}")
    print(codex_str)

    # 4. アラートチェック
    alerts = check_alerts(claude_data)

    # 5. supervisorも実行（デーモン再起動）
    supervisor_result = run_script("supervisor.py", ["run-once"])
    print(f"\nSupervisor: {supervisor_result}")

    # 6. Discord報告作成
    # ダッシュボードからService/Daemon状態を抽出
    ok_count = dashboard.count("[OK]")
    ng_count = dashboard.count("[NG]")
    warn_count = dashboard.count("[WARN]")

    report_lines = [
        f"**📊 定期ヘルスチェック ({now.strftime('%H:%M JST')})**",
        "",
        f"**インフラ**: OK={ok_count} / NG={ng_count} / WARN={warn_count}",
        f"**Security**: {security}",
        f"**Claude**: {claude_str}",
        f"**Codex**: {codex_str}",
        f"**Supervisor**: {supervisor_result}",
    ]

    if alerts:
        report_lines.append("")
        report_lines.append("**⚠️ アラート**")
        report_lines.extend(alerts)

    report = "\n".join(report_lines)

    # 常にDiscord送信（状態記録として）
    send_discord(report)
    print(f"\n✅ Discord報告送信完了")

    # ログ保存
    log_dir = HELIX_DIR / "health_check"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"check_{now.strftime('%Y%m%d_%H%M')}.json"
    log_data = {
        "timestamp": now.isoformat(),
        "ok": ok_count,
        "ng": ng_count,
        "warn": warn_count,
        "security": security,
        "claude_usage": claude_data,
        "codex_usage": codex_data,
        "alerts": alerts,
        "supervisor": supervisor_result,
    }
    log_file.write_text(json.dumps(log_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ログ保存: {log_file}")


if __name__ == "__main__":
    main()
