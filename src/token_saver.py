"""Token Saver: reduce Claude Code token consumption for Computer Use workflows.

Addresses documented pain points:
- Screenshots consume 5,000-15,000 tokens each (TestCollab benchmark)
- Playwright MCP DOM payloads consume 114K tokens per call (vs 27K for CLI)
- MCP tool schemas alone eat 66K tokens at session startup
- Retry loops burn through Max plan quotas in minutes

This module provides local-LLM-backed compression tools so Claude Code only
sees compact structured summaries instead of raw screenshots or DOM dumps.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

from .vision import VisionAnalyzer


# --- Vision Compression ---------------------------------------------------

VISION_COMPRESS_PROMPT = """Analyze this screenshot and extract a compact structured summary.

Output STRICT JSON only (no prose, no markdown fences). Schema:
{
  "page_type": "short label like login|dashboard|form|article|error",
  "title": "visible page/window title if any",
  "primary_action": "most likely next action a user would take",
  "interactive_elements": [
    {"role": "button|link|input|checkbox|select", "label": "visible text", "location": "top|bottom|left|right|center"}
  ],
  "key_text": ["most important visible text snippets, max 5 items, each under 80 chars"],
  "state_flags": {
    "has_error": true|false,
    "has_modal": true|false,
    "requires_auth": true|false,
    "loading": true|false
  },
  "notes": "one short sentence with anything unusual"
}

Keep arrays short. Prioritize signal over completeness. Target output under 400 tokens."""

VISION_AUTO_PROMPT = """この画像を見て、何が写っているかを詳細に日本語で説明してください。

あなたは優秀な画像分析者です。型にはめず、見えるものをそのまま正確に伝えてください。

## 必ず含めること
1. **何の画像か** — 最初に一言で（例: 「アニメ風の女の子のイラスト」「手書きのフローチャート」「Chromeのエラー画面」）
2. **見えるもの全て** — テキスト、図、人物、UI要素、手書き、数式、コード、何でも。読めるテキストはそのまま書き起こす
3. **空間的な配置** — 何がどこにあるか（左上に〇〇、中央に△△、右下に□□）

## 画像の種類に応じて追加で含めること（該当するもの全て）
- **人物・キャラクター**: 外見（髪・服・体型）、ポーズ（手足の位置・体の向き）、表情（目・口・眉）、視線の方向
- **イラスト・アート**: 画風、色使い、構図（アングル・フレーミング）、雰囲気
- **写真**: 撮影状況（屋内外・光・季節）、被写体の状態
- **UI・スクリーンショット**: アプリ名、画面状態、ボタンやフォームの内容、エラーの有無
- **図・チャート・ER図**: 種類、ノードや矢印の関係、数値やラベル
- **手書き**: 書き起こし（読める範囲で全て）、図の説明、判読の確信度
- **文書・コード**: テキスト内容、言語、構造
- **複合（複数要素混在）**: 全ての要素を漏れなく。「左半分は手書きメモ、右半分はグラフ」等

## 出力形式
自由な文章で構いません。ただし最後に1行で要約をつけてください:
要約: （この画像を1文で説明）"""


# --- DOM Compression ------------------------------------------------------

DOM_COMPRESS_PROMPT = """You are compressing an HTML page for a downstream AI coding agent.
Extract ONLY what an agent needs to take the next action. Output STRICT JSON:

{
  "url": "the page url",
  "title": "page title",
  "summary": "one-sentence description of what this page shows",
  "forms": [
    {"name": "form purpose", "fields": [{"name": "field name", "type": "text|email|password|select", "required": true|false, "placeholder": "..."}]}
  ],
  "links": [{"text": "link text", "href": "url or #anchor"}],
  "buttons": [{"text": "button label", "action_hint": "what it does"}],
  "main_content": "key text content, max 500 chars",
  "errors": ["any visible error messages"],
  "next_action_candidates": ["top 3 actions an agent could take next"]
}

