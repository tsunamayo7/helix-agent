"""Department Fine-Tuning Advisor — 部門LLMのファインチューニング提案.

各部門RAGのデータ蓄積状況・ドメイン特性・現状モデルを分析し、
部門特化LLMのファインチューニング提案を自動生成する。

提案内容:
  1. ベースモデル選定 (部門特性に応じて)
  2. データセット品質評価 (十分か?)
  3. ファインチューニング手法 (LoRA/QLoRA/Full)
  4. 必要なGPUリソース推定
  5. トレーニングデータ生成プラン
  6. 期待される改善点

使い方:
    python scripts/dept_ft_advisor.py              # 全部門の提案生成
    python scripts/dept_ft_advisor.py dept_build    # 特定部門のみ
    python scripts/dept_ft_advisor.py --export      # 提案をmemoryに保存
    python scripts/dept_ft_advisor.py status        # 過去の提案履歴
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

QDRANT_URL = "http://localhost:6333"
OLLAMA_URL = "http://localhost:11434"
STATE_DIR = Path.home() / ".helix-agent" / "ft_advisor"
PROPOSALS_FILE = STATE_DIR / "proposals.json"
MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# ---------------------------------------------------------------------------
# 部門ごとの特性定義
# ---------------------------------------------------------------------------

DEPT_PROFILES = {
    "dept_hr": {
        "name": "人事/採用",
        "bias": "市場価値・適合性",
        "required_skills": ["候補者評価", "求人票解析", "キャリアマッチング", "交渉支援"],
        "base_models_ranked": [
            ("gemma3:27b", "汎用バランス、日本語堪能、LoRA適合"),
            ("qwen3:32b", "長文理解、多言語"),
            ("llama3.3:70b", "厳密推論、大規模"),
        ],
        "min_points_for_ft": 500,  # LoRAに必要な最小データ量
        "recommended_method": "LoRA (r=16, alpha=32)",
        "vram_estimate_gb": {"LoRA": 24, "QLoRA": 16, "Full": 120},
    },
    "dept_research": {
        "name": "調査研究",
        "bias": "網羅性・最新性",
        "required_skills": ["ソース横断", "要約", "事実検証", "トレンド分析"],
        "base_models_ranked": [
            ("gemma3:27b", "Web情報統合、要約、バランス型"),
            ("qwen3:32b", "長文コンテキスト、多言語検索"),
            ("mistral-large:123b", "大規模汎用"),
        ],
        "min_points_for_ft": 1000,
        "recommended_method": "LoRA (r=32, alpha=64) — 幅広い知識投入",
        "vram_estimate_gb": {"LoRA": 32, "QLoRA": 20, "Full": 160},
    },
    "dept_design": {
        "name": "設計",
        "bias": "拡張性・保守性",
        "required_skills": ["アーキテクチャ判断", "SOLID原則", "依存性分析", "リファクタリング案"],
        "base_models_ranked": [
            ("qwen3-coder:32b", "コード理解特化、設計パターン認識"),
            ("deepseek-coder-v2:16b", "軽量コード特化、高速"),
            ("gemma3:27b", "汎用、図解・文章説明"),
        ],
        "min_points_for_ft": 800,
        "recommended_method": "LoRA (r=16, alpha=32) + Instruction tuning",
        "vram_estimate_gb": {"LoRA": 28, "QLoRA": 16, "Full": 140},
    },
    "dept_build": {
        "name": "構築",
        "bias": "品質・テスト通過",
        "required_skills": ["実装", "デバッグ", "ビルドエラー修正", "CI/CD構成"],
        "base_models_ranked": [
            ("qwen3-coder:32b", "コード生成最強、ビルドエラー対応"),
            ("deepseek-coder-v2:16b", "軽量・高速、ループ少ない"),
            ("codellama:70b", "コード特化、大規模"),
        ],
        "min_points_for_ft": 800,
        "recommended_method": "LoRA (r=16, alpha=32) on coding traces",
        "vram_estimate_gb": {"LoRA": 28, "QLoRA": 16, "Full": 140},
    },
    "dept_qa": {
        "name": "品質管理",
        "bias": "防御的・最悪ケース",
        "required_skills": ["OWASP Top 10", "テスト設計", "脆弱性スキャン", "コードレビュー"],
        "base_models_ranked": [
            ("llama3.3:70b", "厳密推論、攻撃パターン認識"),
            ("qwen3:32b", "多言語セキュリティ情報、CVE追跡"),
            ("gemma3:27b", "汎用バランス、テスト設計"),
        ],
        "min_points_for_ft": 600,
        "recommended_method": "LoRA (r=32, alpha=64) — 多様な攻撃パターン",
        "vram_estimate_gb": {"LoRA": 32, "QLoRA": 20, "Full": 160},
    },
}

# GPU制約 (RTX 5070 Ti 16GB + RTX PRO 6000 96GB = 112GB total)
AVAILABLE_VRAM_GB = 112
BLACKWELL_NOTE = "Blackwell GPU → PyTorch cu128以上必須"


# ---------------------------------------------------------------------------
# データ収集
# ---------------------------------------------------------------------------


def get_collection_info(name: str) -> dict:
    try:
        with urllib.request.urlopen(f"{QDRANT_URL}/collections/{name}", timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def sample_collection_content(name: str, limit: int = 50) -> list[str]:
    """コレクションからテキストサンプルを取得."""
    try:
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{name}/points/scroll",
            data=json.dumps({"limit": limit, "with_payload": True, "with_vector": False}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        texts = []
        for p in data.get("result", {}).get("points", []):
            payload = p.get("payload", {})
            text = payload.get("data", payload.get("memory", ""))
            if text:
                texts.append(text)
        return texts
    except Exception:
        return []


def analyze_content_diversity(texts: list[str]) -> dict:
    """テキストの多様性・品質を簡易分析."""
    if not texts:
        return {"count": 0, "avg_length": 0, "unique_ratio": 0.0, "lang": "unknown"}

    total_len = sum(len(t) for t in texts)
    avg_len = total_len / len(texts)

    # ユニーク率 (先頭50文字で判定)
    heads = {t[:50] for t in texts}
    unique_ratio = len(heads) / len(texts)

    # 言語判定 (ASCII比率)
    ascii_count = sum(1 for t in texts for c in t[:500] if ord(c) < 128)
    total_count = sum(min(len(t), 500) for t in texts)
    ascii_ratio = ascii_count / total_count if total_count else 0
    lang = "english" if ascii_ratio > 0.9 else "japanese" if ascii_ratio < 0.4 else "mixed"

    return {
        "count": len(texts),
        "avg_length": int(avg_len),
        "unique_ratio": round(unique_ratio, 2),
        "lang": lang,
        "total_chars": total_len,
    }


def get_available_models() -> list[str]:
    """Ollama にインストール済みのモデル一覧."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 提案生成
