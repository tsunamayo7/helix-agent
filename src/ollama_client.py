"""Async Ollama API client."""

from __future__ import annotations

import httpx


class OllamaClient:
    """Lightweight async client for Ollama REST API."""

    def __init__(self, host: str = "http://localhost:11434", timeout: float = 120.0):
        self.host = host.rstrip("/")
        self.timeout = timeout

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

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.host}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
            return data.get("message", {}).get("content", "")

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
