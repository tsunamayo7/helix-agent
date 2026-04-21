"""Context Budget — セッション開始時のコンテキスト予算チェック.

memory/のファイル数・合計サイズからClaude Codeセッションの
コンテキスト消費量を推定し、過剰ならば整理を提案する。

推定式:
  1文字 ≈ 0.5トークン（日本語）
  1文字 ≈ 0.25トークン（英語/コード）
  CLAUDE.md + MEMORY.md + memory/ frontmatter → 起動時に読まれるトークン

使い方:
    python scripts/context_budget.py           # 予算チェック
    python scripts/context_budget.py --detail   # 詳細表示
"""

import io
import json
import os
import sys
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory"
CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
PROJECT_CLAUDE_MD = Path("C:/Development/.claude/CLAUDE.md") if Path("C:/Development/.claude/CLAUDE.md").exists() else None

# Opus 4.7 1Mコンテキスト
MAX_CONTEXT_TOKENS = 1_000_000

# トークン推定係数
JAPANESE_RATIO = 0.5    # 1日本語文字 ≈ 0.5トークン
ENGLISH_RATIO = 0.25    # 1英字 ≈ 0.25トークン
MIXED_RATIO = 0.35      # 日英混合の平均

# 警告閾値
WARN_PCT = 15   # 起動時に15%超 → 整理推奨
ALERT_PCT = 25  # 起動時に25%超 → 緊急整理


def estimate_tokens(text: str) -> int:
    """テキストのトークン数を推定."""
    if not text:
        return 0
    # 日本語文字の割合で係数を調整
    jp_chars = sum(1 for c in text if '\u3000' <= c <= '\u9fff' or '\uff00' <= c <= '\uffef')
    total_chars = len(text)
    if total_chars == 0:
        return 0

    jp_ratio = jp_chars / total_chars
    ratio = JAPANESE_RATIO * jp_ratio + ENGLISH_RATIO * (1 - jp_ratio)
    return int(total_chars * ratio)


def analyze_file(filepath: Path) -> dict:
    """ファイルのトークン消費を分析."""
    try:
        content = filepath.read_text(encoding="utf-8")
        tokens = estimate_tokens(content)
        return {
            "path": str(filepath.name),
            "size_bytes": len(content.encode("utf-8")),
            "chars": len(content),
            "tokens_est": tokens,
        }
    except (OSError, UnicodeDecodeError):
        return {
            "path": str(filepath.name),
            "size_bytes": 0,
            "chars": 0,
            "tokens_est": 0,
        }


def run_check(detail: bool = False) -> dict:
    """コンテキスト予算チェック."""
    print("=== Context Budget Check ===")
    print(f"Max context: {MAX_CONTEXT_TOKENS:,} tokens (Opus 4.7 1M)")
    print()

    components = []
    total_tokens = 0

    # 1. CLAUDE.md (グローバル)
    if CLAUDE_MD.exists():
        info = analyze_file(CLAUDE_MD)
        info["category"] = "CLAUDE.md (global)"
        components.append(info)
        total_tokens += info["tokens_est"]
        print(f"[1] CLAUDE.md (global): ~{info['tokens_est']:,} tok ({info['size_bytes']//1024}KB)")

    # 2. Project CLAUDE.md
    if PROJECT_CLAUDE_MD and PROJECT_CLAUDE_MD.exists():
        info = analyze_file(PROJECT_CLAUDE_MD)
        info["category"] = "CLAUDE.md (project)"
        components.append(info)
        total_tokens += info["tokens_est"]
        print(f"[2] CLAUDE.md (project): ~{info['tokens_est']:,} tok ({info['size_bytes']//1024}KB)")

    # 3. MEMORY.md (常時ロード)
    memory_index = MEMORY_DIR / "MEMORY.md"
    if memory_index.exists():
        info = analyze_file(memory_index)
        info["category"] = "MEMORY.md"
        components.append(info)
        total_tokens += info["tokens_est"]
        print(f"[3] MEMORY.md: ~{info['tokens_est']:,} tok ({info['size_bytes']//1024}KB)")

    # 4. session_prompt.txt (bat起動時注入)
    session_prompt = Path("C:/Development/start/manual/session_prompt.txt")
    if session_prompt.exists():
        info = analyze_file(session_prompt)
        info["category"] = "session_prompt"
        components.append(info)
        total_tokens += info["tokens_est"]

    # 5. memory/ファイル全体（参照時に読まれる潜在量）
    potential_tokens = 0
    file_details = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        info = analyze_file(f)
        potential_tokens += info["tokens_est"]
        file_details.append(info)

    # content/
    content_dir = MEMORY_DIR / "content"
    if content_dir.exists():
        for f in content_dir.rglob("*.md"):
            info = analyze_file(f)
            potential_tokens += info["tokens_est"]
            file_details.append(info)

    print(f"[4] memory/ 全ファイル (潜在): ~{potential_tokens:,} tok ({len(file_details)}ファイル)")

    # セッション開始時の固定消費
    startup_tokens = total_tokens  # CLAUDE.md + MEMORY.md
    startup_pct = startup_tokens / MAX_CONTEXT_TOKENS * 100

    print(f"\n--- サマリ ---")
    print(f"起動時固定消費: ~{startup_tokens:,} tok ({startup_pct:.1f}%)")
    print(f"潜在消費 (全memory/参照時): ~{potential_tokens:,} tok ({potential_tokens/MAX_CONTEXT_TOKENS*100:.1f}%)")
    print(f"合計潜在: ~{startup_tokens + potential_tokens:,} tok ({(startup_tokens + potential_tokens)/MAX_CONTEXT_TOKENS*100:.1f}%)")

    # 警告判定
    status = "ok"
    recommendations = []

    if startup_pct >= ALERT_PCT:
        status = "alert"
        recommendations.append(f"起動時消費が{startup_pct:.0f}%で高すぎます。CLAUDE.mdの圧縮を検討してください")
    elif startup_pct >= WARN_PCT:
        status = "warning"
        recommendations.append(f"起動時消費が{startup_pct:.0f}%です。CLAUDE.mdが肥大化していないか確認してください")

    # 大きいファイルTOP5
    top_files = sorted(file_details, key=lambda x: x["tokens_est"], reverse=True)[:5]
    if top_files and detail:
        print(f"\n--- トークン消費 TOP5 ---")
        for i, f in enumerate(top_files, 1):
            print(f"  {i}. {f['path']}: ~{f['tokens_est']:,} tok ({f['size_bytes']//1024}KB)")

    if recommendations:
        print(f"\n⚠️ 推奨事項:")
        for r in recommendations:
            print(f"  - {r}")

    print(f"\nStatus: {status.upper()}")

    return {
        "startup_tokens": startup_tokens,
        "startup_pct": round(startup_pct, 1),
        "potential_tokens": potential_tokens,
        "total_memory_files": len(file_details),
        "status": status,
        "recommendations": recommendations,
        "top_files": [{"file": f["path"], "tokens": f["tokens_est"]} for f in top_files],
    }


if __name__ == "__main__":
    detail = "--detail" in sys.argv
    run_check(detail=detail)
