"""Provider runtime for helix-agent.

Supports multiple LLM backends while keeping Ollama as a first-class path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from .builtin_tools import create_full_registry
from .ollama_client import OllamaClient
from .react_loop import ReactLoop
from .router import Capability, ModelRouter, _infer_capability
from .tools import ToolRegistry

SUPPORTED_PROVIDERS = ("ollama", "codex", "openai-compatible")
VALID_CODEX_SANDBOXES = frozenset({"read-only", "workspace-write", "danger-full-access"})
VALID_CODEX_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})


def build_system_prompt(mode: str, provider_name: str) -> str:
    base = (
        f"You are a helpful AI assistant running via {provider_name}. "
        "Your output will be reviewed by a more capable orchestrator, "
        "so focus on accuracy and useful content rather than politeness. "
        "Be concise and direct."
    )
    if mode == "quality":
        return base + " Prioritize accuracy and thoroughness. Think step by step if needed."
    if mode == "fast":
        return base + " Be extremely brief. One paragraph max."
    if mode == "creative":
        return base + " Be creative and explore unconventional ideas."
    return base


def build_user_content(task: str, context: str = "") -> str:
    if not context:
        return task
    return f"{task}\n\n---\nContext:\n{context}"


class ChatBackend(Protocol):
    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        format_json: bool = False,
        num_ctx: int | None = None,
    ) -> str: ...


@dataclass
class ProviderStatus:
    provider: str
    available: bool
    configured: bool
    default_model: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "available": self.available,
            "configured": self.configured,
            "default_model": self.default_model,
            "details": self.details,
        }


class OpenAICompatibleClient:
    """Small async client for OpenAI-compatible chat endpoints."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        api_key_env: str = "OPENAI_API_KEY",
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.timeout = timeout

    def _resolved_key(self) -> str:
        return self.api_key or os.getenv(self.api_key_env, "")

    def _headers(self) -> dict[str, str]:
        key = self._resolved_key()
        if not key:
            return {}
        return {"Authorization": f"Bearer {key}"}

    async def is_available(self) -> bool:
        if not self._resolved_key():
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/models", headers=self._headers())
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self.base_url}/models", headers=self._headers())
            response.raise_for_status()
            return response.json().get("data", [])

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
            "temperature": temperature,
        }
        if format_json:
            payload["response_format"] = {"type": "json_object"}
        if num_ctx:
            payload["max_tokens"] = num_ctx

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()
            message = data.get("choices", [{}])[0].get("message", {})
            content = message.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(str(part.get("text", "")))
                return "\n".join(text_parts).strip()
            return str(content)