# ---------------------------------------------------------------------------


def generate_proposal(dept: str) -> dict:
    """部門ごとのファインチューニング提案を生成."""
    profile = DEPT_PROFILES.get(dept)
    if not profile:
        return {"error": f"Unknown department: {dept}"}

    # RAG状態取得
    info = get_collection_info(dept)
    points_count = info.get("result", {}).get("points_count", 0) if info else 0

    # サンプル取得+多様性分析
    samples = sample_collection_content(dept, limit=100)
    diversity = analyze_content_diversity(samples)

    # 利用可能モデルとクロスチェック
    available = get_available_models()

    # 提案作成
    proposal = {
        "department": dept,
        "dept_name_ja": profile["name"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "current_state": {
            "points_count": points_count,
            "min_required": profile["min_points_for_ft"],
            "readiness": "READY" if points_count >= profile["min_points_for_ft"] else "INSUFFICIENT",
            "data_diversity": diversity,
        },
        "recommendation": {},
    }

    # データ不足なら待機提案
    if points_count < profile["min_points_for_ft"]:
        shortage = profile["min_points_for_ft"] - points_count
        proposal["recommendation"] = {
            "action": "WAIT",
            "reason": f"データ不足 ({points_count}/{profile['min_points_for_ft']})",
            "shortage": shortage,
            "suggestion": f"{shortage}ポイント追加投入後に再評価。x-feed-collector の{dept}関連カテゴリ拡充を推奨。",
        }
        return proposal

    # 推奨モデル選定 (利用可能性チェック)
    recommended_models = []
    for model_name, reason in profile["base_models_ranked"]:
        base_name = model_name.split(":")[0]
        is_available = any(base_name in m for m in available)
        recommended_models.append({
            "model": model_name,
            "reason": reason,
            "installed": is_available,
            "install_command": f"ollama pull {model_name}" if not is_available else None,
        })

    # VRAM判定 (最大モデルがフィットするか)
    vram = profile["vram_estimate_gb"]
    fits_in_vram = {
        method: f"{gb}GB " + ("✓ fit in 96GB PRO 6000" if gb <= 96 else
                              "✓ fit in combined 112GB" if gb <= AVAILABLE_VRAM_GB else
                              "✗ insufficient")
        for method, gb in vram.items()
    }

    # データセット生成プラン
    dataset_plan = {
        "source": f"Qdrant {dept} コレクション",
        "size_estimate": f"{points_count * 2}～{points_count * 5}件のinstruction-output pair",
        "generation_method": [
            "1. RAGデータをQ&A形式に変換 (gemma4:31bで自動生成)",
            "2. 重複排除 (content_hash)",
            "3. 品質フィルタ (relevance_score >= 5)",
            f"4. {profile['bias']}の思考スタイルに合わせたプロンプト整形",
        ],
        "format": "JSONL (ShareGPT/Alpaca形式)",
    }

    proposal["recommendation"] = {
        "action": "PROCEED",
        "base_models": recommended_models,
        "method": profile["recommended_method"],
        "vram_analysis": fits_in_vram,
        "dataset_plan": dataset_plan,
        "estimated_improvements": [
            f"応答スタイル: 汎用から{profile['bias']}志向へ",
            f"専門知識: {', '.join(profile['required_skills'])}",
            f"応答速度: LoRA merge後は推論時オーバーヘッドなし",
        ],
        "risks": [
            "過学習: 部門RAGのデータ偏りを吸収してしまう",
            "汎用能力低下: 他ドメインの質問への対応力が下がる可能性",
            "メンテナンス: モデル更新時に再トレーニング必要",
        ],
        "next_steps": [
            "1. データセット生成スクリプト作成 (dept_dataset_builder.py)",
            "2. ベースモデル選定・ダウンロード確認",
            "3. Unsloth/axolotl等でLoRAトレーニング",
            "4. 評価 (部門タスクでのA/Bテスト)",
            "5. Ollama/VLLM等でデプロイ",
        ],
        "note": BLACKWELL_NOTE,
    }

    return proposal


def save_proposals(proposals: list[dict]) -> None:
    """提案をJSONファイルに保存."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    if PROPOSALS_FILE.exists():
        try:
            existing = json.loads(PROPOSALS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing.extend(proposals)
    # 過去30件のみ保持
    PROPOSALS_FILE.write_text(
        json.dumps(existing[-30:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def export_to_memory(proposals: list[dict]) -> Path:
    """提案をmarkdownとしてmemory/に保存."""
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = MEMORY_DIR / f"project_ft_proposals_{ts}.md"

    lines = [
        "---",
        "name: 部門LLMファインチューニング提案",
        f"description: {datetime.now().strftime('%Y-%m-%d')} 時点の5部門FT提案",
        "type: project",
        "---",
        "",
        f"# 部門LLMファインチューニング提案 ({datetime.now().strftime('%Y-%m-%d')})",
        "",
        "## 概要",
        "",
        "Helix Corp 5部門のRAG蓄積状況に基づくファインチューニング提案。",
        "",
    ]

    for prop in proposals:
        dept = prop.get("department", "?")
        name_ja = prop.get("dept_name_ja", "?")
        current = prop.get("current_state", {})
        rec = prop.get("recommendation", {})

        lines.append(f"## {name_ja} ({dept})")
        lines.append("")
        lines.append(f"**データ状況**: {current.get('points_count', 0)} / {current.get('min_required', 0)} points ({current.get('readiness', '?')})")
        lines.append("")

        div = current.get("data_diversity", {})
        lines.append(f"- 平均文字数: {div.get('avg_length', 0)}")
        lines.append(f"- ユニーク率: {div.get('unique_ratio', 0)}")
        lines.append(f"- 主言語: {div.get('lang', '?')}")
        lines.append("")

        action = rec.get("action", "?")
        lines.append(f"**判定**: {action}")
        lines.append("")

        if action == "WAIT":
            lines.append(f"- 理由: {rec.get('reason', '')}")
            lines.append(f"- 不足: {rec.get('shortage', 0)}ポイント")
            lines.append(f"- 提案: {rec.get('suggestion', '')}")
        else:
            lines.append("**推奨ベースモデル**:")
            for m in rec.get("base_models", []):
                marker = "✓" if m.get("installed") else "✗ (要pull)"
                lines.append(f"- {marker} `{m['model']}` — {m['reason']}")
            lines.append("")
            lines.append(f"**手法**: {rec.get('method', '?')}")
            lines.append("")
            lines.append("**VRAM分析**:")
            for method, analysis in rec.get("vram_analysis", {}).items():
                lines.append(f"- {method}: {analysis}")
            lines.append("")
            lines.append("**期待される改善**:")
            for imp in rec.get("estimated_improvements", []):
                lines.append(f"- {imp}")
            lines.append("")
            lines.append("**リスク**:")
            for risk in rec.get("risks", []):
                lines.append(f"- {risk}")
            lines.append("")
            lines.append("**次のステップ**:")
            for step in rec.get("next_steps", []):
                lines.append(f"- {step}")
            lines.append("")
            if rec.get("note"):
                lines.append(f"> **注意**: {rec['note']}")
                lines.append("")

        lines.append("---")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def notify_discord(proposals: list[dict]) -> None:
    ready = [p for p in proposals if p.get("recommendation", {}).get("action") == "PROCEED"]
    waiting = [p for p in proposals if p.get("recommendation", {}).get("action") == "WAIT"]
    lines = [
        f"🎓 **部門LLMファインチューニング提案**",
        f"準備完了: {len(ready)}部門 | データ不足: {len(waiting)}部門",
        "",
    ]
    for p in ready[:5]:
        dept_name = p.get("dept_name_ja", "?")
        pts = p.get("current_state", {}).get("points_count", 0)
        top_model = p.get("recommendation", {}).get("base_models", [{}])[0].get("model", "?")
        lines.append(f"✅ {dept_name}: {pts}pts → {top_model} (LoRA)")
    for p in waiting[:3]:
        dept_name = p.get("dept_name_ja", "?")
        shortage = p.get("recommendation", {}).get("shortage", 0)
        lines.append(f"⏳ {dept_name}: あと{shortage}pts必要")
    msg = "\n".join(lines)
    try:
        subprocess.run(
            ["python", str(WEBHOOK_SCRIPT), msg],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def print_proposal(prop: dict) -> None:
    dept = prop.get("department", "?")
    name_ja = prop.get("dept_name_ja", "?")
    current = prop.get("current_state", {})
    rec = prop.get("recommendation", {})

    print(f"\n=== {name_ja} ({dept}) ===")
    print(f"  Points: {current.get('points_count', 0)} / {current.get('min_required', 0)}")
    print(f"  Readiness: {current.get('readiness', '?')}")
    div = current.get("data_diversity", {})
    print(f"  Diversity: avg_len={div.get('avg_length', 0)}, unique={div.get('unique_ratio', 0)}, lang={div.get('lang', '?')}")
    print(f"  Action: {rec.get('action', '?')}")

    if rec.get("action") == "WAIT":
        print(f"    Shortage: {rec.get('shortage', 0)} points")
        print(f"    Suggestion: {rec.get('suggestion', '')}")
    else:
        print("  Recommended models:")
        for m in rec.get("base_models", []):
            mark = "[installed]" if m.get("installed") else "[need pull]"
            print(f"    {mark} {m['model']} — {m['reason']}")
        print(f"  Method: {rec.get('method', '?')}")
        print("  VRAM:")
        for method, analysis in rec.get("vram_analysis", {}).items():
            print(f"    {method}: {analysis}")


def main():
    export = "--export" in sys.argv
    json_out = "--json" in sys.argv
    dept_only = None
    if len(sys.argv) > 1 and sys.argv[1].startswith("dept_"):
        dept_only = sys.argv[1]
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        # 履歴表示
        if PROPOSALS_FILE.exists():
            data = json.loads(PROPOSALS_FILE.read_text(encoding="utf-8"))
            print(f"提案履歴: {len(data)}件")
            for p in data[-5:]:
                print(f"  [{p.get('timestamp', '')[:19]}] {p.get('department', '?')}: {p.get('recommendation', {}).get('action', '?')}")
        else:
            print("提案履歴なし")
        return

    target_depts = [dept_only] if dept_only else list(DEPT_PROFILES.keys())
    proposals = []
    for d in target_depts:
        prop = generate_proposal(d)
        proposals.append(prop)
        if not json_out:
            print_proposal(prop)

    save_proposals(proposals)

    if json_out:
        print(json.dumps(proposals, ensure_ascii=False, indent=2))

    if export:
        path = export_to_memory(proposals)
        print(f"\nExported: {path}")
        notify_discord(proposals)


if __name__ == "__main__":
    main()
