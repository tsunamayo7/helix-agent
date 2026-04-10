"""Flexible GPU-aware model selection with Opus-driven routing.

Design philosophy:
  - Opus (the caller) can specify model/strategy hints for intelligent routing
  - helix-agent auto-selects when no hint is given, considering:
    * Task type, input complexity, urgency, quality requirements
    * Available VRAM and current GPU load
    * Model capabilities (v4 for ReAct/review, 31b for exploration, e2b/e4b for light tasks)
  - Fixed lookup tables are fallback only; the caller's intent takes priority

Model roster (2026-04-08):
  gemma4:e2b       (~4GB)  — Fastest, light tasks (summarize/translate/classify)
  gemma4:e4b       (~6GB)  — Balanced, medium tasks (code_gen, moderate review)
  gemma4-agent-coder-v4 (~20GB) — Custom fine-tuned: ReAct, tool use, critique, structured output
  gemma4:31b      (~20GB)  — Base model: best for unknown/novel problem discovery
  qwen3-vl:32b   (~20GB)  — Vision specialist (OCR, image analysis)
  qwen3.5:72b    (~45GB)  — High-quality reasoning
  qwen3.5:122b   (~75GB)  — Maximum reasoning power

v4 vs 31b (benchmark 2026-04-08):
  v4: ReAct 6.6s(OK), Review 39s(P1=2), Critique OK, Tool select 3/3, Free-form OK
  31b: ReAct 19.2s(OK), Review 54s(P1=2), Critique OK, Tool select 3/3
  → v4 is 1.3-3x faster with equal precision. Use v4 as default, 31b for exploration.
"""

from __future__ import annotations

import subprocess
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GPUInfo:
    name: str = "unknown"
    vram_mb: int = 0
    vram_gb: float = 0.0


@dataclass
class ModelSelection:
    """Result of model selection with reasoning."""
    model: str
    reason: str
    strategy: str = "auto"  # auto, fast, quality, dialogue, caller_specified


# --- Strategy definitions ---
# Each strategy is a prioritized model preference for different contexts.
# Opus can request a strategy by name, or specify a model directly.

STRATEGIES = {
    "fast": {
        "description": "Minimize latency. Use smallest capable model.",
        "prefer": ["gemma4:e2b", "gemma4:e4b", "gemma4-agent-coder-v4:latest"],
    },
    "balanced": {
        "description": "Default. v4 for most tasks, e2b/e4b for simple ones.",
        "prefer": ["gemma4-agent-coder-v4:latest", "gemma4:e4b", "gemma4:e2b"],
    },
    "quality": {
        "description": "Maximum single-model quality. Use 31b or larger.",
        "prefer": ["gemma4:31b", "qwen3.5:72b", "gemma4-agent-coder-v4:latest"],
    },
    "dialogue": {
        "description": "31b↔v4 adversarial dialogue for highest review quality.",
        "prefer": ["gemma4:31b", "gemma4-agent-coder-v4:latest"],
    },
    "exploration": {
        "description": "Unknown problem discovery. 31b's generalization is key.",
        "prefer": ["gemma4:31b", "qwen3.5:122b"],
    },
    "vision": {
        "description": "Image/screenshot analysis.",
        "prefer": ["qwen3-vl:32b", "gemma4:e4b"],
    },
}

# Task → capability mapping: which models can handle which tasks well
MODEL_CAPABILITIES = {
    "gemma4:e2b": {
        "good": ["summarize", "translate", "classify"],
        "ok": ["search"],
        "weak": ["review", "code_gen", "reasoning", "critique", "react"],
        "vram_gb": 4,
        "speed": "fastest",
    },
    "gemma4:e4b": {
        "good": ["summarize", "translate", "classify", "search", "code_gen"],
        "ok": ["review"],
        "weak": ["reasoning", "critique", "react"],
        "vram_gb": 6,
        "speed": "fast",
    },
    "gemma4-agent-coder-v4:latest": {
        "good": ["react", "review", "critique", "search", "code_gen", "summarize", "translate", "classify"],
        "ok": ["reasoning"],
        "weak": [],
        "vram_gb": 20,
        "speed": "fast",
    },
    "gemma4:31b": {
        "good": ["review", "reasoning", "critique", "react", "search"],
        "ok": ["summarize", "translate", "classify", "code_gen"],
        "weak": [],
        "vram_gb": 20,
        "speed": "moderate",
    },
    "qwen3-vl:32b": {
        "good": ["vision"],
        "ok": ["reasoning"],
        "weak": ["react", "critique"],
        "vram_gb": 20,
        "speed": "moderate",
    },
    "qwen3.5:72b": {
        "good": ["reasoning", "review", "code_gen"],
        "ok": ["summarize", "translate"],
        "weak": [],
        "vram_gb": 45,
        "speed": "slow",
    },
    "qwen3.5:122b": {
        "good": ["reasoning", "review", "code_gen", "critique"],
        "ok": ["summarize", "translate"],
        "weak": [],
        "vram_gb": 75,
        "speed": "slowest",
    },
}

