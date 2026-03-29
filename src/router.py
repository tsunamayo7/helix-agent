"""Intelligent auto-routing: select the best Ollama model for a given task."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .ollama_client import OllamaClient


class Capability(str, Enum):
    CODE = "code"
    REASONING = "reasoning"
    VISION = "vision"
    EMBEDDING = "embedding"
    CREATIVE = "creative"
    GENERAL = "general"


@dataclass
class ModelInfo:
    name: str
    size_bytes: int = 0
    parameter_size: str = ""
    family: str = ""
    quantization: str = ""
    capabilities: list[Capability] = field(default_factory=list)
    priority: int = 0  # higher = preferred

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024**3) if self.size_bytes else 0.0


# Pattern-based capability detection (Phase 1)
_CAPABILITY_PATTERNS: dict[Capability, list[str]] = {
    Capability.CODE: [
        r"coder", r"codestral", r"starcoder", r"deepseek-coder",
        r"code-?llama", r"granite-code", r"yi-coder",
    ],
    Capability.VISION: [
        r"mistral-small3\.2", r"gemma3", r"moondream", r"llava",
        r"bakllava", r"llama3\.2-vision", r"minicpm-v",
    ],
    Capability.EMBEDDING: [
        r"embed", r"nomic-embed", r"bge", r"gte", r"e5-",
        r"snowflake-arctic-embed", r"mxbai-embed",
    ],
    Capability.REASONING: [
        r"qwen3", r"nemotron", r"llama", r"mistral",
        r"deepseek-r1", r"phi", r"command-r",
    ],
    Capability.CREATIVE: [
        r"gemma", r"llama", r"mistral", r"yi-",
    ],
}

# Models known to be high quality for specific tasks
_PRIORITY_BOOST: dict[str, dict[Capability, int]] = {
    "qwen3.5": {Capability.REASONING: 10, Capability.CODE: 8},
    "nemotron": {Capability.REASONING: 9},
    "qwen-coder": {Capability.CODE: 10},
    "codestral": {Capability.CODE: 9},
    "deepseek-coder": {Capability.CODE: 8},
    "mistral-small3.2": {Capability.VISION: 10},
    "gemma3": {Capability.VISION: 8, Capability.CREATIVE: 7},
    "moondream": {Capability.VISION: 6},
    "qwen3-embedding": {Capability.EMBEDDING: 10},
    "nomic-embed": {Capability.EMBEDDING: 8},
}


def _detect_capabilities(model_name: str) -> list[Capability]:
    """Detect model capabilities from name patterns."""
    name_lower = model_name.lower()
    caps = set()
    for cap, patterns in _CAPABILITY_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, name_lower):
                caps.add(cap)
                break
    if not caps:
        caps.add(Capability.GENERAL)
    return list(caps)


def _compute_priority(model_name: str, capability: Capability, size_bytes: int) -> int:
    """Compute selection priority for a model + capability pair."""
    name_lower = model_name.lower()
    priority = 0

    # Base priority from known models
    for pattern, boosts in _PRIORITY_BOOST.items():
        if pattern in name_lower and capability in boosts:
            priority += boosts[capability]
            break

    # Larger models generally better (but diminishing returns)
    size_gb = size_bytes / (1024**3) if size_bytes else 0
    if size_gb > 30:
        priority += 3
    elif size_gb > 10:
        priority += 2
    elif size_gb > 3:
        priority += 1

    return priority


class ModelRouter:
    """Selects the best model for a given task based on capabilities and priority."""

    def __init__(self, client: OllamaClient):
        self.client = client
        self._models: dict[str, ModelInfo] = {}
        self._initialized = False

    async def refresh(self) -> None:
        """Refresh model list from Ollama."""
        raw_models = await self.client.list_models()
        self._models.clear()

        for m in raw_models:
            name = m.get("name", "")
            size = m.get("size", 0)
            details = m.get("details", {})

            caps = _detect_capabilities(name)
            info = ModelInfo(
                name=name,
                size_bytes=size,
                parameter_size=details.get("parameter_size", ""),
                family=details.get("family", ""),
                quantization=details.get("quantization_level", ""),
                capabilities=caps,
            )
            self._models[name] = info

        self._initialized = True

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.refresh()

    def get_all_models(self) -> list[ModelInfo]:
        return list(self._models.values())

    async def select(
        self,
        capability: Capability,
        *,
        prefer_fast: bool = False,
        prefer_large: bool = False,
    ) -> str | None:
        """Select the best model for a given capability."""
        await self._ensure_initialized()

        candidates: list[tuple[int, str]] = []
        for name, info in self._models.items():
            if capability in info.capabilities or capability == Capability.GENERAL:
                priority = _compute_priority(name, capability, info.size_bytes)

                if prefer_fast:
                    # Penalize large models
                    if info.size_gb > 20:
                        priority -= 3
                elif prefer_large:
                    # Boost large models
                    if info.size_gb > 20:
                        priority += 5

                candidates.append((priority, name))

        if not candidates:
            # Fallback: return any available model
            if self._models:
                return next(iter(self._models))
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    async def select_for_task(self, task_description: str, mode: str = "quality") -> str | None:
        """Infer the best capability from task description, then select model."""
        cap = _infer_capability(task_description)
        prefer_fast = mode == "fast"
        prefer_large = mode == "quality"
        return await self.select(cap, prefer_fast=prefer_fast, prefer_large=prefer_large)

    async def get_capabilities_map(self) -> dict[str, list[str]]:
        """Return a map of capability -> model names."""
        await self._ensure_initialized()
        result: dict[str, list[str]] = {}
        for name, info in self._models.items():
            for cap in info.capabilities:
                result.setdefault(cap.value, []).append(name)
        return result


def _infer_capability(task: str) -> Capability:
    """Infer required capability from natural language task description."""
    task_lower = task.lower()

    code_keywords = [
        "code", "function", "class", "bug", "refactor", "test",
        "implement", "script", "program", "debug", "syntax",
        "コード", "関数", "バグ", "実装", "テスト", "リファクタ",
    ]
    vision_keywords = [
        "image", "screenshot", "photo", "picture", "ocr", "visual",
        "画像", "スクリーンショット", "写真", "OCR", "画面",
    ]
    embedding_keywords = [
        "embed", "vector", "similarity", "search", "semantic",
        "埋め込み", "ベクトル", "類似", "検索",
    ]
    creative_keywords = [
        "story", "poem", "creative", "write", "draft", "brainstorm",
        "物語", "詩", "創作", "ブレスト", "アイデア",
    ]

    if any(kw in task_lower for kw in code_keywords):
        return Capability.CODE
    if any(kw in task_lower for kw in vision_keywords):
        return Capability.VISION
    if any(kw in task_lower for kw in embedding_keywords):
        return Capability.EMBEDDING
    if any(kw in task_lower for kw in creative_keywords):
        return Capability.CREATIVE
    return Capability.REASONING
