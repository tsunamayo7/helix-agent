"""Async Ollama API client."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ChatResponse:
    """Chat response with token usage info."""

    content: str
    input_tokens: int = 0
    output_tokens: int = 0


class OllamaClient:
    """Lightweight async client for Ollama REST API."""

    def __init__(self, host: str = "http://localhost:11434", timeout: float = 120.0):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._context_lengths: dict[str, int] = {}

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.host}/api/version")
                return r.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    async def list_models(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.host}/api/tags")
            r.raise_for_status()
            return r.json().get("models", [])

    async def show_model(self, name: str) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{self.host}/api/show", json={"name": name})
            r.raise_for_status()
            return r.json()

    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        format_json: bool = False,
        num_ctx: int | None = None,
    ) -> str:
        resp = await self.chat_with_usage(
            model=model, messages=messages,
            temperature=temperature, format_json=format_json, num_ctx=num_ctx,
        )
        return resp.content

    async def chat_with_usage(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        format_json: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
    ) -> ChatResponse:
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if format_json:
            payload["format"] = "json"
        if num_ctx:
            payload["options"]["num_ctx"] = num_ctx
        if num_predict:
            payload["options"]["num_predict"] = num_predict

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.host}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
            return ChatResponse(
                content=data.get("message", {}).get("content", ""),
                input_tokens=data.get("prompt_eval_count", 0) or 0,
                output_tokens=data.get("eval_count", 0) or 0,
            )

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        format_json: bool = False,
        num_ctx: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream chat tokens. Yields content chunks as they arrive."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if format_json:
            payload["format"] = "json"
        if num_ctx:
            payload["options"]["num_ctx"] = num_ctx

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.host}/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    import json
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = data.get("message", {}).get("content", "")
                    if chunk:
                        yield chunk
                    if data.get("done", False):
                        return

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: float = 0.7,
        num_ctx: int | None = None,
    ) -> dict:
        """Chat with Ollama function calling (tools parameter).

        Returns the full message dict which may contain tool_calls.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "tools": tools,
            "options": {"temperature": temperature},
        }
        if num_ctx:
            payload["options"]["num_ctx"] = num_ctx

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.host}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
            return data.get("message", {})

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.7,
        format_json: bool = False,
    ) -> str:
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system
        if format_json:
            payload["format"] = "json"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.host}/api/generate", json=payload)
            r.raise_for_status()
            return r.json().get("response", "")

    async def embeddings(self, model: str, input_text: str | list[str]) -> list[list[float]]:
        payload = {
            "model": model,
            "input": input_text if isinstance(input_text, list) else [input_text],
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{self.host}/api/embed", json=payload)
            r.raise_for_status()
            return r.json().get("embeddings", [])

    async def chat_vision(
        self,
        model: str,
        prompt: str,
        images: list[str],
        *,
        temperature: float = 0.3,
    ) -> str:
        messages = [
            {
                "role": "user",
                "content": prompt,
                "images": images,
            }
        ]
        return await self.chat(model, messages, temperature=temperature)

    async def get_context_length(self, model: str) -> int:
        """Get the context window size for a model via /api/show."""
        if model in self._context_lengths:
            return self._context_lengths[model]
        try:
            info = await self.show_model(model)
            params = info.get("model_info", {})
            for key, val in params.items():
                if "context_length" in key and isinstance(val, (int, float)):
                    self._context_lengths[model] = int(val)
                    return int(val)
            # Fallback: parse from modelfile parameters
            modelfile = info.get("parameters", "")
            if modelfile:
                match = re.search(r"num_ctx\s+(\d+)", modelfile)
                if match:
                    ctx = int(match.group(1))
                    self._context_lengths[model] = ctx
                    return ctx
        except Exception:
            pass
        default = 8192
        self._context_lengths[model] = default
        return default
