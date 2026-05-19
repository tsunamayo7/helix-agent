"""Vision analysis using Ollama Vision models."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

DEFAULT_VISION_MODEL = "qwen3.6:27b"


class VisionAnalyzer:
    """Analyze images using Ollama Vision-capable models."""

    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = DEFAULT_VISION_MODEL,
        timeout: float = 60.0,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout

    async def analyze(
        self,
        image_base64: str,
        prompt: str = "Describe what you see in this image.",
        model: str | None = None,
    ) -> str:
        """Analyze an image with a Vision model.

        Args:
            image_base64: Base64-encoded PNG/JPEG image data.
            prompt: Question or instruction for the model.
            model: Override the default model.

        Returns:
            Model's text response describing the image.
        """
        use_model = model or self.model
        payload: dict[str, Any] = {
            "model": use_model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [image_base64],
                }
            ],
            "stream": False,
            "options": {"temperature": 0.3},
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(f"{self.host}/api/chat", json=payload)
                r.raise_for_status()
                data = r.json()
                return data.get("message", {}).get("content", "")
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            return f"Vision analysis unavailable: {e}"
        except httpx.HTTPStatusError as e:
            return f"Vision analysis error: {e.response.status_code}"

    async def analyze_file(
        self,
        image_path: str | Path,
        prompt: str = "Describe what you see in this image.",
        model: str | None = None,
    ) -> str:
        """Analyze an image file."""
        path = Path(image_path)
        if not path.exists():
            return f"Error: Image file not found: {image_path}"
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except Exception as e:
            return f"Error reading image file: {e}"
        return await self.analyze(b64, prompt=prompt, model=model)

    async def is_available(self) -> bool:
        """Check if the Vision model is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.host}/api/version")
                return r.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False
