"""X(Twitter) 自己成長RAGシステム.

投稿分析・効果測定の結果をQdrantに蓄積し、
次回の投稿作成時にRAGで参照することで精度が日々向上する。

使い方:
    uv run python scripts/x_rag_learner.py analyze
    uv run python scripts/x_rag_learner.py record --url <tweet_url> --impressions 500 --likes 10
    uv run python scripts/x_rag_learner.py context --topic "Claude Code token saving"
    uv run python scripts/x_rag_learner.py patterns
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# src/ を import path に追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gpu_detect import auto_select_model
from ollama_client import OllamaClient

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

QDRANT_URL = "http://localhost:6333"
QDRANT_COLLECTION = "mem0_shared"
OLLAMA_URL = "http://localhost:11434"
EMBEDDING_MODEL = "qwen3-embedding:8b"
EMBEDDING_DIM = 4096

USER_ID = "tsunamayo7"
SOURCE_TAG = "x-rag-learner"

X_MONITOR_DIR = Path.home() / ".helix-agent" / "x_monitor"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [x_rag_learner] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------------

async def get_embedding(text: str) -> list[float] | None:
    """Ollama経由でqwen3-embedding:8bの埋め込みベクトルを取得."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": EMBEDDING_MODEL, "input": text},
            )
            r.raise_for_status()
            embeddings = r.json().get("embeddings", [])
            if embeddings:
                return embeddings[0]
            log.warning("埋め込みレスポンスが空です")
            return None
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        log.warning("Ollama埋め込み取得失敗 (接続): %s", e)
        return None
    except Exception as e:
        log.error("埋め込み取得失敗: %s", e)
        return None