class OllamaProvider:
    provider_name = "ollama"

    def __init__(self, config):
        self.config = config
        self.client = OllamaClient(
            host=self.config.ollama_host,
            timeout=self.config.ollama_timeout,
        )
        self.router = ModelRouter(self.client)

    async def status(self) -> ProviderStatus:
        available = await self.client.is_available()
        return ProviderStatus(
            provider=self.provider_name,
            available=available,
            configured=True,
            details={"host": self.config.ollama_host},
        )

    async def select_model(self, task: str, mode: str, model: str = "auto") -> str | None:
        if model != "auto":
            return model
        return await self.router.select_for_task(task, mode=mode)

    async def think(
        self,
        task: str,
        *,
        context: str = "",
        model: str = "auto",
        mode: str = "quality",
    ) -> dict:
        selected = await self.select_model(task, mode, model)
        if not selected:
            return {"error": "No Ollama models available. Run: ollama pull gemma3", "provider": self.provider_name}

        messages = [
            {"role": "system", "content": build_system_prompt(mode, "Ollama")},
            {"role": "user", "content": build_user_content(task, context)},
        ]
        temperature = {"quality": 0.3, "fast": 0.5, "creative": 0.9}.get(mode, 0.5)

        try:
            result = await self.client.chat(
                model=selected,
                messages=messages,
                temperature=temperature,
            )
        except Exception as exc:
            return {"error": f"Ollama request failed: {exc}", "model": selected, "provider": self.provider_name}

        return {
            "result": result,
            "model": selected,
            "mode": mode,
            "task": task[:100],
            "provider": self.provider_name,
        }

    async def agent(
        self,
        task: str,
        *,
        context: str = "",
        model: str = "auto",
        mode: str = "quality",
        max_steps: int = 10,
        tools: list[str] | None = None,
        on_progress=None,
    ) -> dict:
        selected = await self.select_model(task, mode, model)
        if not selected:
            return {"error": "No Ollama models available. Run: ollama pull gemma3", "provider": self.provider_name}

        registry = create_full_registry()
        if tools:
            filtered = ToolRegistry()
            for name in tools:
                tool = registry.get(name)
                if tool:
                    filtered.register(tool)
            registry = filtered

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
                on_progress=on_progress,
            )
        except Exception as exc:
            return {"error": f"Agent loop failed: {exc}", "model": selected, "provider": self.provider_name}

        payload = result.to_dict()
        payload["provider"] = self.provider_name
        return payload

    async def see(
        self,
        image_path: str,
        *,
        question: str,
        model: str = "auto",
    ) -> dict:
        selected = model
        if model == "auto":
            selected = await self.router.select(Capability.VISION)
            if not selected:
                return {
                    "error": "No Vision model available. Run: ollama pull mistral-small3.2",
                    "provider": self.provider_name,
                }

        path = Path(image_path)
        if not path.exists():
            return {"error": f"Image not found: {image_path}", "provider": self.provider_name}

        image_data = base64.b64encode(path.read_bytes()).decode("utf-8")
        try:
            result = await self.client.chat_vision(
                model=selected,
                prompt=question,
                images=[image_data],
                temperature=0.3,
            )
        except Exception as exc:
            return {"error": f"Vision request failed: {exc}", "model": selected, "provider": self.provider_name}

        return {
            "result": result,
            "model": selected,
            "image": image_path,
            "provider": self.provider_name,
        }

    async def models(self, action: str = "list", model_name: str = "") -> dict:
        available = await self.client.is_available()
        if not available:
            return {"error": "Ollama is not running. Start with: ollama serve", "provider": self.provider_name}

        if action == "status":
            return {"status": "connected", "host": self.config.ollama_host, "provider": self.provider_name}

        if action == "use":
            if not model_name:
                return {"error": "model_name is required for 'use' action", "provider": self.provider_name}
            self.router.set_model_override(model_name)
            return {
                "model_override": model_name,
                "message": f"All Ollama requests will now use: {model_name}",
                "provider": self.provider_name,
            }

        if action == "use_auto":
            self.router.set_model_override(None)
            return {
                "model_override": None,
                "message": "Switched Ollama back to auto-selection",
                "provider": self.provider_name,
            }

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
            available_count = sum(1 for value in results.values() if value)
            return {
                "probed": len(results),
                "available": available_count,
                "unavailable": len(results) - available_count,
                "models": summary,
                "provider": self.provider_name,
            }

        fetch_meta = action == "detailed"
        await self.router.refresh(fetch_metadata=fetch_meta)

        if action == "capabilities":
            cap_map = await self.router.get_capabilities_map()
            return {"capabilities": cap_map, "provider": self.provider_name}

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
            models_list.append(entry)

        result = {"models": models_list, "count": len(models_list), "provider": self.provider_name}
        override = self.router.get_model_override()
        if override:
            result["model_override"] = override
        return result


