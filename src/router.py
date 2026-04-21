"""Intelligent auto-routing: select the best Ollama model for a given task."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .benchmark import BenchmarkEngine
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
    context_length: int = 0
    capabilities: list[Capability] = field(default_factory=list)
    priority: int = 0  # higher = preferred
    available: bool = True  # False if model fails to respond (500, timeout)
    avg_response_sec: float = 0.0  # measured response time

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024**3) if self.size_bytes else 0.0

    @property
    def param_billions(self) -> float:
        """Extract parameter count in billions from parameter_size string."""
        if not self.parameter_size:
            return 0.0
        s = self.parameter_size.upper().replace(",", "")
        try:
            if "B" in s:
                return float(s.replace("B", ""))
            if "M" in s:
                return float(s.replace("M", "")) / 1000
        except ValueError:
            pass
        return 0.0


# Pattern-based capability detection (Phase 1)
_CAPABILITY_PATTERNS: dict[Capability, list[str]] = {
    Capability.CODE: [
        r"coder", r"codestral", r"devstral", r"starcoder", r"deepseek-coder",
        r"code-?llama", r"granite-code", r"yi-coder",
    ],
    Capability.VISION: [
        r"mistral-small3\.2", r"gemma4", r"gemma3", r"moondream", r"llava",
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
    "qwen3-next": {Capability.REASONING: 10, Capability.CODE: 8},
    "nemotron": {Capability.REASONING: 9},
    "command-a": {Capability.REASONING: 8, Capability.CODE: 7},
    "qwen-coder": {Capability.CODE: 10},
    "codestral": {Capability.CODE: 9},
    "devstral": {Capability.CODE: 9},
    "deepseek-coder": {Capability.CODE: 8},
    "mistral-small3.2": {Capability.VISION: 10, Capability.REASONING: 7},
    "gemma4": {Capability.REASONING: 9, Capability.CODE: 8, Capability.VISION: 9, Capability.CREATIVE: 8},
    "gemma3": {Capability.VISION: 8, Capability.CREATIVE: 7},
    "moondream": {Capability.VISION: 6},
    "qwen3-embedding": {Capability.EMBEDDING: 10},
    "nomic-embed": {Capability.EMBEDDING: 8},
    "bge": {Capability.EMBEDDING: 7},
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
        self.benchmark_engine = BenchmarkEngine(client)
        self._model_override: str | None = None  # user-specified model lock

    async def refresh(self, *, fetch_metadata: bool = False) -> None:
        """Refresh model list from Ollama.

        Args:
            fetch_metadata: If True, call 'ollama show' for each model to get
                           context_length and detailed info (slower but more accurate).
        """
        raw_models = await self.client.list_models()
        self._models.clear()

        for m in raw_models:
            name = m.get("name", "")
            size = m.get("size", 0)
            details = m.get("details", {})

            caps = _detect_capabilities(name)
            ctx_len = 0

            # Phase 2: fetch metadata for better routing
            if fetch_metadata:
                try:
                    meta = await self.client.show_model(name)
                    model_info = meta.get("model_info", {})
                    # Context length from various possible keys
                    for key in model_info:
                        if "context_length" in key:
                            ctx_len = model_info[key]
                            break
                except Exception:
                    pass  # Metadata fetch is best-effort

            info = ModelInfo(
                name=name,
                size_bytes=size,
                parameter_size=details.get("parameter_size", ""),
                family=details.get("family", ""),
                quantization=details.get("quantization_level", ""),
                context_length=ctx_len,
                capabilities=caps,
            )
            self._models[name] = info

        self._initialized = True

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.refresh()

    def get_all_models(self) -> list[ModelInfo]:
        return list(self._models.values())

    async def probe_models(self, timeout_sec: float = 30.0) -> dict[str, bool]:
        """Test each model with a minimal prompt to check availability.

        Models that fail (500 error, timeout) are marked as unavailable.
        Returns a map of model name -> available status.
        """
        import time

        results: dict[str, bool] = {}
        await self._ensure_initialized()

        for name, info in self._models.items():
            # Skip embedding models (they don't support chat)
            if Capability.EMBEDDING in info.capabilities and len(info.capabilities) == 1:
                info.available = True
                results[name] = True
                continue

            start = time.monotonic()
            try:
                # Minimal probe: single token response
                await self.client.generate(
                    model=name,
                    prompt="Hi",
                    temperature=0.0,
                )
                elapsed = time.monotonic() - start
                info.available = True
                info.avg_response_sec = round(elapsed, 1)
                results[name] = True
            except Exception:
                info.available = False
                results[name] = False

        return results

    def set_model_override(self, model_name: str | None) -> None:
        """Lock routing to a specific model. Pass None to clear."""
        self._model_override = model_name

    def get_model_override(self) -> str | None:
        """Return current model override, if any."""
        return self._model_override

    def _benchmark_priority(self, model_name: str, capability: Capability) -> float:
        """Get priority bonus from cached benchmark scores (0-10 scale)."""
        bm = self.benchmark_engine.get_cached(model_name)
        if bm is None:
            return 0.0

        cap_to_category = {
            Capability.CODE: "code",
            Capability.REASONING: "reasoning",
            Capability.VISION: "code",  # no specific vision bench yet, use overall
            Capability.CREATIVE: "japanese",  # creative → use general quality
            Capability.EMBEDDING: None,  # embeddings not benchmarked
            Capability.GENERAL: None,
        }

        category = cap_to_category.get(capability)
        if category and category in bm.category_scores:
            # Convert 0-100 score to 0-10 priority bonus
            return bm.category_scores[category] / 10.0

        # Fallback: use total score
        return bm.total_score / 10.0

    async def select(
        self,
        capability: Capability,
        *,
        prefer_fast: bool = False,
        prefer_large: bool = False,
    ) -> str | None:
        """Select the best model for a given capability."""
        # If user override is set, always use it
        if self._model_override:
            return self._model_override

        await self._ensure_initialized()

        candidates: list[tuple[float, str]] = []
        for name, info in self._models.items():
            # Skip models marked as unavailable
            if not info.available:
                continue
            if capability in info.capabilities or capability == Capability.GENERAL:
                priority = float(_compute_priority(name, capability, info.size_bytes))

                # Add benchmark score bonus (strongest signal when available)
                bench_bonus = self._benchmark_priority(name, capability)
                if bench_bonus > 0:
                    priority += bench_bonus

                if prefer_fast:
                    # Penalize large models heavily in fast mode
                    if info.size_gb > 50:
                        priority -= 8
                    elif info.size_gb > 20:
                        priority -= 5
                    elif info.size_gb < 10:
                        priority += 3
                    # Speed bonus from benchmark
                    bm = self.benchmark_engine.get_cached(name)
                    if bm and bm.category_scores.get("speed", 0) > 0:
                        priority += bm.category_scores["speed"] / 20.0
                elif prefer_large:
                    # Boost large models
                    if info.size_gb > 20:
                        priority += 5

                candidates.append((priority, name))

        # Fast mode fallback: if best candidate is still huge, try any small model
        if prefer_fast and candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            best_name = candidates[0][1]
            best_info = self._models.get(best_name)
            if best_info and best_info.size_gb > 30:
                # Find any model under 20GB as fallback
                small = [(p, n) for p, n in candidates if self._models[n].size_gb < 20]
                if not small:
                    # Broaden search to ALL models under 20GB
                    for n, info in self._models.items():
                        if info.size_gb < 20 and Capability.EMBEDDING not in info.capabilities:
                            small.append((0.0, n))
                if small:
                    small.sort(key=lambda x: x[0], reverse=True)
                    return small[0][1]

        if not candidates:
            if self._models:
                return next(iter(self._models))
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    async def select_for_task(self, task_description: str, mode: str = "quality") -> str | None:
        """Infer the best capability from task description, then select model."""
        # If user override is set, always use it
        if self._model_override:
            return self._model_override

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