# Legacy fixed tiers — used only as fallback when no strategy/hint is given
MODEL_TIERS = {
    (0, 10): {t: "gemma4:e2b" for t in ["vision","text","review","reasoning","summarize","translate","classify","code_gen","search"]},
    (10, 20): {
        "vision": "gemma4:e4b", "text": "gemma4:e4b",
        "review": "gemma4-agent-coder-v4:latest", "reasoning": "gemma4-agent-coder-v4:latest",
        "summarize": "gemma4:e2b", "translate": "gemma4:e2b", "classify": "gemma4:e2b",
        "code_gen": "gemma4:e4b", "search": "gemma4-agent-coder-v4:latest",
    },
    (20, 48): {
        "vision": "qwen3-vl:32b", "text": "gemma4-agent-coder-v4:latest",
        "review": "gemma4-agent-coder-v4:latest", "reasoning": "gemma4:31b",
        "summarize": "gemma4:e2b", "translate": "gemma4:e2b", "classify": "gemma4:e2b",
        "code_gen": "gemma4-agent-coder-v4:latest", "search": "gemma4-agent-coder-v4:latest",
    },
    (48, 1000): {
        "vision": "qwen3-vl:32b", "text": "gemma4-agent-coder-v4:latest",
        "review": "gemma4-agent-coder-v4:latest", "reasoning": "qwen3.5:122b",
        "summarize": "gemma4:e2b", "translate": "gemma4:e2b", "classify": "gemma4:e2b",
        "code_gen": "gemma4-agent-coder-v4:latest", "search": "gemma4-agent-coder-v4:latest",
    },
}