async def qdrant_upsert(points: list[dict]) -> bool:
    """QdrantにポイントをUpsertする."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.put(
                f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points",
                json={"points": points},
            )
            r.raise_for_status()
            log.info("Qdrant upsert 成功: %d ポイント", len(points))
            return True
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        log.warning("Qdrant接続失敗: %s", e)
        return False
    except Exception as e:
        log.error("Qdrant upsert失敗: %s", e)
        return False


async def qdrant_search(
    vector: list[float],
    filter_conditions: dict | None = None,
    limit: int = 10,
) -> list[dict]:
    """Qdrantでベクトル検索を実行."""
    payload: dict[str, Any] = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
    }
    if filter_conditions:
        payload["filter"] = filter_conditions

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/search",
                json=payload,
            )
            r.raise_for_status()
            return r.json().get("result", [])
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        log.warning("Qdrant検索失敗 (接続): %s", e)
        return []
    except Exception as e:
        log.error("Qdrant検索失敗: %s", e)
        return []


def make_point(
    text: str,
    vector: list[float],
    metadata: dict,
) -> dict:
    """Qdrantポイントを生成."""
    payload = {
        "source": SOURCE_TAG,
        "user_id": USER_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
        **metadata,
    }
    return {
        "id": str(uuid.uuid4()),
        "vector": vector,
        "payload": payload,
    }


MODEL_FALLBACK_CHAIN = ["gemma4:31b", "gemma4:26b", "gemma4:e4b", "gemma4:e2b"]

# モデルごとの推定VRAM使用量 (MB)
MODEL_VRAM_MB = {
    "gemma4:31b": 20000,
    "gemma4:26b": 12000,
    "gemma4:e4b": 6000,
    "gemma4:e2b": 4000,
}


def get_max_free_vram_mb() -> int:
    """nvidia-smiで全GPUの空きVRAMを確認し、最大値をMBで返す."""
    import subprocess
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return 0
        free_list = [int(x.strip()) for x in result.stdout.strip().split("\n") if x.strip()]
        return max(free_list) if free_list else 0
    except Exception as e:
        log.warning("VRAM確認失敗: %s", e)
        return 0


def select_model_by_vram(available_models: set[str]) -> str | None:
    """空きVRAMに基づいて最適なモデルを選択.

    nvidia-smiで空きVRAMを確認し、確実に動くモデルを選ぶ。
    e2bすら動かないVRAM状況なら None を返す（30分後の再実行を待つ）。
    """
    free_mb = get_max_free_vram_mb()
    if free_mb == 0:
        log.warning("VRAM情報取得不可。フォールバックチェーンで試行します。")
        return next((m for m in MODEL_FALLBACK_CHAIN if m in available_models), None)

    log.info("空きVRAM: %d MB", free_mb)

    # VRAM余裕を持って選択（推定量の1.2倍を要求）
    for model in MODEL_FALLBACK_CHAIN:
        required = int(MODEL_VRAM_MB.get(model, 99999) * 1.2)
        if model in available_models and free_mb >= required:
            log.info("VRAMベース選択: %s (必要: %dMB, 空き: %dMB)", model, required, free_mb)
            return model

    log.warning("空きVRAM %dMB — e2bすら不足。30分後の再実行を待ちます。", free_mb)
    return None


async def get_ollama_model() -> tuple[OllamaClient, str] | None:
    """利用可能なOllamaモデルを取得（VRAMベース選択 + フォールバック）."""
    client = OllamaClient(timeout=300.0)
    if not await client.is_available():
        log.warning("Ollamaが起動していません。スキップします。")
        return None

    available = {m["name"] for m in await client.list_models()}

    # Step 1: VRAMベースで最適モデルを選択
    model = select_model_by_vram(available)
    if model is None:
        return None

    log.info("使用モデル: %s", model)
    return client, model


async def chat_with_oom_fallback(
    client: OllamaClient,
    model: str,
    messages: list[dict],
    temperature: float = 0.3,
) -> str | None:
    """OOM/VRAM不足時に自動的に小さいモデルにフォールバックするchat.

    事前にVRAMチェック済みだが、他プロセスが割り込む可能性があるため
    実行時OOMにも対応する。
    """
    available = {m["name"] for m in await client.list_models()}
    try:
        start_idx = MODEL_FALLBACK_CHAIN.index(model)
    except ValueError:
        start_idx = 0
    models_to_try = [model] + [
        m for m in MODEL_FALLBACK_CHAIN[start_idx + 1:] if m in available
    ]

    for try_model in models_to_try:
        try:
            resp = await client.chat(
                model=try_model,
                messages=messages,
                options={"temperature": temperature},
            )
            if try_model != model:
                log.info("OOMフォールバック: %s → %s で成功", model, try_model)
            return resp
        except Exception as e:
            err_str = str(e).lower()
            if "out of memory" in err_str or "oom" in err_str or "insufficient" in err_str or "timeout" in err_str:
                log.warning("VRAM不足/タイムアウト (%s)、小さいモデルで再試行...", try_model)
                continue
            else:
                log.error("LLM呼び出しエラー (%s): %s", try_model, e)
                return None

    log.error("すべてのモデルで失敗。30分後の再実行を待ちます。")
    return None


# ---------------------------------------------------------------------------
# A. 分析結果の蓄積 (analyze_and_store)
# ---------------------------------------------------------------------------

ANALYZE_PROMPT = """\
以下のX監視データを分析し、投稿戦略に有用なパターンを抽出してください。
JSON形式で出力してください（他のテキスト不要）。