Max 10 links, 10 buttons, 5 forms. Be ruthless about cutting boilerplate, ads, footers, nav menus."""


class TokenSaver:
    """Compress screenshots and DOM payloads before Claude Code sees them."""

    def __init__(
        self,
        ollama_host: str = "http://localhost:11434",
        vision_model: str = "",
        text_model: str = "",
        timeout: float = 120.0,
    ):
        from .gpu_detect import auto_select_model

        self.ollama_host = ollama_host.rstrip("/")
        self.vision_model = vision_model or auto_select_model("vision")
        self.text_model = text_model or auto_select_model("text")
        self.timeout = timeout
        self.vision = VisionAnalyzer(host=self.ollama_host, model=self.vision_model, timeout=timeout)

    async def vision_compress(
        self,
        image_path: str = "",
        image_base64: str = "",
        custom_prompt: str = "",
        model: str = "",
        mode: str = "auto",
    ) -> dict[str, Any]:
        """Compress an image into a structured summary.

        Args:
            image_path: Path to image file (alternative to image_base64).
            image_base64: Base64-encoded image data.
            custom_prompt: Override the default extraction prompt.
            model: Override the vision model.
            mode: "ui" for screenshots (default), "describe" for illustrations/photos.

        Returns:
            Dict with `summary` (structured data), `raw_response`, `tokens_saved_estimate`.
        """
        if not image_path and not image_base64:
            return {"error": "image_path or image_base64 required"}

        if image_path and not image_base64:
            path = Path(image_path)
            if not path.exists():
                return {"error": f"Image file not found: {image_path}"}
            try:
                image_base64 = base64.b64encode(path.read_bytes()).decode("ascii")
            except Exception as e:
                return {"error": f"Failed to read image: {e}"}

        if custom_prompt:
            prompt = custom_prompt
        elif mode == "auto":
            prompt = VISION_AUTO_PROMPT
        elif mode == "ui":
            prompt = VISION_COMPRESS_PROMPT
        else:
            prompt = VISION_AUTO_PROMPT
        use_model = model or self.vision_model

        raw = await self.vision.analyze(image_base64, prompt=prompt, model=use_model)

        structured = _try_parse_json(raw)

        # Estimate: avg screenshot ~8K tokens uncompressed, summary ~400 tokens
        estimated_saved = 8000 - _estimate_tokens(raw)

        if structured:
            summary = structured
        elif mode == "auto":
            summary = {"description": raw}
        else:
            summary = {"_unparsed": raw[:600]}

        return {
            "summary": summary,
            "raw_response": raw,
            "tokens_saved_estimate": max(estimated_saved, 0),
            "model": use_model,
        }

    async def dom_compress(
        self,
        html: str = "",
        url: str = "",
        text_content: str = "",
        custom_prompt: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        """Compress a DOM/HTML payload into a structured summary.

        Args:
            html: Raw HTML string (alternative to text_content).
            url: Page URL for context.
            text_content: Pre-extracted text content (alternative to html).
            custom_prompt: Override the default extraction prompt.
            model: Override the text model.

        Returns:
            Dict with `summary` (structured data), `tokens_saved_estimate`.
        """
        if not html and not text_content:
            return {"error": "html or text_content required"}

        content = html or text_content
        original_length = len(content)

        # Truncate very large payloads before sending to local model
        if len(content) > 30000:
            content = content[:30000] + "\n[... truncated for compression ...]"

        prompt = custom_prompt or DOM_COMPRESS_PROMPT
        use_model = model or self.text_model

        full_prompt = (
            f"{prompt}\n\n"
            f"URL: {url}\n\n"
            f"PAGE CONTENT:\n{content}"
        )

        raw = await _ollama_generate(
            self.ollama_host, use_model, full_prompt, self.timeout
        )

        structured = _try_parse_json(raw)

        # Estimate: raw DOM ~original_length/4 tokens, summary ~400 tokens
        estimated_raw_tokens = original_length // 4
        estimated_saved = estimated_raw_tokens - _estimate_tokens(raw)

        return {
            "summary": structured if structured else {"_unparsed": raw[:800]},
            "original_char_count": original_length,
            "tokens_saved_estimate": max(estimated_saved, 0),
            "model": use_model,
        }


# --- Retry Loop Guard -----------------------------------------------------


class RetryGuard:
    """Detect when the orchestrator repeats the same tool call pattern.

    Addresses anthropics/claude-code#41659: Claude Code sometimes ignores user
    corrections and keeps calling the same failing tool with the same args.
    This guard tracks call history per session and returns a warning signal
    when a repeat-loop is detected, letting the caller break out early.
    """

    def __init__(self, threshold: int = 3, window_seconds: int = 300):
        self.threshold = threshold
        self.window_seconds = window_seconds
        self._history: dict[str, list[tuple[str, float]]] = defaultdict(list)

    @staticmethod
    def _hash_call(tool_name: str, args: Any) -> str:
        payload = json.dumps({"t": tool_name, "a": args}, sort_keys=True, default=str)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _prune(self, session: str, now: float) -> None:
        cutoff = now - self.window_seconds
        self._history[session] = [
            (h, t) for (h, t) in self._history[session] if t >= cutoff
        ]

    def check(
        self,
        tool_name: str,
        args: Any,
        session_id: str = "default",
    ) -> dict[str, Any]:
        """Check if this tool call forms a repeat loop.

        Returns:
            Dict with keys:
                `loop_detected`: bool
                `repeat_count`: int
                `recommendation`: str
                `recent_calls`: list[str]
        """
        now = time.time()
        self._prune(session_id, now)

        call_hash = self._hash_call(tool_name, args)
        self._history[session_id].append((call_hash, now))

        recent = self._history[session_id]
        repeat_count = sum(1 for (h, _) in recent if h == call_hash)
        loop_detected = repeat_count >= self.threshold

        recommendation = ""
        if loop_detected:
            recommendation = (
                f"Tool '{tool_name}' has been called {repeat_count} times with identical args "
                f"within {self.window_seconds}s. Likely stuck in retry loop. "
                "Recommend: inspect last error, vary args, or escalate to Claude/Opus."
            )
        elif repeat_count == self.threshold - 1:
            recommendation = (
                f"Tool '{tool_name}' called {repeat_count} times with identical args. "
                "One more repeat will trigger loop warning."
            )

        return {
            "loop_detected": loop_detected,
            "repeat_count": repeat_count,
            "tool_name": tool_name,
            "session_id": session_id,
            "recommendation": recommendation,
            "threshold": self.threshold,
            "window_seconds": self.window_seconds,
        }

    def reset(self, session_id: str = "default") -> dict[str, Any]:
        """Clear history for a session."""
        removed = len(self._history.pop(session_id, []))
        return {"session_id": session_id, "cleared_entries": removed}

    def status(self, session_id: str = "default") -> dict[str, Any]:
        """Get current session history stats."""
        now = time.time()
        self._prune(session_id, now)
        hist = self._history[session_id]
        by_hash: dict[str, int] = defaultdict(int)
        for h, _ in hist:
            by_hash[h] += 1
        return {
            "session_id": session_id,
            "total_calls": len(hist),
            "unique_calls": len(by_hash),
            "max_repeats": max(by_hash.values()) if by_hash else 0,
        }


# --- Helpers --------------------------------------------------------------


def _try_parse_json(text: str) -> dict | None:
    """Best-effort JSON extraction from local LLM output."""
    if not text:
        return None
    text = text.strip()
    # Strip common markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    # Find JSON object braces
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English."""
    return max(len(text) // 4, 1)


async def _ollama_generate(
    host: str,
    model: str,
    prompt: str,
    timeout: float,
) -> str:
    """Call Ollama /api/generate for a simple text completion."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
        "format": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{host}/api/generate", json=payload)
            r.raise_for_status()
            data = r.json()
            return data.get("response", "")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return json.dumps({"error": f"ollama unavailable: {e}"})
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"ollama http {e.response.status_code}"})