def detect_gpu() -> GPUInfo:
    """Detect GPU using nvidia-smi."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            best = GPUInfo()
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    name = parts[0]
                    vram_mb = int(parts[1])
                    if vram_mb > best.vram_mb:
                        best = GPUInfo(
                            name=name,
                            vram_mb=vram_mb,
                            vram_gb=round(vram_mb / 1024, 1),
                        )
            return best
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return GPUInfo()


def _get_vram_gb() -> float:
    """Get available VRAM in GB, with caching."""
    gpu = detect_gpu()
    return gpu.vram_gb if gpu.vram_gb > 0 else 4.0


def _model_fits_vram(model: str, vram_gb: float) -> bool:
    """Check if a model fits in available VRAM."""
    caps = MODEL_CAPABILITIES.get(model)
    if not caps:
        return True  # Unknown model, assume it fits
    return caps["vram_gb"] <= vram_gb


def _model_good_for_task(model: str, task: str) -> str:
    """Return capability level: 'good', 'ok', 'weak', or 'unknown'."""
    caps = MODEL_CAPABILITIES.get(model)
    if not caps:
        return "unknown"
    if task in caps["good"]:
        return "good"
    if task in caps["ok"]:
        return "ok"
    if task in caps["weak"]:
        return "weak"
    return "ok"  # Not listed = assume ok


def select_model(
    task: str = "text",
    strategy: Optional[str] = None,
    model: Optional[str] = None,
    input_len: int = 0,
    urgent: bool = False,
    quality: str = "balanced",
    vram_gb: float = 0,
) -> ModelSelection:
    """Flexible model selection driven by caller intent.

    Priority order:
      1. Explicit model specified by caller (Opus) → use it directly
      2. Strategy specified by caller → follow strategy preferences
      3. Auto-select based on task + input_len + urgency + quality

    Args:
        task: Task type — "react", "review", "critique", "search",
              "summarize", "translate", "classify", "code_gen",
              "reasoning", "vision", "text"
        strategy: Named strategy — "fast", "balanced", "quality",
                  "dialogue", "exploration", "vision"
        model: Explicit model name (overrides everything)
        input_len: Input text length in characters
        urgent: If True, prefer faster models
        quality: "fast", "balanced", "quality" — overridden by strategy
        vram_gb: Available VRAM. 0 = auto-detect.

    Returns:
        ModelSelection with model name, reason, and strategy used.
    """
    if vram_gb <= 0:
        vram_gb = _get_vram_gb()

    # 1. Explicit model from caller (Opus's direct decision)
    if model:
        return ModelSelection(
            model=model,
            reason=f"Caller specified model: {model}",
            strategy="caller_specified",
        )

    # 2. Named strategy from caller
    if strategy and strategy in STRATEGIES:
        strat = STRATEGIES[strategy]
        for candidate in strat["prefer"]:
            if _model_fits_vram(candidate, vram_gb) and _model_good_for_task(candidate, task) != "weak":
                return ModelSelection(
                    model=candidate,
                    reason=f"Strategy '{strategy}': {strat['description']}",
                    strategy=strategy,
                )

    # 3. Auto-select: consider task, input size, urgency, quality
    # Map quality hint to effective strategy
    if urgent or quality == "fast":
        effective_strategy = "fast"
    elif quality == "quality":
        effective_strategy = "quality"
    else:
        effective_strategy = "balanced"

    # Special cases
    if task == "vision":
        effective_strategy = "vision"
    elif task == "critique" and input_len > 3000:
        effective_strategy = "quality"
    elif task == "react":
        # v4 is specifically trained for ReAct
        return ModelSelection(
            model="gemma4-agent-coder-v4:latest",
            reason="v4 trained for ReAct (3x faster than 31b, equal precision)",
            strategy="auto",
        )

    # Input complexity upgrade
    if input_len > 8000 and effective_strategy == "balanced":
        effective_strategy = "quality"

    # Select from effective strategy
    strat = STRATEGIES.get(effective_strategy, STRATEGIES["balanced"])
    for candidate in strat["prefer"]:
        if _model_fits_vram(candidate, vram_gb) and _model_good_for_task(candidate, task) != "weak":
            return ModelSelection(
                model=candidate,
                reason=f"Auto ({effective_strategy}): task={task}, input={input_len}chars",
                strategy=f"auto_{effective_strategy}",
            )

    # Fallback to legacy tier-based selection
    return ModelSelection(
        model=_legacy_select(task, vram_gb),
        reason="Fallback to legacy tier selection",
        strategy="legacy_fallback",
    )


def _legacy_select(task: str, vram_gb: float) -> str:
    """Legacy fixed-tier selection as ultimate fallback."""
    for (min_gb, max_gb), models in MODEL_TIERS.items():
        if min_gb <= vram_gb < max_gb:
            return models.get(task, models.get("text", "gemma4:e2b"))
    return "gemma4:e2b"


# Backward compatibility
def auto_select_model(
    task: str = "text",
    vram_gb: float = 0,
    input_len: int = 0,
) -> str:
    """Legacy API — delegates to select_model()."""
    result = select_model(task=task, vram_gb=vram_gb, input_len=input_len)
    return result.model


def recommend_models(vram_gb: float = 0) -> dict[str, str]:
    """Legacy API — return task→model mapping for a VRAM tier."""
    if vram_gb <= 0:
        vram_gb = _get_vram_gb()
    for (min_gb, max_gb), models in MODEL_TIERS.items():
        if min_gb <= vram_gb < max_gb:
            return models
    return MODEL_TIERS[(0, 10)]


def gpu_summary() -> dict:
    """Return GPU info, capabilities, and available strategies."""
    gpu = detect_gpu()
    models = recommend_models(gpu.vram_gb)
    available_models = {
        name: caps for name, caps in MODEL_CAPABILITIES.items()
        if caps["vram_gb"] <= gpu.vram_gb
    }
    return {
        "gpu": {"name": gpu.name, "vram_gb": gpu.vram_gb},
        "recommended_models": models,
        "available_models": {name: caps["speed"] for name, caps in available_models.items()},
        "strategies": {name: s["description"] for name, s in STRATEGIES.items()},
    }
