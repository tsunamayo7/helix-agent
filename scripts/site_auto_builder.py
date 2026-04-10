"""Webサイト自動成長パイプライン.

優秀なOSSサイトからデザインパターンを学習し、
helix-*プロジェクトのLPを自動改善するオーケストレーター。

3層アーキテクチャ:
  L1: gemma4 ($0) — サイトスキャン、パターン抽出、コンテンツ下書き
  L2: Sonnet Agent — コード生成、デザイン判断、品質レビュー
  L3: Opus — 最終判断、デプロイ承認

使い方（Claude Codeセッション内で呼び出し）:
    python scripts/site_auto_builder.py scan          # 参考サイトスキャン
    python scripts/site_auto_builder.py update        # LP自動更新
    python scripts/site_auto_builder.py deploy        # デプロイ（要承認）
    python scripts/site_auto_builder.py status        # 現在の状態
    python scripts/site_auto_builder.py report        # 改善レポート生成
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------------
# パス設定
# ---------------------------------------------------------------------------

SITES_DIR = Path("C:/Development/workspace/generated-sites")
PATTERNS_DIR = Path.home() / ".helix-agent" / "site_patterns"
REPORT_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory" / "content"

# 管理対象サイト
MANAGED_SITES = {
    "helix-agent": {
        "dir": SITES_DIR / "helix-demo",  # 将来リネーム予定
        "repo": "tsunamayo7/helix-agent",
        "description": "ローカルOllama委譲MCPサーバー",
        "deploy_url": None,  # Vercel/Render URL
    },
}

# 参考にするOSSサイト
REFERENCE_SITES = [
    {"name": "Cursor", "url": "https://cursor.com", "stars": "100K+", "category": "AI Editor"},
    {"name": "Supabase", "url": "https://supabase.com", "stars": "80K+", "category": "BaaS"},
    {"name": "Vercel", "url": "https://vercel.com", "stars": "N/A", "category": "Deploy"},
    {"name": "Linear", "url": "https://linear.app", "stars": "N/A", "category": "PM"},
    {"name": "Suna", "url": "https://suna.so", "stars": "18K+", "category": "AI Agent"},
    {"name": "AFFiNE", "url": "https://affine.pro", "stars": "50K+", "category": "Knowledge"},
]

# デザインパターンカテゴリ
PATTERN_CATEGORIES = [
    "hero_section",      # ヒーローエリア（キャッチコピー+CTA）
    "social_proof",      # Star badge、testimonials
    "feature_grid",      # 機能一覧のレイアウト
    "demo_showcase",     # GIF/動画の配置
    "metrics_display",   # 数値の見せ方
    "color_scheme",      # カラー設計
    "cta_design",        # CTAボタンの文言と配置
]


# ---------------------------------------------------------------------------
# パターン蓄積
# ---------------------------------------------------------------------------

def load_patterns() -> dict:
    """蓄積済みデザインパターンを読み込む."""
    patterns_file = PATTERNS_DIR / "patterns.json"
    if patterns_file.exists():
        return json.loads(patterns_file.read_text(encoding="utf-8"))
    return {"patterns": [], "last_scan": None, "scan_count": 0}


def save_patterns(data: dict):
    """デザインパターンを保存."""
    PATTERNS_DIR.mkdir(parents=True, exist_ok=True)
    patterns_file = PATTERNS_DIR / "patterns.json"
    patterns_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_pattern(category: str, name: str, description: str, source: str):
    """パターンを追加."""
    data = load_patterns()
    pattern = {
        "category": category,
        "name": name,
        "description": description,
        "source": source,
        "added_at": datetime.now(JST).isoformat(),
    }
    # 重複チェック
    existing = [p for p in data["patterns"] if p["name"] == name and p["category"] == category]
    if not existing:
        data["patterns"].append(pattern)
        save_patterns(data)
    return pattern


# ---------------------------------------------------------------------------
# ステータス
# ---------------------------------------------------------------------------

def show_status():
    """現在の状態を表示."""
    data = load_patterns()
    print(f"=== Site Auto Builder ===")
    print(f"  パターン数: {len(data['patterns'])}")
    print(f"  最終スキャン: {data.get('last_scan', 'なし')}")
    print(f"  スキャン回数: {data.get('scan_count', 0)}")
    print()
    print(f"=== 管理対象サイト ===")
    for name, config in MANAGED_SITES.items():
        exists = config["dir"].exists()
        print(f"  {name}: {'OK' if exists else 'NG'} {config['dir']}")
        if config.get("deploy_url"):
            print(f"    Deploy: {config['deploy_url']}")
    print()
    print(f"=== 参考サイト ===")
    for site in REFERENCE_SITES:
        print(f"  {site['name']} ({site['stars']}): {site['url']}")
    print()
    # カテゴリ別パターン数
    if data["patterns"]:
        print(f"=== パターン分布 ===")
        from collections import Counter
        cats = Counter(p["category"] for p in data["patterns"])
        for cat in PATTERN_CATEGORIES:
            print(f"  {cat}: {cats.get(cat, 0)}件")


def show_report():
    """改善レポートを生成."""
    report_file = REPORT_DIR / "site_improvement_report.md"
    if report_file.exists():
        print(report_file.read_text(encoding="utf-8"))
    else:
        print("レポートなし。'scan' を先に実行してください。")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("使い方: python scripts/site_auto_builder.py [scan|update|deploy|status|report]")
        return

    cmd = sys.argv[1]

    if cmd == "status":
        show_status()
    elif cmd == "report":
        show_report()
    elif cmd == "scan":
        print("スキャンはClaude Codeセッション内でSonnet Agentに委譲して実行します。")
        print("Claude Code内で: Agent(model='sonnet', prompt='site_auto_builder scan ...')")
    elif cmd == "update":
        print("更新はClaude Codeセッション内でSonnet Agentに委譲して実行します。")
    elif cmd == "deploy":
        print("デプロイはOpusの承認が必要です。Claude Codeセッション内で実行してください。")
    else:
        print(f"不明なコマンド: {cmd}")


if __name__ == "__main__":
    main()
