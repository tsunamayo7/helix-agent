"""HelixAgent: multi-provider task delegation and background agents."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from .provider_runtime import (
    CodexProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    SUPPORTED_PROVIDERS,
)
from .router import Capability, _infer_capability


@dataclass
class AgentConfig:
    default_provider: str = "auto"  # auto | ollama | codex | openai-compatible
    default_mode: str = "quality"  # quality | fast | creative
    max_output_tokens: int = 4096
    result_summary: bool = True

    ollama_host: str = "http://localhost:11434"
    ollama_timeout: float = 120.0

    codex_model: str = "gpt-5.4"
    codex_sandbox: str = "workspace-write"
    codex_timeout: int = 180

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_api_key_env: str = "OPENAI_API_KEY"
    openai_model: str = "gpt-4.1-mini"
    openai_timeout: float = 120.0


AGENT_ROLE_PROMPTS = {
    "default": (
        "You are a Claude Code-style sub-agent. "
        "Complete the assigned software task pragmatically and return concise high-signal results."
    ),
    "explorer": (
        "You are a read-heavy explorer agent. Focus on investigation, concrete findings, "
        "and precise references. Avoid edits unless explicitly asked."
    ),
    "worker": (
        "You are an implementation-focused worker agent. Make targeted changes, "
        "run relevant checks, and report what changed plus residual risk."
    ),
}


def _normalize_provider(provider: str) -> str:
    if provider in SUPPORTED_PROVIDERS:
        return provider
    return "auto"


def _normalize_agent_type(agent_type: str) -> str:
    return agent_type if agent_type in AGENT_ROLE_PROMPTS else "default"


def _default_sandbox(agent_type: str) -> str:
    return "read-only" if agent_type == "explorer" else "workspace-write"


def _summarize_result(result: dict) -> str:
    if "error" in result:
        return str(result["error"])[:1200]
    text = (
        result.get("result")
        or result.get("answer")
        or result.get("message")
        or ""
    )
    if isinstance(text, dict):
        text = str(text)
    return str(text).strip()[:1200]


@dataclass
class BackgroundAgentTurn:
    prompt: str
    success: bool
    summary: str
    finished_at: float
    provider: str
    model: str


@dataclass
class BackgroundAgentRecord:
    agent_id: str
    description: str
    agent_type: str
    provider: str
    model: str
    mode: str
    sandbox: str
    cwd: str | None
    created_at: float
    updated_at: float
    status: str = "idle"
    last_prompt: str = ""
    last_summary: str = ""
    last_result: dict = field(default_factory=dict)
    last_success: bool | None = None
    history: list[BackgroundAgentTurn] = field(default_factory=list)
    current_task: asyncio.Task | None = field(default=None, repr=False)
    closed: bool = False


class BackgroundAgentManager:
    def __init__(self, owner: "HelixAgent", max_agents: int = 16):
        self.owner = owner
        self.max_agents = max_agents
        self._agents: dict[str, BackgroundAgentRecord] = {}
        self._order: list[str] = []

    def create(
        self,
        *,
        description: str,
        provider: str,
        model: str,
        mode: str,
        agent_type: str,
        sandbox: str,
        cwd: str | None,
    ) -> BackgroundAgentRecord:
        normalized_type = _normalize_agent_type(agent_type)
        now = time.time()
        record = BackgroundAgentRecord(
            agent_id=f"helix-{uuid.uuid4().hex[:8]}",
            description=description.strip(),
            agent_type=normalized_type,
            provider=_normalize_provider(provider),
            model=model,
            mode=mode,
            sandbox=sandbox or _default_sandbox(normalized_type),
            cwd=cwd,
            created_at=now,
            updated_at=now,
        )
        self._agents[record.agent_id] = record
        self._order.append(record.agent_id)
        self._trim()
        return record

    def _trim(self) -> None:
        if len(self._order) <= self.max_agents:
            return
        removable: list[str] = []
        for agent_id in self._order:
            record = self._agents.get(agent_id)
            if not record:
                removable.append(agent_id)
                continue
            if record.current_task is None and (record.closed or record.status in {"completed", "failed"}):
                removable.append(agent_id)
            if len(self._order) - len(removable) <= self.max_agents:
                break
        for agent_id in removable:
            self._agents.pop(agent_id, None)
            if agent_id in self._order:
                self._order.remove(agent_id)

    def get(self, agent_id: str) -> BackgroundAgentRecord | None:
        return self._agents.get(agent_id)

    def list_all(self) -> list[BackgroundAgentRecord]:
        return [self._agents[agent_id] for agent_id in reversed(self._order) if agent_id in self._agents]

    def snapshot(self, record: BackgroundAgentRecord) -> dict:
        return {
            "agent_id": record.agent_id,
            "description": record.description,
            "agent_type": record.agent_type,
            "provider": record.provider,
            "model": record.model,
            "mode": record.mode,
            "sandbox": record.sandbox,
            "cwd": record.cwd,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "last_prompt": record.last_prompt,
            "last_summary": record.last_summary,
            "last_success": record.last_success,
            "turns": len(record.history),
            "closed": record.closed,
        }

    def _build_prompt(self, record: BackgroundAgentRecord, prompt: str) -> str:
        sections = [AGENT_ROLE_PROMPTS[record.agent_type]]
        if record.description:
            sections.append(f"Agent description:\n{record.description}")
        if record.history:
            history_lines = []
            for turn in record.history[-3:]:
                history_lines.append(f"- Previous instruction: {turn.prompt[:300]}")
                history_lines.append(f"  Result summary: {turn.summary[:500]}")
            sections.append("Prior agent context:\n" + "\n".join(history_lines))
        sections.append(f"Current assignment:\n{prompt.strip()}")
        return "\n\n".join(sections)

    async def _run_turn(self, record: BackgroundAgentRecord, prompt: str, timeout: int) -> None:
        record.status = "running"
        record.updated_at = time.time()
        record.last_prompt = prompt
        try:
            result = await self.owner.run_assignment(
                task=self._build_prompt(record, prompt),
                provider=record.provider,
                model=record.model,
                mode=record.mode,
                cwd=record.cwd,
                sandbox=record.sandbox,
                timeout=timeout,
            )
            success = "error" not in result
            summary = _summarize_result(result)
            provider = result.get("provider", record.provider)
            model = result.get("model", record.model)
            record.last_result = result
            record.last_summary = summary
            record.last_success = success
            record.history.append(
                BackgroundAgentTurn(
                    prompt=prompt,
                    success=success,
                    summary=summary,
                    finished_at=time.time(),
                    provider=str(provider),
                    model=str(model),
                )
            )
            record.status = "completed" if success else "failed"
            record.provider = str(provider)
            record.model = str(model)
        except asyncio.CancelledError:
            record.status = "closed"
            record.last_success = False
            record.last_result = {"error": "Agent run was cancelled before completion."}
            record.last_summary = "Agent run was cancelled before completion."
            raise
        finally:
            record.updated_at = time.time()
            record.current_task = None

    def start(self, record: BackgroundAgentRecord, prompt: str, timeout: int) -> dict:
        if record.closed:
            raise ValueError("Agent is already closed.")
        if record.current_task is not None:
            raise ValueError("Agent is already running.")
        record.status = "running"
        record.updated_at = time.time()
        record.last_prompt = prompt
        record.current_task = asyncio.create_task(self._run_turn(record, prompt, timeout))
        return self.snapshot(record)

    async def wait(self, record: BackgroundAgentRecord, timeout: int) -> dict:
        if record.current_task is None:
            return self.snapshot(record)
        try:
            await asyncio.wait_for(asyncio.shield(record.current_task), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self.snapshot(record)

    def close(self, record: BackgroundAgentRecord) -> dict:
        if record.current_task is not None:
            raise ValueError("Agent is still running. Wait for completion before closing it.")
        record.closed = True
        record.status = "closed"
        record.updated_at = time.time()
        return self.snapshot(record)


class HelixAgent:
    """Orchestrates task delegation across multiple providers."""

    def __init__(self, config: AgentConfig | None = None):
        self.config = config or AgentConfig()
        self._providers = {
            "ollama": OllamaProvider(self.config),
            "codex": CodexProvider(self.config),
            "openai-compatible": OpenAICompatibleProvider(self.config),
        }
        # Backward-compatibility for older tests and callers.
        self.client = self._providers["ollama"].client
        self.router = self._providers["ollama"].router
        self.background_agents = BackgroundAgentManager(self)

    def _get_provider(self, provider: str):
        return self._providers[_normalize_provider(provider)] if provider in self._providers else None

    async def _resolve_provider(self, requested_provider: str, task: str = "", vision: bool = False) -> str:
        provider = _normalize_provider(requested_provider)
        if provider != "auto":
            return provider

        default_provider = _normalize_provider(self.config.default_provider)
        if default_provider != "auto":
            return default_provider

        if vision:
            return "ollama"

        capability = _infer_capability(task) if task else Capability.REASONING

        if capability == Capability.CODE and (await self._providers["codex"].status()).available:
            return "codex"

        if (await self._providers["ollama"].status()).available:
            return "ollama"

        if (await self._providers["openai-compatible"].status()).available:
            return "openai-compatible"

        if (await self._providers["codex"].status()).available:
            return "codex"

        return "ollama"

    async def providers(self, action: str = "list", provider: str = "") -> dict:
        if action == "use":
            if provider not in SUPPORTED_PROVIDERS and provider != "auto":
                return {
                    "error": f"Unknown provider: {provider}",
                    "supported": list(SUPPORTED_PROVIDERS) + ["auto"],
                }
            old = self.config.default_provider
            self.config.default_provider = provider or "auto"
            return {"updated": "default_provider", "old": old, "new": self.config.default_provider}

        if action == "show":
            return {"default_provider": self.config.default_provider, "supported": list(SUPPORTED_PROVIDERS)}

        statuses = []
        for name, runtime in self._providers.items():
            statuses.append((await runtime.status()).to_dict())
        return {"default_provider": self.config.default_provider, "providers": statuses}

    async def think(
        self,
        task: str,
        context: str = "",
        model: str = "auto",
        mode: str = "",
        provider: str = "auto",
        cwd: str | None = None,
        sandbox: str = "",
        timeout: int | None = None,
    ) -> dict:
        mode = mode or self.config.default_mode
        resolved = await self._resolve_provider(provider, task=task)
        runtime = self._providers[resolved]

        if resolved == "codex":
            return await runtime.think(
                task,
                context=context,
                model=model,
                mode=mode,
                cwd=cwd,
                sandbox=sandbox,
                timeout=timeout or self.config.codex_timeout,
            )

        return await runtime.think(task, context=context, model=model, mode=mode)

    async def agent(
        self,
        task: str,
        context: str = "",
        model: str = "auto",
        mode: str = "",
        provider: str = "auto",
        max_steps: int = 10,
        tools: list[str] | None = None,
        _on_progress=None,
        cwd: str | None = None,
        sandbox: str = "",
        timeout: int | None = None,
    ) -> dict:
        mode = mode or self.config.default_mode
        resolved = await self._resolve_provider(provider, task=task)
        runtime = self._providers[resolved]

        if resolved == "codex":
            return await runtime.agent(
                task,
                context=context,
                model=model,
                mode=mode,
                max_steps=max_steps,
                tools=tools,
                on_progress=_on_progress,
                cwd=cwd,
                sandbox=sandbox,
                timeout=timeout or self.config.codex_timeout,
            )

        return await runtime.agent(
            task,
            context=context,
            model=model,
            mode=mode,
            max_steps=max_steps,
            tools=tools,
            on_progress=_on_progress,
        )

    async def see(
        self,
        image_path: str,
        question: str = "Describe what you see in this image in detail.",
        model: str = "auto",
        provider: str = "auto",
    ) -> dict:
        resolved = await self._resolve_provider(provider, vision=True)
        runtime = self._providers[resolved]
        return await runtime.see(image_path, question=question, model=model)

    async def models(self, action: str = "list", model_name: str = "", provider: str = "auto") -> dict:
        resolved = await self._resolve_provider(provider)
        runtime = self._providers[resolved]
        return await runtime.models(action=action, model_name=model_name)

    async def run_assignment(
        self,
        *,
        task: str,
        provider: str = "auto",
        model: str = "auto",
        mode: str = "",
        cwd: str | None = None,
        sandbox: str = "",
        timeout: int | None = None,
    ) -> dict:
        return await self.think(
            task=task,
            model=model,
            mode=mode,
            provider=provider,
            cwd=cwd,
            sandbox=sandbox,
            timeout=timeout,
        )

    def spawn_background_agent(
        self,
        *,
        description: str,
        provider: str = "auto",
        model: str = "auto",
        mode: str = "",
        agent_type: str = "default",
        sandbox: str = "",
        cwd: str | None = None,
        initial_task: str = "",
        timeout: int | None = None,
    ) -> dict:
        record = self.background_agents.create(
            description=description,
            provider=provider,
            model=model,
            mode=mode or self.config.default_mode,
            agent_type=agent_type,
            sandbox=sandbox,
            cwd=cwd,
        )
        snapshot = self.background_agents.snapshot(record)
        if initial_task:
            self.background_agents.start(record, initial_task, timeout or self.config.codex_timeout)
            snapshot = self.background_agents.snapshot(record)
        return snapshot

    def send_background_agent_input(self, agent_id: str, message: str, timeout: int | None = None) -> dict:
        record = self.background_agents.get(agent_id)
        if not record:
            raise ValueError(f"Unknown agent_id: {agent_id}")
        return self.background_agents.start(record, message, timeout or self.config.codex_timeout)

    async def wait_background_agent(self, agent_id: str, timeout: int = 30) -> dict:
        record = self.background_agents.get(agent_id)
        if not record:
            raise ValueError(f"Unknown agent_id: {agent_id}")
        return await self.background_agents.wait(record, timeout)

    def list_background_agents(self) -> dict:
        return {"agents": [self.background_agents.snapshot(record) for record in self.background_agents.list_all()]}

    def close_background_agent(self, agent_id: str) -> dict:
        record = self.background_agents.get(agent_id)
        if not record:
            raise ValueError(f"Unknown agent_id: {agent_id}")
        return self.background_agents.close(record)

    async def config_action(self, action: str = "show", key: str = "", value: str = "") -> dict:
        if action == "show":
            return {
                "default_provider": self.config.default_provider,
                "default_mode": self.config.default_mode,
                "max_output_tokens": self.config.max_output_tokens,
                "result_summary": self.config.result_summary,
                "ollama_host": self.config.ollama_host,
                "ollama_timeout": self.config.ollama_timeout,
                "codex_model": self.config.codex_model,
                "codex_sandbox": self.config.codex_sandbox,
                "codex_timeout": self.config.codex_timeout,
                "openai_base_url": self.config.openai_base_url,
                "openai_api_key_env": self.config.openai_api_key_env,
                "openai_model": self.config.openai_model,
                "openai_timeout": self.config.openai_timeout,
            }

        if action != "set":
            return {"error": f"Unknown action: {action}"}
        if not key:
            return {"error": "key is required for 'set' action"}
        if not hasattr(self.config, key):
            return {"error": f"Unknown config key: {key}"}

        old = getattr(self.config, key)
        if isinstance(old, bool):
            new_value = value.lower() in ("true", "1", "yes")
        elif isinstance(old, int):
            new_value = int(value)
        elif isinstance(old, float):
            new_value = float(value)
        else:
            new_value = value
        setattr(self.config, key, new_value)

        if key in {"ollama_host", "ollama_timeout"}:
            self._providers["ollama"] = OllamaProvider(self.config)
            self.client = self._providers["ollama"].client
            self.router = self._providers["ollama"].router
        elif key in {"openai_base_url", "openai_api_key", "openai_api_key_env", "openai_model", "openai_timeout"}:
            self._providers["openai-compatible"] = OpenAICompatibleProvider(self.config)
        elif key in {"codex_model", "codex_sandbox", "codex_timeout"}:
            self._providers["codex"] = CodexProvider(self.config)

        return {"updated": key, "old": str(old), "new": str(new_value)}
