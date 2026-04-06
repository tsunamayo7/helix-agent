"""Benchmark different LLM models for helix-agent tasks.

Compares accuracy, speed, and token efficiency across model sizes
for vision_compress, dom_compress, and evolving_memory tasks.

Usage:
    uv run python scripts/benchmark_models.py
    uv run python scripts/benchmark_models.py --task vision
    uv run python scripts/benchmark_models.py --task dom
    uv run python scripts/benchmark_models.py --task review
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx


OLLAMA_URL = "http://localhost:11434"

# Models to benchmark (add/remove as available)
MODELS = [
    "gemma4:e2b",   # ~4GB VRAM  (8GB GPU)
    "gemma4:e4b",   # ~6GB VRAM  (16GB GPU)
    "gemma4:26b",   # ~12GB VRAM (24GB GPU, MoE)
    "gemma4:31b",   # ~20GB VRAM (48GB+ GPU, dense)
]

# Test data
VISION_TEST_PROMPT = (
    "Analyze this screenshot and extract: page type, main content summary, "
    "interactive elements (buttons, links, inputs), and any visible text. "
    "Return as JSON."
)

DOM_TEST_HTML = """
<html><head><title>Test Page</title></head><body>
<nav><a href="/">Home</a><a href="/about">About</a><a href="/contact">Contact</a></nav>
<main>
<h1>Welcome to Our Service</h1>
<p>We provide AI-powered solutions for developers.</p>
<form action="/signup" method="post">
<input type="email" placeholder="Email" required>
<input type="password" placeholder="Password" required>
<button type="submit">Sign Up</button>
</form>
<div class="features">
<div class="feature"><h3>Fast</h3><p>Lightning fast responses</p></div>
<div class="feature"><h3>Secure</h3><p>Enterprise-grade security</p></div>
<div class="feature"><h3>Scalable</h3><p>Grows with your needs</p></div>
</div>
</main>
<footer><p>&copy; 2026 TestCorp</p></footer>
</body></html>
"""

REVIEW_TEST_CONVERSATION = {
    "user": "Claude Codeでブラウザ操作するときにPlaywright MCPだとトークンが15Kも消費されるので、agent-browserに切り替えたい",
    "assistant": "agent-browserに切り替えました。Playwrightと比較して82-93%のトークン削減が確認できました。設定はhelix-agentのcomputer_useツール経由で自動ルーティングされます。",
}


@dataclass
class BenchmarkResult:
    model: str
    task: str
    latency_ms: int
    output_length: int
    output_quality: str  # "valid_json" | "text" | "error"
    output_preview: str
    tokens_estimated: int = 0


async def check_model_available(model: str) -> bool:
    """Check if model is available in Ollama."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                return model in models or any(model in m for m in models)
    except Exception:
        pass
    return False


async def run_generate(model: str, prompt: str, images: list[str] | None = None) -> tuple[str, int]:
    """Run Ollama generate and return (response, latency_ms)."""
    start = time.monotonic()
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if images:
        payload["images"] = images

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            return resp.json().get("response", ""), latency
        return f"Error: {resp.status_code}", latency


async def benchmark_vision(model: str, image_path: str) -> BenchmarkResult:
    """Benchmark vision_compress task."""
    import base64

    img_data = Path(image_path).read_bytes()
    img_b64 = base64.b64encode(img_data).decode()

    response, latency = await run_generate(model, VISION_TEST_PROMPT, images=[img_b64])

    # Check output quality
    quality = "text"
    try:
        json.loads(response)
        quality = "valid_json"
    except (json.JSONDecodeError, TypeError):
        if response.startswith("Error"):
            quality = "error"

    return BenchmarkResult(
        model=model,
        task="vision_compress",
        latency_ms=latency,
        output_length=len(response),
        output_quality=quality,
        output_preview=response[:200],
        tokens_estimated=len(response) // 4,
    )


async def benchmark_dom(model: str) -> BenchmarkResult:
    """Benchmark dom_compress task."""
    prompt = (
        "Compress this HTML into a structured JSON summary. Extract only: "
        "page title, navigation links, form fields, main content summary, "
        "and interactive elements.\n\n" + DOM_TEST_HTML
    )

    response, latency = await run_generate(model, prompt)

    quality = "text"
    try:
        json.loads(response)
        quality = "valid_json"
    except (json.JSONDecodeError, TypeError):
        if response.startswith("Error"):
            quality = "error"

    return BenchmarkResult(
        model=model,
        task="dom_compress",
        latency_ms=latency,
        output_length=len(response),
        output_quality=quality,
        output_preview=response[:200],
        tokens_estimated=len(response) // 4,
    )