フォーマット:
{
  "summary": "全体のまとめ（2-3文）",
  "topics": ["トピック1", "トピック2"],
  "patterns": [
    {
      "pattern": "パターンの説明",
      "relevance": "high/medium/low",
      "actionable_insight": "具体的なアクション"
    }
  ],
  "trending_themes": ["テーマ1", "テーマ2"],
  "recommended_topics_to_post": ["投稿すべきトピック1", "投稿すべきトピック2"]
}
"""


async def analyze_and_store(days: int = 7) -> int:
    """x_monitorのJSON結果を分析してQdrantに蓄積."""
    # x_monitorデータ読み込み
    if not X_MONITOR_DIR.exists():
        log.info("x_monitorデータなし: %s", X_MONITOR_DIR)
        return 0

    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=days)
    all_entries: list[dict] = []

    for fp in sorted(X_MONITOR_DIR.glob("x_monitor_*.json")):
        stem = fp.stem.replace("x_monitor_", "")
        try:
            file_dt = datetime.strptime(stem, "%Y%m%d_%H%M")
            if file_dt < cutoff:
                continue
        except ValueError:
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, list):
                all_entries.extend(data)
        except (json.JSONDecodeError, OSError):
            continue

    if not all_entries:
        log.info("分析対象のエントリなし（%d日以内）", days)
        return 0

    log.info("分析対象: %d エントリ（%d日分）", len(all_entries), days)

    # Ollamaモデル取得
    result = await get_ollama_model()
    if result is None:
        return 0
    client, model = result

    # gemma4で分析（VRAM不足時は自動フォールバック）
    entries_text = json.dumps(all_entries[:30], ensure_ascii=False, indent=1)
    response = await chat_with_oom_fallback(
        client, model,
        messages=[
            {"role": "system", "content": ANALYZE_PROMPT},
            {"role": "user", "content": f"データ:\n{entries_text}"},
        ],
    )

    # JSON抽出
    analysis = _extract_json(response)
    if analysis is None:
        log.error("分析結果のJSON解析失敗")
        return 0

    summary = analysis.get("summary", response[:500])
    topics = analysis.get("topics", [])

    # 埋め込み生成
    embed_text = f"X分析: {summary}\nトピック: {', '.join(topics)}"
    vector = await get_embedding(embed_text)
    if vector is None:
        return 0

    # Qdrant保存
    point = make_point(
        text=embed_text,
        vector=vector,
        metadata={
            "type": "x_analysis",
            "topics": topics,
            "content_summary": summary,
            "entry_count": len(all_entries),
            "trending_themes": analysis.get("trending_themes", []),
            "recommended_topics": analysis.get("recommended_topics_to_post", []),
            "patterns": json.dumps(
                analysis.get("patterns", []), ensure_ascii=False
            ),
        },
    )
    success = await qdrant_upsert([point])
    if success:
        log.info("分析結果をQdrantに保存しました (トピック: %s)", ", ".join(topics))
        return 1
    return 0


# ---------------------------------------------------------------------------
# B. 投稿効果の記録 (record_post_performance)
# ---------------------------------------------------------------------------

RECORD_ANALYZE_PROMPT = """\
以下のX投稿のパフォーマンスデータを分析してください。
JSON形式で出力してください:

{
  "why_performed": "この投稿が伸びた/伸びなかった理由（2-3文）",
  "effective_elements": ["効果的だった要素1", "要素2"],
  "improvement_areas": ["改善点1", "改善点2"],
  "content_type": "技術紹介/ニュース/意見/Tips/告知/その他",
  "tone": "カジュアル/専門的/教育的/ユーモア/その他"
}
"""


async def record_post_performance(
    url: str,
    text: str = "",
    impressions: int = 0,
    likes: int = 0,
    retweets: int = 0,
    replies: int = 0,
    bookmarks: int = 0,
) -> bool:
    """投稿のパフォーマンスを記録."""
    engagement_total = likes + retweets + replies + bookmarks
    engagement_rate = engagement_total / impressions if impressions > 0 else 0.0

    log.info(
        "投稿記録: %s (imp=%d, eng_rate=%.4f)",
        url, impressions, engagement_rate,
    )

    # gemma4で分析
    result = await get_ollama_model()
    analysis_text = ""
    analysis_data: dict[str, Any] = {}

    if result is not None:
        client, model = result
        perf_info = (
            f"URL: {url}\n"
            f"テキスト: {text or '(未提供)'}\n"
            f"インプレッション: {impressions}\n"
            f"いいね: {likes}, RT: {retweets}, リプライ: {replies}, ブックマーク: {bookmarks}\n"
            f"エンゲージメント率: {engagement_rate:.4f}"
        )
        try:
            response = await chat_with_oom_fallback(
                client, model,
                messages=[
                    {"role": "system", "content": RECORD_ANALYZE_PROMPT},
                    {"role": "user", "content": perf_info},
                ],
            )
            analysis_data = _extract_json(response) or {}
            analysis_text = analysis_data.get("why_performed", response[:300])
        except Exception as e:
            log.warning("分析スキップ: %s", e)
            analysis_text = f"imp={impressions}, likes={likes}, rt={retweets}"

    # 埋め込み生成
    embed_text = (
        f"X投稿パフォーマンス: {text or url}\n"
        f"エンゲージメント率: {engagement_rate:.4f}\n"
        f"分析: {analysis_text}"
    )
    vector = await get_embedding(embed_text)
    if vector is None:
        return False

    # Qdrant保存
    point = make_point(
        text=embed_text,
        vector=vector,
        metadata={
            "type": "x_post_performance",
            "url": url,
            "post_text": text[:500] if text else "",
            "impressions": impressions,
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "bookmarks": bookmarks,
            "engagement_rate": round(engagement_rate, 6),
            "content_summary": analysis_text,
            "effective_elements": analysis_data.get("effective_elements", []),
            "improvement_areas": analysis_data.get("improvement_areas", []),
            "content_type": analysis_data.get("content_type", ""),
            "tone": analysis_data.get("tone", ""),
        },
    )
    success = await qdrant_upsert([point])
    if success:
        log.info("投稿パフォーマンスをQdrantに保存しました")
    return success


# ---------------------------------------------------------------------------
# C. 投稿前RAGクエリ (get_posting_context)
# ---------------------------------------------------------------------------

DRAFT_PROMPT = """\
以下の過去データを参考に、指定トピックについてX(Twitter)投稿の草案を作成してください。