class OpenAICompatibleProvider:
    provider_name = "openai-compatible"

    def __init__(self, config):
        self.config = config
        self.client = OpenAICompatibleClient(
            base_url=self.config.openai_base_url,
            api_key=self.config.openai_api_key,
            api_key_env=self.config.openai_api_key_env,
            timeout=self.config.openai_timeout,
        )
        self._model_override: str | None = None

    def _default_model(self) -> str:
        return self._model_override or self.config.openai_model

    async def status(self) -> ProviderStatus:
        configured = bool(self.client._resolved_key())
        available = await self.client.is_available() if configured else False
        return ProviderStatus(
            provider=self.provider_name,
            available=available,
            configured=configured,
            default_model=self._default_model(),
            details={
                "base_url": self.config.openai_base_url,
                "api_key_env": self.config.openai_api_key_env,
            },
        )

    async def think(
        self,
        task: str,
        *,
        context: str = "",
        model: str = "auto",
        mode: str = "quality",
    ) -> dict:
        selected = self._default_model() if model == "auto" else model
        if not self.client._resolved_key():
            return {"error": "OPENAI-compatible provider is not configured.", "provider": self.provider_name}

        messages = [
            {"role": "system", "content": build_system_prompt(mode, "an OpenAI-compatible API")},
            {"role": "user", "content": build_user_content(task, context)},
        ]
        temperature = {"quality": 0.3, "fast": 0.2, "creative": 0.9}.get(mode, 0.3)

        try:
            result = await self.client.chat(
                model=selected,
                messages=messages,
                temperature=temperature,
            )
        except Exception as exc:
            return {"error": f"OpenAI-compatible request failed: {exc}", "model": selected, "provider": self.provider_name}

        return {
            "result": result,
            "model": selected,
            "mode": mode,
            "task": task[:100],
            "provider": self.provider_name,
        }

    async def agent(
        self,
        task: str,
        *,
        context: str = "",
        model: str = "auto",
        mode: str = "quality",
        max_steps: int = 10,
        tools: list[str] | None = None,
        on_progress=None,
    ) -> dict:
        selected = self._default_model() if model == "auto" else model
        if not self.client._resolved_key():
            return {"error": "OPENAI-compatible provider is not configured.", "provider": self.provider_name}

        registry = create_full_registry()
        if tools:
            filtered = ToolRegistry()
            for name in tools:
                tool = registry.get(name)
                if tool:
                    filtered.register(tool)
            registry = filtered

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
                on_progress=on_progress,
            )
        except Exception as exc:
            return {"error": f"Agent loop failed: {exc}", "model": selected, "provider": self.provider_name}

        payload = result.to_dict()
        payload["provider"] = self.provider_name
        return payload

    async def see(
        self,
        image_path: str,
        *,
        question: str,
        model: str = "auto",
    ) -> dict:
        return {
            "error": "Vision is not implemented for the generic OpenAI-compatible provider yet.",
            "provider": self.provider_name,
            "image": image_path,
            "question": question,
            "model": self._default_model() if model == "auto" else model,
        }

    async def models(self, action: str = "list", model_name: str = "") -> dict:
        if action == "use":
            if not model_name:
                return {"error": "model_name is required for 'use' action", "provider": self.provider_name}
            self._model_override = model_name
            return {
                "provider": self.provider_name,
                "model_override": model_name,
                "message": f"All OpenAI-compatible requests will now use: {model_name}",
            }

        if action == "use_auto":
            self._model_override = None
            return {
                "provider": self.provider_name,
                "model_override": None,
                "message": "Switched OpenAI-compatible routing back to the configured default model",
            }

        status = await self.status()
        if action == "status":
            return status.to_dict()
        if not status.configured:
            return {"error": "OPENAI-compatible provider is not configured.", "provider": self.provider_name}

        try:
            raw = await self.client.list_models()
        except Exception as exc:
            return {"error": f"Could not list models: {exc}", "provider": self.provider_name}

        models = []
        for item in raw:
            model_id = item.get("id", "")
            models.append(
                {
                    "name": model_id,
                    "provider": self.provider_name,
                    "capabilities": [_infer_capability(model_id).value],
                }
            )
        return {
            "provider": self.provider_name,
            "models": models,
            "count": len(models),
            "default_model": self._default_model(),
        }


@dataclass
class CodexTraceSummary:
    text: str
    thread_id: str | None = None
    tool_count: int = 0
    files_touched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _find_codex() -> str | None:
    return shutil.which("codex")