async def benchmark_review(model: str) -> BenchmarkResult:
    """Benchmark evolving_memory review task."""
    prompt = (
        "Analyze this conversation turn and decide if anything should be saved.\n\n"
        f"User: {REVIEW_TEST_CONVERSATION['user']}\n"
        f"Assistant: {REVIEW_TEST_CONVERSATION['assistant']}\n\n"
        'Return JSON: {"should_save": bool, "content": "...", "type": "preference|correction|fact"}\n'
        'If nothing to save: {"should_save": false}'
    )

    response, latency = await run_generate(model, prompt)

    quality = "text"
    try:
        parsed = json.loads(response)
        if "should_save" in parsed:
            quality = "valid_json"
    except (json.JSONDecodeError, TypeError):
        if response.startswith("Error"):
            quality = "error"

    return BenchmarkResult(
        model=model,
        task="evolving_memory",
        latency_ms=latency,
        output_length=len(response),
        output_quality=quality,
        output_preview=response[:200],
        tokens_estimated=len(response) // 4,
    )


async def main():
    parser = argparse.ArgumentParser(description="Benchmark LLM models for helix-agent")
    parser.add_argument("--task", choices=["vision", "dom", "review", "all"], default="all")
    parser.add_argument("--image", default="", help="Image path for vision benchmark")
    parser.add_argument("--runs", type=int, default=1, help="Number of runs per model")
    args = parser.parse_args()

    print("=" * 70)
    print("helix-agent Model Benchmark")
    print("=" * 70)

    # Check available models
    available = []
    for model in MODELS:
        if await check_model_available(model):
            available.append(model)
            print(f"  [OK] {model}")
        else:
            print(f"  [--] {model} (not installed)")

    if not available:
        print("\nNo models available. Install with: ollama pull gemma4:4b")
        return

    results: list[BenchmarkResult] = []

    # DOM benchmark
    if args.task in ("dom", "all"):
        print(f"\n{'─' * 70}")
        print("[DOM] DOM Compress Benchmark")
        print(f"{'─' * 70}")
        for model in available:
            print(f"  Running {model}...", end=" ", flush=True)
            try:
                r = await benchmark_dom(model)
                results.append(r)
                print(f"{r.latency_ms}ms | {r.output_quality} | {r.output_length} chars")
            except Exception as e:
                print(f"ERROR: {e}")

    # Review benchmark
    if args.task in ("review", "all"):
        print(f"\n{'─' * 70}")
        print("[MEM] Evolving Memory Review Benchmark")
        print(f"{'─' * 70}")
        for model in available:
            print(f"  Running {model}...", end=" ", flush=True)
            try:
                r = await benchmark_review(model)
                results.append(r)
                print(f"{r.latency_ms}ms | {r.output_quality} | {r.output_length} chars")
            except Exception as e:
                print(f"ERROR: {e}")

    # Vision benchmark (requires image)
    if args.task in ("vision", "all") and args.image:
        print(f"\n{'─' * 70}")
        print("[VIS] Vision Compress Benchmark")
        print(f"{'─' * 70}")
        for model in available:
            print(f"  Running {model}...", end=" ", flush=True)
            try:
                r = await benchmark_vision(model, args.image)
                results.append(r)
                print(f"{r.latency_ms}ms | {r.output_quality} | {r.output_length} chars")
            except Exception as e:
                print(f"ERROR: {e}")

    # Summary table
    if results:
        print(f"\n{'=' * 70}")
        print("[RESULT] Summary")
        print(f"{'=' * 70}")
        print(f"{'Model':<20} {'Task':<20} {'Latency':<12} {'Quality':<12} {'Chars':<8}")
        print(f"{'─' * 20} {'─' * 20} {'─' * 12} {'─' * 12} {'─' * 8}")
        for r in results:
            print(f"{r.model:<20} {r.task:<20} {r.latency_ms:>8}ms  {r.output_quality:<12} {r.output_length:>6}")

        # Save results
        out_path = Path(__file__).parent.parent / "benchmark_results.json"
        out_data = [
            {
                "model": r.model,
                "task": r.task,
                "latency_ms": r.latency_ms,
                "output_length": r.output_length,
                "output_quality": r.output_quality,
                "output_preview": r.output_preview,
            }
            for r in results
        ]
        out_path.write_text(json.dumps(out_data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n[SAVE] Results saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