ルール:
- 280文字以内（日本語の場合140文字目安）
- 過去に効果が高かったパターンを活用
- ハッシュタグは1-3個
- 具体的な数値やデモ情報を含めると効果的

JSON形式で出力:
{
  "drafts": [
    {
      "text": "投稿テキスト",
      "strategy": "この草案の戦略",
      "expected_engagement": "high/medium/low"
    }
  ],
  "context_summary": "過去データから得た知見のまとめ",
  "best_posting_time": "推奨投稿時間帯",
  "warnings": ["注意点"]
}
"""


async def get_posting_context(topic: str, limit: int = 10) -> dict[str, Any]:
    """トピックに関連する過去の分析・パフォーマンスデータを検索し、投稿草案を生成."""
    log.info("RAGクエリ: トピック='%s'", topic)

    # 埋め込み生成
    vector = await get_embedding(f"X投稿 {topic}")
    if vector is None:
        return {"error": "埋め込み生成失敗"}

    # Qdrantから検索（x-rag-learnerのデータのみ）
    filter_cond = {
        "must": [
            {"key": "source", "match": {"value": SOURCE_TAG}},
        ],
    }
    results = await qdrant_search(vector, filter_cond, limit=limit)

    if not results:
        log.info("過去データなし。初回のためコンテキストなしで草案生成します。")

    # 検索結果を整理
    analyses = []
    performances = []
    patterns = []

    for r in results:
        payload = r.get("payload", {})
        rtype = payload.get("type", "")
        if rtype == "x_analysis":
            analyses.append({
                "summary": payload.get("content_summary", ""),
                "topics": payload.get("topics", []),
                "trending": payload.get("trending_themes", []),
                "recommended": payload.get("recommended_topics", []),
            })
        elif rtype == "x_post_performance":
            performances.append({
                "text": payload.get("post_text", ""),
                "engagement_rate": payload.get("engagement_rate", 0),
                "impressions": payload.get("impressions", 0),
                "analysis": payload.get("content_summary", ""),
                "effective": payload.get("effective_elements", []),
            })
        elif rtype == "x_winning_pattern":
            patterns.append({
                "summary": payload.get("content_summary", ""),
            })

    context = {
        "topic": topic,
        "past_analyses": analyses,
        "past_performances": performances,
        "winning_patterns": patterns,
        "total_references": len(results),
    }

    # gemma4で草案生成
    result = await get_ollama_model()
    if result is not None:
        client, model = result
        context_text = json.dumps(context, ensure_ascii=False, indent=1)
        try:
            response = await chat_with_oom_fallback(
                client, model,
                messages=[
                    {"role": "system", "content": DRAFT_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"トピック: {topic}\n\n"
                            f"過去データ:\n{context_text}"
                        ),
                    },
                ],
                temperature=0.5,
                num_ctx=8192,
            )
            draft_data = _extract_json(response)
            if draft_data:
                context["drafts"] = draft_data.get("drafts", [])
                context["context_summary"] = draft_data.get("context_summary", "")
                context["best_posting_time"] = draft_data.get("best_posting_time", "")
                context["warnings"] = draft_data.get("warnings", [])
        except Exception as e:
            log.warning("草案生成スキップ: %s", e)

    return context


# ---------------------------------------------------------------------------
# D. 成功パターン抽出 (extract_winning_patterns)
# ---------------------------------------------------------------------------

PATTERN_PROMPT = """\
以下の高エンゲージメント投稿データから成功パターンを抽出してください。
JSON形式で出力:

