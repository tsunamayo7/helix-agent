"""HelixAgent: core logic for task delegation to local Ollama models."""

from __future__ import annotations

from dataclasses import dataclass, field

from .ollama_client import OllamaClient
from .react_loop import ReactLoop
from .router import Capability, ModelRouter
from .builtin_tools import create_full_registry
from .tools import create_default_registry


@dataclass
class AgentConfig:
    ollama_host: str = "http://localhost:11434"
    ollama_timeout: float = 120.0
    default_mode: str = "quality"  # quality | fast | creative
    max_output_tokens: int = 4096
    result_summary: bool = True  # Summarize long outputs to save context


class HelixAgent:
    """Orchestrates task delegation to local Ollama models."""

    def __init__(self, config: AgentConfig | None = None):
        self.config = config or AgentConfig()
        self.client = OllamaClient(
            host=self.config.ollama_host,
            timeout=self.config.ollama_timeout,
        )
        self.router = ModelRouter(self.client)

    async def agent(
        self,
        task: str,
        context: str = "",
        model: str = "auto",
        mode: str = "",
        max_steps: int = 10,
        tools: list[str] | None = None,
    ) -> dict:
        """Run a ReAct agent loop: the local LLM reasons and acts iteratively."""
        mode = mode or self.config.default_mode

        # Model selection
        if model == "auto":
            try:
                selected = await self.router.select_for_task(task, mode=mode)
            except Exception:
                return {"error": "Cannot connect to Ollama. Is it running?"}
            if not selected:
                return {"error": "No Ollama models available. Run: ollama pull gemma3"}
        else:
            selected = model

        # Build tool registry (full set with file access)
        registry = create_full_registry()

        # Filter tools if specified
        if tools:
            from .tools import ToolRegistry
            filtered = ToolRegistry()
            for name in tools:
                tool = registry.get(name)
                if tool:
                    filtered.register(tool)
            registry = filtered

        # Run ReAct loop
        loop = ReactLoop(
            client=self.client,
            tools=registry,
            max_steps=max_steps,
        )

        try:
            result = await loop.run(
                task=task,
                model=selected,
                context=context,
                temperature=0.1,
            )
        except Exception as e:
            return {"error": f"Agent loop failed: {e}", "model": selected}

        return result.to_dict()

    async def think(
        self,
        task: str,
        context: str = "",
        model: str = "auto",
        mode: str = "",
    ) -> dict:
        """Delegate a reasoning/analysis/code task to a local Ollama model."""
        mode = mode or self.config.default_mode

        # Model selection
        if model == "auto":
            try:
                selected = await self.router.select_for_task(task, mode=mode)
            except Exception:
                return {"error": "Cannot connect to Ollama. Is it running? (ollama serve)"}
            if not selected:
                return {"error": "No Ollama models available. Run: ollama pull gemma3"}
        else:
            selected = model

        # Build messages
        system_prompt = self._build_system_prompt(mode)
        user_content = task
        if context:
            user_content = f"{task}\n\n---\nContext:\n{context}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # Temperature based on mode
        temperature = {"quality": 0.3, "fast": 0.5, "creative": 0.9}.get(mode, 0.5)

        try:
            result = await self.client.chat(
                model=selected,
                messages=messages,
                temperature=temperature,
            )
        except Exception as e:
            return {"error": f"Ollama request failed: {e}", "model": selected}

        return {
            "result": result,
            "model": selected,
            "mode": mode,
            "task": task[:100],
        }

    async def see(
        self,
        image_path: str,
        question: str = "Describe what you see in this image in detail.",
        model: str = "auto",
    ) -> dict:
        """Analyze an image using a local Vision LLM."""
        import base64
        from pathlib import Path

        # Model selection
        if model == "auto":
            selected = await self.router.select(Capability.VISION)
            if not selected:
                return {"error": "No Vision model available. Run: ollama pull mistral-small3.2"}
        else:
            selected = model

        # Load image
        path = Path(image_path)
        if not path.exists():
            return {"error": f"Image not found: {image_path}"}

        image_data = base64.b64encode(path.read_bytes()).decode("utf-8")

        try:
            result = await self.client.chat_vision(
                model=selected,
                prompt=question,
                images=[image_data],
                temperature=0.3,
            )
        except Exception as e:
            return {"error": f"Vision request failed: {e}", "model": selected}

        return {
            "result": result,
            "model": selected,
            "image": image_path,
        }

    async def models(self, action: str = "list", model_name: str = "") -> dict:
        """Get information about available Ollama models."""
        available = await self.client.is_available()
        if not available:
            return {"error": "Ollama is not running. Start with: ollama serve"}

        if action == "status":
            return {"status": "connected", "host": self.config.ollama_host}

        if action == "use":
            if not model_name:
                return {"error": "model_name is required for 'use' action"}
            self.router.set_model_override(model_name)
            return {"model_override": model_name, "message": f"All requests will now use: {model_name}"}

        if action == "use_auto":
            self.router.set_model_override(None)
            return {"model_override": None, "message": "Switched back to auto-selection"}

        if action == "probe":
            await self.router.refresh()
            results = await self.router.probe_models()
            summary = []
            for name, ok in results.items():
                info = self.router._models.get(name)
                entry = {"name": name, "available": ok}
                if info and info.avg_response_sec:
                    entry["response_sec"] = info.avg_response_sec
                if info:
                    entry["size_gb"] = round(info.size_gb, 1)
                summary.append(entry)
            available_count = sum(1 for v in results.values() if v)
            return {
                "probed": len(results),
                "available": available_count,
                "unavailable": len(results) - available_count,
                "models": summary,
            }

        if action == "benchmark":
            return await self._run_benchmark(model_name)

        if action == "benchmark_status":
            return self._benchmark_status()

        fetch_meta = action == "detailed"
        await self.router.refresh(fetch_metadata=fetch_meta)

        if action == "capabilities":
            cap_map = await self.router.get_capabilities_map()
            return {"capabilities": cap_map}

        # Default: list (include benchmark scores if available)
        models_list = []
        for info in self.router.get_all_models():
            entry = {
                "name": info.name,
                "size_gb": round(info.size_gb, 1),
                "parameters": info.parameter_size,
                "param_billions": info.param_billions,
                "family": info.family,
                "capabilities": [c.value for c in info.capabilities],
            }
            if info.context_length:
                entry["context_length"] = info.context_length
            # Add benchmark score if available
            bm = self.router.benchmark_engine.get_cached(info.name)
            if bm:
                entry["benchmark_score"] = bm.total_score
                entry["benchmark_categories"] = bm.category_scores
            models_list.append(entry)

        result = {"models": models_list, "count": len(models_list)}

        # Show override status
        override = self.router.get_model_override()
        if override:
            result["model_override"] = override

        return result

    async def _run_benchmark(self, model_name: str = "") -> dict:
        """Run benchmarks on specified model or all unbenchmarked models.

        Includes preflight VRAM check, adaptive timeout, warmup, and lite mode
        for large models (>30GB).
        """
        from .benchmark import preflight_check

        await self.router.refresh()
        engine = self.router.benchmark_engine
        all_models = [info.name for info in self.router.get_all_models()
                       if Capability.EMBEDDING not in info.capabilities
                       or len(info.capabilities) > 1]

        if model_name:
            targets = [model_name]
        else:
            targets = engine.get_unbenchmarked(all_models)
            if not targets:
                return {
                    "message": "All models already benchmarked",
                    "benchmarked": len(engine.get_all_cached()),
                    "hint": "Use model_name to re-benchmark a specific model",
                }

        results = []
        for target in targets:
            info = self.router._models.get(target)
            if info and Capability.EMBEDDING in info.capabilities and len(info.capabilities) == 1:
                continue

            size_gb = info.size_gb if info else 0.0

            # Preflight VRAM check for large models
            if size_gb > 30:
                check = preflight_check(size_gb)
                if not check["can_run"]:
                    results.append({
                        "model": target,
                        "skipped": True,
                        "reason": check["reason"],
                        "size_gb": round(size_gb, 1),
                    })
                    continue

            # Use lite mode for large models (>30GB)
            use_lite = size_gb > 30

            try:
                bm = await engine.run_benchmark(
                    target,
                    model_size_gb=size_gb,
                    lite=use_lite,
                    warmup=True,
                )

                entry = {
                    "model": target,
                    "total_score": bm.total_score,
                    "categories": bm.category_scores,
                    "avg_tps": bm.avg_tokens_per_sec,
                    "size_gb": round(size_gb, 1),
                }
                if use_lite:
                    entry["mode"] = "lite"
                # Check if warmup failed
                if bm.results and isinstance(bm.results[0], dict) and bm.results[0].get("warmup_failed"):
                    entry["warmup_failed"] = True
                    entry["error"] = bm.results[0].get("error", "warmup failed")
                results.append(entry)
            except Exception as e:
                results.append({
                    "model": target,
                    "error": str(e),
                    "size_gb": round(size_gb, 1),
                })

        return {
            "benchmarked": len(results),
            "results": results,
            "total_cached": len(engine.get_all_cached()),
        }

    def _benchmark_status(self) -> dict:
        """Get current benchmark status."""
        engine = self.router.benchmark_engine
        cached = engine.get_all_cached()

        models_summary = []
        for name, bm in cached.items():
            models_summary.append({
                "model": name,
                "total_score": bm.total_score,
                "categories": bm.category_scores,
                "avg_tps": bm.avg_tokens_per_sec,
                "timestamp": bm.timestamp,
            })

        # Sort by total score descending
        models_summary.sort(key=lambda x: x["total_score"], reverse=True)

        override = self.router.get_model_override()
        result = {
            "benchmarked_models": len(cached),
            "ranking": models_summary,
        }
        if override:
            result["model_override"] = override
        return result

    async def config_action(self, action: str = "show", key: str = "", value: str = "") -> dict:
        """View or update agent configuration."""
        if action == "show":
            return {
                "ollama_host": self.config.ollama_host,
                "default_mode": self.config.default_mode,
                "max_output_tokens": self.config.max_output_tokens,
                "result_summary": self.config.result_summary,
            }
        elif action == "set":
            if not key:
                return {"error": "key is required for 'set' action"}
            if hasattr(self.config, key):
                old = getattr(self.config, key)
                # Type coercion
                if isinstance(old, bool):
                    setattr(self.config, key, value.lower() in ("true", "1", "yes"))
                elif isinstance(old, int):
                    setattr(self.config, key, int(value))
                elif isinstance(old, float):
                    setattr(self.config, key, float(value))
                else:
                    setattr(self.config, key, value)
                return {"updated": key, "old": str(old), "new": value}
            return {"error": f"Unknown config key: {key}"}
        return {"error": f"Unknown action: {action}"}

    def _build_system_prompt(self, mode: str) -> str:
        base = (
            "You are a helpful local AI assistant running via Ollama. "
            "Your output will be reviewed by a more capable AI (Claude), "
            "so focus on accuracy and useful content rather than politeness. "
            "Be concise and direct."
        )
        if mode == "quality":
            return base + " Prioritize accuracy and thoroughness. Think step by step if needed."
        elif mode == "fast":
            return base + " Be extremely brief. One paragraph max."
        elif mode == "creative":
            return base + " Be creative and explore unconventional ideas."
        return base
