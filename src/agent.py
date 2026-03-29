"""HelixAgent: core logic for task delegation to local Ollama models."""

from __future__ import annotations

from dataclasses import dataclass, field

from .ollama_client import OllamaClient
from .router import Capability, ModelRouter


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

    async def models(self, action: str = "list") -> dict:
        """Get information about available Ollama models."""
        available = await self.client.is_available()
        if not available:
            return {"error": "Ollama is not running. Start with: ollama serve"}

        if action == "status":
            return {"status": "connected", "host": self.config.ollama_host}

        await self.router.refresh()

        if action == "capabilities":
            cap_map = await self.router.get_capabilities_map()
            return {"capabilities": cap_map}

        # Default: list
        models_list = []
        for info in self.router.get_all_models():
            models_list.append({
                "name": info.name,
                "size_gb": round(info.size_gb, 1),
                "parameters": info.parameter_size,
                "family": info.family,
                "capabilities": [c.value for c in info.capabilities],
            })
        return {"models": models_list, "count": len(models_list)}

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