{
  "patterns": [
    {
      "category": "時間帯/文体/トピック/構成/ハッシュタグ",
      "description": "パターンの説明",
      "evidence": "根拠となるデータ",
      "confidence": "high/medium/low",
      "actionable_advice": "具体的なアドバイス"
    }
  ],
  "summary": "全体的な傾向のまとめ（3-5文）",
  "top_performing_elements": ["要素1", "要素2", "要素3"],
  "avoid_list": ["避けるべきこと1", "避けるべきこと2"]
}
"""


async def extract_winning_patterns() -> int:
    """高エンゲージメント投稿のパターンを抽出してQdrantに保存."""
    log.info("成功パターン抽出を開始...")

    # Qdrantから全パフォーマンスデータを取得
    # ダミーベクトルで全件検索（engagement_rateでフィルタしたいが、
    # セマンティック検索なので「高パフォーマンス投稿」で検索）
    vector = await get_embedding("高エンゲージメント X投稿 成功パターン バズ")
    if vector is None:
        return 0

    filter_cond = {
        "must": [
            {"key": "source", "match": {"value": SOURCE_TAG}},
            {"key": "type", "match": {"value": "x_post_performance"}},
        ],
    }
    results = await qdrant_search(vector, filter_cond, limit=50)

    if not results:
        log.info("パフォーマンスデータなし。先にrecordコマンドでデータを蓄積してください。")
        return 0

    # エンゲージメント率でソート
    perf_data = []
    for r in results:
        p = r.get("payload", {})
        perf_data.append({
            "text": p.get("post_text", ""),
            "impressions": p.get("impressions", 0),
            "engagement_rate": p.get("engagement_rate", 0),
            "effective_elements": p.get("effective_elements", []),
            "content_type": p.get("content_type", ""),
            "tone": p.get("tone", ""),
            "analysis": p.get("content_summary", ""),
        })

    perf_data.sort(key=lambda x: x.get("engagement_rate", 0), reverse=True)
    log.info("分析対象: %d 件のパフォーマンスデータ", len(perf_data))

    # gemma4でパターン抽出
    result = await get_ollama_model()
    if result is None:
        return 0
    client, model = result

    perf_text = json.dumps(perf_data[:20], ensure_ascii=False, indent=1)
    response = await chat_with_oom_fallback(
        client, model,
        messages=[
            {"role": "system", "content": PATTERN_PROMPT},
            {"role": "user", "content": f"投稿パフォーマンスデータ:\n{perf_text}"},
        ],
    )

    pattern_data = _extract_json(response)
    if pattern_data is None:
        log.error("パターン抽出結果のJSON解析失敗")
        return 0

    summary = pattern_data.get("summary", response[:500])
    patterns = pattern_data.get("patterns", [])

    # 埋め込み生成
    embed_text = (
        f"X投稿 成功パターン: {summary}\n"
        f"トップ要素: {', '.join(pattern_data.get('top_performing_elements', []))}"
    )
    vector = await get_embedding(embed_text)
    if vector is None:
        return 0

    # Qdrant保存
    point = make_point(
        text=embed_text,
        vector=vector,
        metadata={
            "type": "x_winning_pattern",
            "content_summary": summary,
            "patterns": json.dumps(patterns, ensure_ascii=False),
            "top_elements": pattern_data.get("top_performing_elements", []),
            "avoid_list": pattern_data.get("avoid_list", []),
            "data_count": len(perf_data),
        },
    )
    success = await qdrant_upsert([point])
    if success:
        log.info("成功パターンをQdrantに保存しました (%d パターン)", len(patterns))
        return 1
    return 0


# ---------------------------------------------------------------------------
# JSON解析ヘルパー
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict | None:
    """テキストからJSONオブジェクトを抽出."""
    import re as _re

    text = text.strip()

    # そのままパース
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return None
    except json.JSONDecodeError:
        pass

    # Markdownコードブロック除去
    match = _re.search(r"```(?:json)?\s*\n?(.*?)```", text, _re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # 最初の { から最後の } まで
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        try:
            parsed = json.loads(text[first:last + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_context_output(ctx: dict) -> str:
    """get_posting_contextの結果を見やすく整形."""
    lines: list[str] = []
    lines.append(f"=== RAGコンテキスト: {ctx.get('topic', '?')} ===")
    lines.append(f"参照データ数: {ctx.get('total_references', 0)}")
    lines.append("")

    if ctx.get("context_summary"):
        lines.append(f"[知見まとめ] {ctx['context_summary']}")
        lines.append("")

    if ctx.get("best_posting_time"):
        lines.append(f"[推奨投稿時間] {ctx['best_posting_time']}")
        lines.append("")

    drafts = ctx.get("drafts", [])
    if drafts:
        lines.append(f"--- 投稿草案 ({len(drafts)}件) ---")
        for i, d in enumerate(drafts, 1):
            lines.append(f"  [{i}] {d.get('text', '')}")
            lines.append(f"      戦略: {d.get('strategy', '?')}")
            lines.append(f"      予想効果: {d.get('expected_engagement', '?')}")
            lines.append("")

    warnings = ctx.get("warnings", [])
    if warnings:
        lines.append("--- 注意点 ---")
        for w in warnings:
            lines.append(f"  - {w}")
        lines.append("")

    past_perf = ctx.get("past_performances", [])
    if past_perf:
        lines.append(f"--- 過去の類似投稿パフォーマンス ({len(past_perf)}件) ---")
        for p in past_perf[:5]:
            er = p.get("engagement_rate", 0)
            lines.append(
                f"  imp={p.get('impressions', 0)}, "
                f"eng_rate={er:.4f}: {p.get('text', '')[:80]}"
            )
        lines.append("")

    return "\n".join(lines)


async def cmd_analyze(args: argparse.Namespace) -> None:
    count = await analyze_and_store(days=args.days)
    log.info("完了。蓄積数: %d", count)


async def cmd_record(args: argparse.Namespace) -> None:
    success = await record_post_performance(
        url=args.url,
        text=args.text or "",
        impressions=args.impressions,
        likes=args.likes,
        retweets=args.retweets,
        replies=args.replies,
        bookmarks=args.bookmarks,
    )
    log.info("記録結果: %s", "成功" if success else "失敗")


async def cmd_context(args: argparse.Namespace) -> None:
    ctx = await get_posting_context(topic=args.topic, limit=args.limit)
    if "error" in ctx:
        log.error("エラー: %s", ctx["error"])
        return
    if args.json_output:
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
    else:
        print(_format_context_output(ctx))


async def cmd_patterns(args: argparse.Namespace) -> None:
    count = await extract_winning_patterns()
    log.info("完了。抽出パターン数: %d", count)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="X(Twitter) 自己成長RAGシステム",
    )
    subparsers = parser.add_subparsers(dest="command", help="サブコマンド")

    # analyze
    p_analyze = subparsers.add_parser(
        "analyze", help="x_monitorの結果をRAGに蓄積",
    )
    p_analyze.add_argument(
        "--days", type=int, default=7, help="何日分のデータを分析するか (デフォルト: 7)",
    )

    # record
    p_record = subparsers.add_parser(
        "record", help="投稿パフォーマンスを記録",
    )
    p_record.add_argument("--url", required=True, help="投稿URL")
    p_record.add_argument("--text", default="", help="投稿テキスト")
    p_record.add_argument("--impressions", type=int, default=0, help="インプレッション数")
    p_record.add_argument("--likes", type=int, default=0, help="いいね数")
    p_record.add_argument("--retweets", type=int, default=0, help="RT数")
    p_record.add_argument("--replies", type=int, default=0, help="リプライ数")
    p_record.add_argument("--bookmarks", type=int, default=0, help="ブックマーク数")

    # context
    p_context = subparsers.add_parser(
        "context", help="トピックに基づくRAGクエリ＋投稿草案生成",
    )
    p_context.add_argument("--topic", required=True, help="トピック/キーワード")
    p_context.add_argument("--limit", type=int, default=10, help="検索上限 (デフォルト: 10)")
    p_context.add_argument("--json", dest="json_output", action="store_true", help="JSON出力")

    # patterns
    p_patterns = subparsers.add_parser(
        "patterns", help="成功パターンを抽出",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    cmd_map = {
        "analyze": cmd_analyze,
        "record": cmd_record,
        "context": cmd_context,
        "patterns": cmd_patterns,
    }
    asyncio.run(cmd_map[args.command](args))


if __name__ == "__main__":
    main()
