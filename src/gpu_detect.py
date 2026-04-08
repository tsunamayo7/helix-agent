"""Auto-detect GPU VRAM and select optimal model for each task.

Enables helix-agent to work on any GPU from 4GB to 96GB VRAM,
automatically choosing the best model for the available hardware.

Benchmark results (RTX PRO 6000):
  gemma4:e2b  (~4GB VRAM): DOM 9.6s, Review 3.2s — fast, good enough
  gemma4:e4b  (~6GB VRAM): DOM 11.4s, Review 4.4s — sweet spot
  gemma4:31b (~20GB VRAM): DOM 11.9s, Review 6.1s — most accurate
"""

from __future__ import annotations

import subprocess
import json
from dataclasses import dataclass


@dataclass
class GPUInfo:
    name: str = "unknown"
    vram_mb: int = 0
    vram_gb: float = 0.0


# Model recommendations by VRAM tier
# Benchmark results (2026-04-08, clip-bridge 501 lines):
#   review:    31b=5件(130s) >> e2b=1件(46s) > e4b=0件(35s)
#   summarize: e2b=4s(OK) << e4b=12s(best) << 31b=21s(OK)
#   translate: e2b=3s(OK) ≈ e4b=15s(OK) ≈ 31b=12s(OK)
#   code_gen:  e4b=50s(OK) >> e2b/31b(fail)
#   classify:  e2b=6s(OK) ≈ e4b=13s(OK) ≈ 31b=23s(OK)
#   search:    e4b=25s(best) > e2b=18s(OK) > 31b=80s(short)
MODEL_TIERS = {
    # (min_vram_gb, max_vram_gb): {task: model}
    # 8GB GPU (RTX 4060, RTX 3060, etc.)
    (0, 10): {
        "vision": "gemma4:e2b",
        "text": "gemma4:e2b",
        "review": "gemma4:e2b",
        "reasoning": "gemma4:e2b",
        "summarize": "gemma4:e2b",
        "translate": "gemma4:e2b",
        "classify": "gemma4:e2b",
        "code_gen": "gemma4:e2b",
        "search": "gemma4:e2b",
    },
    # 16GB GPU (RTX 4070 Ti, RTX 5070 Ti, etc.)
    (10, 20): {
        "vision": "gemma4:e4b",
        "text": "gemma4:e4b",
        "review": "gemma4:e4b",
        "reasoning": "gemma4:e4b",
        "summarize": "gemma4:e2b",
        "translate": "gemma4:e2b",
        "classify": "gemma4:e2b",
        "code_gen": "gemma4:e4b",
        "search": "gemma4:e4b",
    },
    # 24GB GPU (RTX 4090, RTX 3090, etc.)
    (20, 32): {
        "vision": "gemma4:26b",
        "text": "gemma4:26b",
        "review": "gemma4:26b",
        "reasoning": "gemma4:26b",
        "summarize": "gemma4:e2b",
        "translate": "gemma4:e2b",
        "classify": "gemma4:e2b",
        "code_gen": "gemma4:e4b",
        "search": "gemma4:e4b",
    },
    # 48GB+ GPU (RTX PRO 6000, A6000, etc.)
    (32, 64): {
        "vision": "qwen3-vl:32b",
        "text": "gemma4:31b",
        "review": "gemma4:31b",
        "reasoning": "gemma4:31b",
        "summarize": "gemma4:e2b",
        "translate": "gemma4:e2b",
        "classify": "gemma4:e2b",
        "code_gen": "gemma4:e4b",
        "search": "gemma4:e4b",
    },
    # 64GB+ GPU (RTX PRO 6000 96GB, multi-GPU, etc.)
    (64, 1000): {
        "vision": "qwen3-vl:32b",
        "text": "qwen3.5:72b",
        "review": "gemma4:31b",
        "reasoning": "qwen3.5:122b",
        "summarize": "gemma4:e2b",
        "translate": "gemma4:e2b",
        "classify": "gemma4:e2b",
        "code_gen": "gemma4:e4b",
        "search": "gemma4:e4b",
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
            # Take the GPU with most VRAM if multiple
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


def recommend_models(vram_gb: float = 0) -> dict[str, str]:
    """Recommend optimal models based on available VRAM.

    Args:
        vram_gb: Available VRAM in GB. If 0, auto-detect.

    Returns:
        Dict mapping task names to recommended model names.
    """
    if vram_gb <= 0:
        gpu = detect_gpu()
        vram_gb = gpu.vram_gb

    if vram_gb <= 0:
        # No GPU detected, use smallest models
        vram_gb = 4

    for (min_gb, max_gb), models in MODEL_TIERS.items():
        if min_gb <= vram_gb < max_gb:
            return models

    # Fallback to smallest
    return MODEL_TIERS[(0, 10)]


# Complexity-based model upgrade rules.
# When input exceeds a threshold, upgrade from the tier default to a stronger model.
# Format: {task: [(char_threshold, upgrade_model), ...]} — evaluated in order, first match wins.
_COMPLEXITY_UPGRADES: dict[str, list[tuple[int, str]]] = {
    "summarize":  [(8000, "gemma4:31b"), (3000, "gemma4:e4b")],
    "translate":  [(5000, "gemma4:31b"), (2000, "gemma4:e4b")],
    "classify":   [(10000, "gemma4:e4b")],
    "code_gen":   [(5000, "gemma4:31b")],
    "search":     [(3000, "gemma4:31b")],
    "review":     [],  # already uses strongest model
    "reasoning":  [],  # already uses strongest model
}


def auto_select_model(
    task: str = "text",
    vram_gb: float = 0,
    input_len: int = 0,
) -> str:
    """Select the optimal model for a specific task, considering input complexity.

    Args:
        task: One of "vision", "text", "review", "reasoning",
              "summarize", "translate", "classify", "code_gen", "search"
        vram_gb: Available VRAM in GB. If 0, auto-detect.
        input_len: Length of input text in characters. If >0, may upgrade
                   to a stronger model for complex inputs.

    Returns:
        Model name string (e.g., "gemma4:e4b")
    """
    models = recommend_models(vram_gb)
    base_model = models.get(task, models.get("text", "gemma4:e2b"))

    # Upgrade based on input complexity
    if input_len > 0 and task in _COMPLEXITY_UPGRADES:
        for threshold, upgrade_model in _COMPLEXITY_UPGRADES[task]:
            if input_len >= threshold:
                return upgrade_model

    return base_model


def gpu_summary() -> dict:
    """Return a summary of GPU info and recommended models."""
    gpu = detect_gpu()
    models = recommend_models(gpu.vram_gb)
    return {
        "gpu": {
            "name": gpu.name,
            "vram_gb": gpu.vram_gb,
        },
        "recommended_models": models,
        "tiers": {
            "8GB_GPU": {k: v for (mn, mx), v in MODEL_TIERS.items() if mn == 0 for k, v in v.items()},
            "16GB_GPU": {k: v for (mn, mx), v in MODEL_TIERS.items() if mn == 10 for k, v in v.items()},
            "24GB_GPU": {k: v for (mn, mx), v in MODEL_TIERS.items() if mn == 20 for k, v in v.items()},
            "48GB_GPU": {k: v for (mn, mx), v in MODEL_TIERS.items() if mn == 32 for k, v in v.items()},
        },
    }
