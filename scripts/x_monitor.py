"""X(Twitter) 関連情報の定期収集・要約スクリプト.

Windowsタスクスケジューラから30分ごとに呼び出される想定。
Ollama (gemma4等) で開発者コミュニティの議論を検索・要約・スコアリングし、
高スコアのエントリのみJSONファイルに蓄積する。

使い方:
    uv run python scripts/x_monitor.py
    uv run python scripts/x_monitor.py --keywords "Claude Code,MCP"
    uv run python scripts/x_monitor.py --min-score 7
    uv run python scripts/x_monitor.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# src/ を import path に追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gpu_detect import auto_select_model
from ollama_client import OllamaClient

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

DEFAULT_KEYWORDS = [
    # Claude/MCP
    "Claude Code MCP",
    "Model Context Protocol new server",
    # ローカルLLM/エージェント
    "local LLM agent",
    "Gemma4 Ollama",
    "Codex CLI open source",
    # 生成AI全般
    "AI coding assistant OSS",
    "ComfyUI Stable Diffusion workflow",
    # 日本産LLM / AIVTuber
    "Japanese LLM PLaMo Swallow",
    "AI VTuber streaming automation",
    # ハード/自動化
    "NVIDIA RTX VRAM local AI",
    "developer automation AI agent",
    # 重要アカウントの動向
    "AnthropicAI Claude announcement",
    "browser_use AI browser automation",
    "OpenAI Codex new release",
    "Google Gemma update",
]

DEFAULT_OUTPUT_DIR = Path.home() / ".helix-agent" / "x_monitor"
MIN_SCORE_DEFAULT = 8
MAX_ENTRIES_PER_RUN = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [x_monitor] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ollama による情報収集・要約
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a developer community analyst. Output ONLY a JSON array, no other text.
Each entry in the array has these fields:
- topic (string), title (string), summary (string, 1-2 sentences),
- relevance_score (int 1-10), source (string: X/Reddit/HN/Dev.to),
- reply_candidate (bool), suggested_reply (string or null)

Be selective. Max 8 entries. High relevance to MCP servers, local LLMs, AI coding tools.
"""


async def collect_entries(
    client: OllamaClient,
    model: str,
    keywords: list[str],
) -> list[dict]:
    """キーワードリストからOllamaに問い合わせ、構造化エントリを生成する."""
    # キーワードをバッチで処理（一度に全部送る）
    keyword_list = "\n".join(f"- {kw}" for kw in keywords)
    user_prompt = f"""\
Topics:
{keyword_list}

Return a JSON array of recent developer discussions about these topics. Max 8 entries. JSON only.
"""

    log.info("Ollama (%s) に %d キーワードで問い合わせ中...", model, len(keywords))

    # OOM/VRAM不足時は自動的に小さいモデルにフォールバック
    # VRAMベース選択を先に試行
    from scripts.x_rag_learner import select_model_by_vram
    available_set = {m["name"] for m in await client.list_models()}
    vram_model = select_model_by_vram(available_set)
    if vram_model is None:
        log.error("VRAM不足。30分後の再実行を待ちます。")
        return []
    if vram_model != model:
        log.info("VRAMベース: %s → %s に変更", model, vram_model)
        model = vram_model

    MODEL_CHAIN = ["gemma4:31b", "gemma4:26b", "gemma4:e4b", "gemma4:e2b"]
    available = {m["name"] for m in await client.list_models()}
    try:
        start = MODEL_CHAIN.index(model)
    except ValueError:
        start = 0
    models_to_try = [model] + [m for m in MODEL_CHAIN[start + 1:] if m in available]

    response = None
    for try_model in models_to_try:
        try:
            response = await client.chat(
                model=try_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                format_json=False,
                num_ctx=4096,
            )
            if try_model != model:
                log.info("OOMフォールバック: %s → %s", model, try_model)
            break
        except Exception as e:
            err = str(e).lower()
            if "out of memory" in err or "oom" in err or "timeout" in err:
                log.warning("VRAM不足/タイムアウト (%s)、小さいモデルで再試行...", try_model)
                continue
            log.error("Ollama 呼び出し失敗: %s", e)
            return []

    if response is None:
        log.error("すべてのモデルで失敗。VRAM不足の可能性があります。")
        return []

    # JSON パース（切り詰められたレスポンスの修復も試みる）
    try:
        parsed = _parse_json_response(response)
        if parsed is None:
            return []
        return parsed
    except Exception as e:
        log.error("JSON パース失敗: %s", e)
        log.debug("レスポンス先頭: %s", response[:500])
        return []


