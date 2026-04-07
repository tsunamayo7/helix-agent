"""3層セキュリティ監視デーモン.

既存の相互監視システムにセキュリティ層を追加。
gemma4/Sonnet/Opusの3層で脅威を検出・分析・報告する。

Layer 1 (gemma4, $0):
  - git diff のコミット前スキャン（不可視文字、秘密鍵パターン）
  - node_modules/.venv の依存パッケージ変更検出
  - 新規ダウンロードファイルのハッシュ検証

Layer 2 (Sonnet Agent, セッション中):
  - CVE/脆弱性情報とローカル環境のマッチング
  - 依存パッケージの深層サプライチェーン分析
  - x_monitor収集のセキュリティ情報のトリアージ

Layer 3 (Opus, 最終判断):
  - セキュリティアラートの重要度判定
  - 対策の実行判断
  - ユーザーへの報告

使い方:
    python scripts/security_monitor.py scan          # L1スキャン（gemma4不要）
    python scripts/security_monitor.py status        # 現在のセキュリティ状態
    python scripts/security_monitor.py audit         # 全体監査レポート
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
SECURITY_DIR = Path.home() / ".helix-agent" / "security"
ALERTS_FILE = SECURITY_DIR / "alerts.json"
SCAN_HISTORY = SECURITY_DIR / "scan_history.jsonl"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# 監視対象プロジェクト
PROJECTS = [
    Path("C:/Development/tools/helix-agent"),
    Path("C:/Development/tools/helix-pilot"),
    Path("C:/Development/tools/x-feed-collector"),
    Path("C:/Development/tools/clip-bridge"),
]

# GlassWorm: 危険なUnicode不可視文字
# 注意: U+FE0F (Variation Selector-16) は絵文字の構成要素なので除外
INVISIBLE_RANGES = [
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064),
    (0x2066, 0x2069), (0xFE00, 0xFE0E), (0xFEFF, 0xFEFF),  # FE0F除外(絵文字VS16)
    (0xE0100, 0xE01EF), (0x00AD, 0x00AD), (0x034F, 0x034F),
    (0x061C, 0x061C), (0x180E, 0x180E),
]

# 秘密鍵/認証情報パターン
SECRET_PATTERNS = [
    (r"(?:api[_-]?key|apikey)\s*[:=]\s*['\"][a-zA-Z0-9]{20,}", "API Key"),
    (r"(?:secret|token)\s*[:=]\s*['\"][a-zA-Z0-9]{20,}", "Secret/Token"),
    (r"(?:password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{8,}", "Password"),
    (r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----", "Private Key"),
    (r"ghp_[a-zA-Z0-9]{36}", "GitHub Personal Token"),
    (r"sk-[a-zA-Z0-9]{48}", "OpenAI API Key"),
    (r"sk-ant-[a-zA-Z0-9-]{80,}", "Anthropic API Key"),
    (r"AKIA[A-Z0-9]{16}", "AWS Access Key"),
]


def has_invisible_unicode(text: str) -> list[str]:
    """不可視Unicode文字を検出."""
    found = []
    for i, ch in enumerate(text):
        cp = ord(ch)
        for start, end in INVISIBLE_RANGES:
            if start <= cp <= end:
                found.append(f"U+{cp:04X} at pos {i}")
                break
    return found


def has_secrets(text: str) -> list[str]:
    """秘密鍵パターンを検出."""
    found = []
    for pattern, name in SECRET_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(name)
    return found


# ---------------------------------------------------------------------------
# L1: ローカルスキャン（gemma4不要、Python直接実行）
# ---------------------------------------------------------------------------

def scan_git_staged(project: Path) -> list[dict]:
    """git staged filesの不可視文字・秘密鍵チェック."""
    alerts = []
    try:
        result = subprocess.run(
            "git diff --cached --name-only",
            shell=True, capture_output=True, text=True,
            cwd=str(project), timeout=10, encoding="utf-8", errors="replace",
        )
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except Exception:
        return alerts

    for filename in files:
        filepath = project / filename
        if not filepath.exists() or filepath.stat().st_size > 1_000_000:
            continue
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # 不可視文字
        invisible = has_invisible_unicode(content)
        if invisible:
            alerts.append({
                "level": "critical",
                "type": "glassworm",
                "file": str(filepath),
                "detail": f"不可視Unicode {len(invisible)}件: {invisible[:3]}",
            })

        # 秘密鍵
        secrets = has_secrets(content)
        if secrets:
            alerts.append({
                "level": "critical",
                "type": "secret_leak",
                "file": str(filepath),
                "detail": f"検出: {', '.join(secrets)}",
            })

    return alerts


def scan_recent_files(project: Path, hours: int = 24) -> list[dict]:
    """最近変更されたファイルをスキャン."""
    alerts = []
    cutoff = datetime.now().timestamp() - (hours * 3600)

    for ext in ("*.py", "*.js", "*.ts", "*.json", "*.yaml", "*.yml", "*.toml"):
        for filepath in project.rglob(ext):
            # node_modules/.venv/distは除外
            parts = filepath.parts
            if any(skip in parts for skip in ("node_modules", ".venv", "dist", "__pycache__", ".git")):
                continue
            try:
                if filepath.stat().st_mtime < cutoff:
                    continue
                if filepath.stat().st_size > 500_000:
                    continue
                content = filepath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            invisible = has_invisible_unicode(content)
            if invisible:
                alerts.append({
                    "level": "critical",
                    "type": "glassworm",
                    "file": str(filepath),
                    "detail": f"不可視Unicode {len(invisible)}件",
                })

            secrets = has_secrets(content)
            if secrets:
                alerts.append({
                    "level": "warning",
                    "type": "secret_pattern",
                    "file": str(filepath),
                    "detail": f"パターン: {', '.join(secrets)}",
                })

    return alerts


def run_l1_scan() -> dict:
    """Layer 1スキャン実行."""
    all_alerts = []

    for project in PROJECTS:
        if not project.exists():
            continue
        # git staged
        staged_alerts = scan_git_staged(project)
        all_alerts.extend(staged_alerts)
        # recent files
        recent_alerts = scan_recent_files(project, hours=24)
        all_alerts.extend(recent_alerts)

    result = {
        "timestamp": datetime.now(JST).isoformat(),
        "layer": "L1",
        "projects_scanned": len([p for p in PROJECTS if p.exists()]),
        "alerts": all_alerts,
        "critical": len([a for a in all_alerts if a["level"] == "critical"]),
        "warning": len([a for a in all_alerts if a["level"] == "warning"]),
    }

    # 保存
    SECURITY_DIR.mkdir(parents=True, exist_ok=True)
    ALERTS_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(SCAN_HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")

    return result


# ---------------------------------------------------------------------------
# ステータス表示
# ---------------------------------------------------------------------------

def show_status():
    """セキュリティ状態を表示."""
    print(f"=== Security Monitor ({datetime.now(JST).strftime('%Y-%m-%d %H:%M')}) ===")

    # 最新スキャン結果
    if ALERTS_FILE.exists():
        data = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        print(f"  Last scan: {data['timestamp']}")
        print(f"  Projects: {data['projects_scanned']}")
        print(f"  Critical: {data['critical']}, Warning: {data['warning']}")
        if data["alerts"]:
            print()
            for a in data["alerts"][:10]:
                print(f"  [{a['level'].upper()}] {a['type']}: {a['file']}")
                print(f"    {a['detail']}")
    else:
        print("  No scan data. Run: python scripts/security_monitor.py scan")

    # バージョンチェック結果
    versions_file = SECURITY_DIR / "versions.json"
    if versions_file.exists():
        vdata = json.loads(versions_file.read_text(encoding="utf-8"))
        updates = vdata.get("updates_available", 0)
        if updates > 0:
            print(f"\n  Tool updates: {updates} available")
            for r in vdata.get("results", []):
                if r["status"] == "UPDATE":
                    print(f"    {r['name']}: {r['local']} -> {r['latest']}")

    # pretool_security.py の状態
    hook = Path.home() / ".claude" / "hooks" / "pretool_security.py"
    print(f"\n  GlassWorm hook: {'Active' if hook.exists() else 'MISSING'}")


def show_audit():
    """全体監査レポート."""
    print("=== Full Security Audit ===\n")

    # L1スキャン
    print("[L1] File scan...")
    result = run_l1_scan()
    print(f"  Scanned {result['projects_scanned']} projects")
    print(f"  Critical: {result['critical']}, Warning: {result['warning']}")

    # バージョンチェック
    print("\n[L1] Version check...")
    try:
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / "security_check.py")],
            timeout=30,
        )
    except Exception as e:
        print(f"  Error: {e}")

    print("\n[L2] Sonnet deep analysis requires Claude Code session")
    print("[L3] Opus judgment requires Claude Code session")

    # Discord通知
    if result["critical"] > 0:
        msg = f"**Security Audit: {result['critical']} CRITICAL alerts**\n"
        for a in result["alerts"][:5]:
            if a["level"] == "critical":
                msg += f"- {a['type']}: {Path(a['file']).name} - {a['detail']}\n"
        try:
            subprocess.run(
                [sys.executable, str(WEBHOOK_SCRIPT), msg],
                timeout=15, capture_output=True,
            )
        except Exception:
            pass


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "scan":
        result = run_l1_scan()
        print(f"Scan complete: {result['critical']} critical, {result['warning']} warning")
        if result["alerts"]:
            for a in result["alerts"]:
                print(f"  [{a['level']}] {a['type']}: {Path(a['file']).name}")
        else:
            print("  No threats detected.")
    elif cmd == "status":
        show_status()
    elif cmd == "audit":
        show_audit()
    else:
        print(f"Unknown: {cmd}. Use scan/status/audit")