def _sanitize(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def _parse_codex_jsonl(output: str) -> CodexTraceSummary:
    messages: list[str] = []
    files_touched: list[str] = []
    errors: list[str] = []
    thread_id: str | None = None
    tool_count = 0

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")
        if event_type == "thread.started":
            thread_id = event.get("thread_id") or event.get("threadId")
        elif event_type == "item.completed":
            item = event.get("item", {})
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            if item_type == "agent_message" and item.get("text"):
                messages.append(str(item["text"]))
            elif item_type == "message":
                for part in item.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                        messages.append(str(part["text"]))
            elif item_type == "function_call":
                tool_count += 1
                arguments = item.get("arguments", "")
                if isinstance(arguments, str):
                    try:
                        parsed_args = json.loads(arguments)
                    except json.JSONDecodeError:
                        parsed_args = {}
                    if isinstance(parsed_args, dict):
                        path = parsed_args.get("path") or parsed_args.get("file_path")
                        if path:
                            files_touched.append(str(path))
        elif event_type == "turn.completed":
            summary = event.get("summary", "")
            if summary and not messages:
                messages.append(str(summary))
        elif event_type == "error":
            errors.append(str(event.get("message", event)))

    text = "\n\n".join(message for message in messages if message).strip()
    return CodexTraceSummary(
        text=_sanitize(text),
        thread_id=thread_id,
        tool_count=tool_count,
        files_touched=list(dict.fromkeys(files_touched)),
        errors=errors,
    )


class CodexProvider:
    provider_name = "codex"

    def __init__(self, config):
        self.config = config

    async def status(self) -> ProviderStatus:
        codex_path = _find_codex()
        return ProviderStatus(
            provider=self.provider_name,
            available=bool(codex_path),
            configured=bool(codex_path),
            default_model=self.config.codex_model,
            details={
                "path": codex_path or "",
                "sandbox": self.config.codex_sandbox,
                "effort": getattr(self.config, "codex_effort", "high"),
            },
        )

    def _build_cmd(self, model: str, sandbox: str, effort: str = "") -> list[str]:
        codex_path = _find_codex()
        if not codex_path:
            raise FileNotFoundError("Codex CLI not found in PATH.")
        cmd = [
            codex_path,
            "exec",
            "--json",
            "--model",
            model,
            "--sandbox",
            sandbox,
            "--full-auto",
            "--skip-git-repo-check",
            "--ephemeral",
        ]
        # reasoning_effort 指定 (none/minimal/low/medium/high/xhigh)
        # -c フラグでconfig.tomlのmodel_reasoning_effortを上書き
        if effort and effort in VALID_CODEX_EFFORTS:
            cmd.extend(["-c", f"model_reasoning_effort={effort}"])
        cmd.append("-")
        return cmd

    async def run(
        self,
        prompt: str,
        *,
        model: str = "",
        sandbox: str = "",
        effort: str = "",
        cwd: str | None = None,
        timeout: int = 180,
    ) -> dict:
        selected_model = model or self.config.codex_model
        selected_sandbox = sandbox or self.config.codex_sandbox
        selected_effort = effort or getattr(self.config, "codex_effort", "high")
        if selected_sandbox not in VALID_CODEX_SANDBOXES:
            return {
                "error": f"Invalid Codex sandbox: {selected_sandbox}",
                "provider": self.provider_name,
                "model": selected_model,
            }
        if selected_effort and selected_effort not in VALID_CODEX_EFFORTS:
            return {
                "error": f"Invalid Codex effort: {selected_effort}. Valid: {sorted(VALID_CODEX_EFFORTS)}",
                "provider": self.provider_name,
                "model": selected_model,
            }

        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_cmd(selected_model, selected_sandbox, selected_effort),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except Exception as exc:
            return {"error": f"Could not start Codex CLI: {exc}", "provider": self.provider_name, "model": selected_model}

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "error": f"Codex request timed out after {timeout}s",
                "provider": self.provider_name,
                "model": selected_model,
            }

        output = stdout.decode("utf-8", errors="replace")
        parsed = _parse_codex_jsonl(output)
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            error_text = stderr_text or "Codex exited with a non-zero status."
            if parsed.errors:
                error_text = f"{error_text}\n" + "\n".join(parsed.errors)
            return {
                "error": error_text,
                "provider": self.provider_name,
                "model": selected_model,
                "thread_id": parsed.thread_id,
                "tool_count": parsed.tool_count,
                "files_touched": parsed.files_touched,
            }

        result_text = parsed.text or _sanitize(output)
        return {
            "result": result_text,
            "provider": self.provider_name,
            "model": selected_model,
            "thread_id": parsed.thread_id,
            "tool_count": parsed.tool_count,
            "files_touched": parsed.files_touched,
            "sandbox": selected_sandbox,
            "effort": selected_effort,
        }

    async def think(
        self,
        task: str,
        *,
        context: str = "",
        model: str = "auto",
        mode: str = "quality",
        cwd: str | None = None,
        sandbox: str = "",
        effort: str = "",
        timeout: int = 180,
    ) -> dict:
        selected_model = self.config.codex_model if model == "auto" else model
        prompt = (
            f"{build_system_prompt(mode, 'Codex CLI')}\n\n"
            "Complete the task and return only the useful final result.\n\n"
            f"Task:\n{task}"
        )
        if context:
            prompt += f"\n\nContext:\n{context}"
        return await self.run(
            prompt,
            model=selected_model,
            sandbox=sandbox,
            effort=effort,
            cwd=cwd,
            timeout=timeout,
        )

    async def agent(
        self,
        task: str,
        *,
        context: str = "",
        model: str = "auto",
        mode: str = "quality",
        max_steps: int = 10,
        tools: list[str] | None = None,
        on_progress=None,
        cwd: str | None = None,
        sandbox: str = "",
        effort: str = "",
        timeout: int = 180,
    ) -> dict:
        del max_steps, tools, on_progress
        selected_model = self.config.codex_model if model == "auto" else model
        prompt = (
            f"{build_system_prompt(mode, 'Codex CLI')}\n\n"
            "Work as an autonomous implementation agent. Investigate, edit files if needed, "
            "run checks when appropriate, and then return a concise summary of what changed, "
            "what you verified, and any remaining risk.\n\n"
            f"Task:\n{task}"
        )
        if context:
            prompt += f"\n\nContext:\n{context}"
        result = await self.run(
            prompt,
            model=selected_model,
            sandbox=sandbox,
            effort=effort,
            cwd=cwd,
            timeout=timeout,
        )
        if "error" in result:
            return result
        return {
            "answer": result.get("result", ""),
            "model": result.get("model", selected_model),
            "provider": self.provider_name,
            "finished": True,
            "thread_id": result.get("thread_id"),
            "tool_count": result.get("tool_count", 0),
            "files_touched": result.get("files_touched", []),
            "effort": result.get("effort", ""),
        }

    async def see(
        self,
        image_path: str,
        *,
        question: str,
        model: str = "auto",
    ) -> dict:
        del model
        return {
            "error": "Vision is not available through Codex CLI in helix-agent.",
            "provider": self.provider_name,
            "image": image_path,
            "question": question,
        }

    async def models(self, action: str = "list", model_name: str = "") -> dict:
        if action == "use":
            if not model_name:
                return {"error": "model_name is required for 'use' action", "provider": self.provider_name}
            self.config.codex_model = model_name
            return {
                "provider": self.provider_name,
                "model_override": model_name,
                "message": f"Codex will now default to: {model_name}",
            }
        if action == "use_auto":
            return {
                "provider": self.provider_name,
                "model_override": self.config.codex_model,
                "message": "Codex always uses the configured default model.",
            }

        status = await self.status()
        if action == "status":
            return status.to_dict()
        return {
            "provider": self.provider_name,
            "models": [
                {
                    "name": self.config.codex_model,
                    "provider": self.provider_name,
                    "capabilities": ["code", "reasoning"],
                }
            ],
            "count": 1,
            "default_model": self.config.codex_model,
            "status": status.to_dict(),
        }