def _parse_json_response(response: str) -> list[dict] | None:
    """JSON レスポンスをパースする。切り詰めや不完全なJSONの修復も試みる."""
    response = response.strip()

    # まずそのままパース
    try:
        parsed = json.loads(response)
        return _normalize_parsed(parsed)
    except json.JSONDecodeError:
        pass

    # Markdown コードブロック除去
    if "```" in response:
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", response, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1).strip())
                return _normalize_parsed(parsed)
            except json.JSONDecodeError:
                pass

    # 切り詰められたJSON配列の修復: 最後の完全なオブジェクトまでで切る
    if response.startswith("["):
        # 最後の "}," または "}" を探して配列を閉じる
        last_close = response.rfind("}")
        if last_close > 0:
            truncated = response[:last_close + 1]
            # 末尾のカンマを除去して配列を閉じる
            truncated = truncated.rstrip().rstrip(",") + "]"
            try:
                parsed = json.loads(truncated)
                return _normalize_parsed(parsed)
            except json.JSONDecodeError:
                pass

    log.warning("JSON修復も失敗。レスポンス長: %d\n先頭300文字: %s", len(response), response[:300])
    return None


def _normalize_parsed(parsed: Any) -> list[dict] | None:
    """パース結果をリストに正規化する."""
    if isinstance(parsed, dict):
        for key in ("entries", "results", "discussions", "items", "data"):
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        return [parsed]
    if isinstance(parsed, list):
        return parsed
    log.warning("予期しないJSONフォーマット: %s", type(parsed))
    return None


# ---------------------------------------------------------------------------
# フィルタリング・保存
# ---------------------------------------------------------------------------

def filter_entries(entries: list[dict], min_score: int) -> list[dict]:
    """スコアが閾値以上のエントリのみ残す."""
    filtered = []
    for entry in entries:
        score = entry.get("relevance_score", 0)
        if isinstance(score, (int, float)) and score >= min_score:
            # タイムスタンプ付与
            entry["collected_at"] = datetime.now(timezone.utc).isoformat()
            filtered.append(entry)
    return filtered


def save_entries(entries: list[dict], output_dir: Path) -> Path | None:
    """エントリをJSONファイルに保存する."""
    if not entries:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    filename = f"x_monitor_{now.strftime('%Y%m%d_%H%M')}.json"
    filepath = output_dir / filename

    # 既存ファイルがあればマージ（同じ30分枠で複数回実行された場合）
    existing = []
    if filepath.exists():
        try:
            existing = json.loads(filepath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    merged = existing + entries
    filepath.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return filepath


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

async def main(
    keywords: list[str] | None = None,
    min_score: int = MIN_SCORE_DEFAULT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    dry_run: bool = False,
) -> int:
    """メイン処理. 戻り値は蓄積されたエントリ数."""
    # ハートビート送信
    try:
        from supervisor import write_heartbeat
        write_heartbeat("x_monitor")
    except ImportError:
        pass

    keywords = keywords or DEFAULT_KEYWORDS

    client = OllamaClient(timeout=300.0)

    # Ollama 起動チェック
    if not await client.is_available():
        log.warning("Ollama が起動していません。スキップします。")
        return 0

    # GPU に応じたモデル自動選択（利用可能なモデルにフォールバック）
    model = auto_select_model(task="text")
    available_models = {m["name"] for m in await client.list_models()}
    if model not in available_models:
        # フォールバック: gemma4系を優先して探す
        fallback_order = ["gemma4:31b", "gemma4:26b", "gemma4:e4b", "gemma4:e2b"]
        model = next((m for m in fallback_order if m in available_models), None)
        if model is None:
            log.warning("利用可能なgemma4モデルが見つかりません。スキップします。")
            return 0
    log.info("使用モデル: %s", model)

    # 収集
    entries = await collect_entries(client, model, keywords)
    log.info("取得エントリ数: %d", len(entries))

    # フィルタリング
    filtered = filter_entries(entries, min_score)
    log.info("スコア %d+ のエントリ: %d 件", min_score, len(filtered))

    if dry_run:
        log.info("--- dry-run モード: 保存しません ---")
        for entry in filtered:
            log.info(
                "  [%s] %s (score=%s, reply=%s)",
                entry.get("source", "?"),
                entry.get("title", "?")[:60],
                entry.get("relevance_score", "?"),
                entry.get("reply_candidate", False),
            )
        return len(filtered)

    # 保存
    if filtered:
        filepath = save_entries(filtered, output_dir)
        if filepath:
            log.info("保存先: %s (%d 件)", filepath, len(filtered))
    else:
        log.info("高スコアのエントリなし。保存スキップ。")

    return len(filtered)


def cli():
    parser = argparse.ArgumentParser(
        description="X(Twitter)関連情報の定期収集・要約",
    )
    parser.add_argument(
        "--keywords",
        type=str,
        default=None,
        help="カンマ区切りのキーワードリスト (デフォルト: 組み込みリスト)",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=MIN_SCORE_DEFAULT,
        help=f"蓄積する最小スコア (デフォルト: {MIN_SCORE_DEFAULT})",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"出力ディレクトリ (デフォルト: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="結果を表示するだけで保存しない",
    )
    args = parser.parse_args()

    keywords = None
    if args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    count = asyncio.run(main(
        keywords=keywords,
        min_score=args.min_score,
        output_dir=Path(args.output_dir),
        dry_run=args.dry_run,
    ))
    log.info("完了。蓄積エントリ数: %d", count)


if __name__ == "__main__":
    cli()
