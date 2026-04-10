"""Memory Health — memory/ファイルの鮮度・品質チェック.

鮮度カテゴリ:
  現役: 14日以内に変更 or type=user/feedback
  経年: 14-30日未変更
  陳腐化: 30日超未変更 + type=project
  矛盾: 他ファイルと内容が矛盾（contradiction_detectorと連携）

使い方:
    python scripts/memory_health.py           # チェック実行
    python scripts/memory_health.py status    # 前回結果
"""

import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory"
STATE_DIR = Path.home() / ".helix-agent" / "memory_health"
STATE_FILE = STATE_DIR / "state.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# 鮮度閾値（日）
STALE_THRESHOLD = 14
OBSOLETE_THRESHOLD = 30

# 陳腐化しにくいtype
EVERGREEN_TYPES = {"user", "feedback", "reference"}


def parse_frontmatter(filepath: Path) -> dict:
    """frontmatterを解析."""
    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    if not content.startswith("---"):
        return {"body": content}

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {"body": content}

    fm = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            fm[key.strip()] = val.strip()
    fm["body"] = parts[2].strip()
    fm["body_length"] = len(parts[2].strip())
    return fm


def categorize_freshness(mtime: float, ftype: str) -> str:
    """鮮度カテゴリを判定."""
    age_days = (time.time() - mtime) / 86400

    if ftype in EVERGREEN_TYPES:
        # user/feedback/referenceは経年しにくい
        if age_days <= OBSOLETE_THRESHOLD:
            return "current"
        return "stale"

    if age_days <= STALE_THRESHOLD:
        return "current"
    elif age_days <= OBSOLETE_THRESHOLD:
        return "stale"
    else:
        return "obsolete"


def check_quality(filepath: Path, fm: dict) -> list[str]:
    """ファイルの品質問題を検出."""
    issues = []

    # frontmatterの不完全
    if not fm.get("name"):
        issues.append("frontmatter: name が未設定")
    if not fm.get("type"):
        issues.append("frontmatter: type が未設定")
    if not fm.get("description"):
        issues.append("frontmatter: description が未設定")

    # 本文が空
    body_len = fm.get("body_length", 0)
    if body_len < 10:
        issues.append(f"本文がほぼ空 ({body_len}文字)")

    # ファイルサイズゼロ
    if filepath.stat().st_size == 0:
        issues.append("ファイルサイズ 0")

    return issues


def send_notification(message: str) -> bool:
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


def run_check() -> dict:
    """全ファイルの鮮度・品質チェック."""
    print("=== Memory Health Check ===")
    print()

    if not MEMORY_DIR.exists():
        print("memory/ ディレクトリが存在しません。")
        return {"status": "error"}

    categories = {"current": [], "stale": [], "obsolete": []}
    quality_issues = []
    file_stats = []

    for filepath in sorted(MEMORY_DIR.glob("*.md")):
        if filepath.name in ("MEMORY.md", "memory-dashboard.base"):
            continue

        fm = parse_frontmatter(filepath)
        ftype = fm.get("type", "unknown")
        mtime = filepath.stat().st_mtime
        age_days = (time.time() - mtime) / 86400
        freshness = categorize_freshness(mtime, ftype)

        categories[freshness].append(filepath.name)

        issues = check_quality(filepath, fm)
        if issues:
            quality_issues.append({
                "file": filepath.name,
                "issues": issues,
            })

        file_stats.append({
            "file": filepath.name,
            "type": ftype,
            "freshness": freshness,
            "age_days": round(age_days, 1),
            "size": filepath.stat().st_size,
            "issues": issues,
        })

    # content/ チェック
    content_dir = MEMORY_DIR / "content"
    content_count = 0
    if content_dir.exists():
        content_count = len(list(content_dir.glob("*.md")))

    # サマリ表示
    print(f"ファイル数: {len(file_stats)}")
    print(f"  現役 (14日以内): {len(categories['current'])}")
    print(f"  経年 (14-30日): {len(categories['stale'])}")
    print(f"  陳腐化 (30日超): {len(categories['obsolete'])}")
    print(f"  content/: {content_count}ファイル")

    if categories["stale"]:
        print(f"\n⚠️ 経年ファイル ({len(categories['stale'])}件):")
        for f in categories["stale"]:
            print(f"  - {f}")

    if categories["obsolete"]:
        print(f"\n🔴 陳腐化ファイル ({len(categories['obsolete'])}件):")
        for f in categories["obsolete"]:
            print(f"  - {f}")

    if quality_issues:
        print(f"\n📝 品質問題 ({len(quality_issues)}件):")
        for qi in quality_issues:
            print(f"  - {qi['file']}: {', '.join(qi['issues'])}")

    # 結果保存
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_files": len(file_stats),
        "current": len(categories["current"]),
        "stale": len(categories["stale"]),
        "obsolete": len(categories["obsolete"]),
        "quality_issues": len(quality_issues),
        "stale_files": categories["stale"],
        "obsolete_files": categories["obsolete"],
        "quality_details": quality_issues,
    }
    STATE_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Discord通知（問題がある場合のみ）
    alerts = []
    if categories["obsolete"]:
        alerts.append(f"陳腐化: {len(categories['obsolete'])}件 (archive/への移動を検討)")
    if quality_issues:
        alerts.append(f"品質問題: {len(quality_issues)}件")

    if alerts:
        msg = "📊 **Memory Health**\n"
        msg += f"- 合計: {len(file_stats)}ファイル\n"
        msg += f"- 現役: {len(categories['current'])} / 経年: {len(categories['stale'])} / 陳腐化: {len(categories['obsolete'])}\n"
        for a in alerts:
            msg += f"- {a}\n"
        send_notification(msg)

    print(f"\nOverall: {'OK' if not alerts else 'ATTENTION NEEDED'}")
    return result


def show_status():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        print(f"最終チェック: {state.get('timestamp', 'unknown')}")
        print(f"現役: {state.get('current', 0)} / 経年: {state.get('stale', 0)} / 陳腐化: {state.get('obsolete', 0)}")
        print(f"品質問題: {state.get('quality_issues', 0)}件")
        if state.get("obsolete_files"):
            print(f"陳腐化: {', '.join(state['obsolete_files'][:5])}")
    else:
        print("まだ実行されていません。")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    else:
        run_check()
