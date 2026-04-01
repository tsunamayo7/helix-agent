"""Tests for HelixAgent multi-provider logic."""

from __future__ import annotations

import pytest

from src.agent import AgentConfig, HelixAgent


class TestAgentConfig:
    def test_default_config(self):
        config = AgentConfig()
        assert config.default_provider == "auto"
        assert config.default_mode == "quality"
        assert config.max_output_tokens == 4096
        assert config.codex_model == "gpt-5.4"
        assert config.openai_model == "gpt-4.1-mini"

    def test_custom_config(self):
        config = AgentConfig(
            default_provider="codex",
            default_mode="fast",
            codex_model="gpt-5.4-mini",
            max_output_tokens=2048,
        )
        assert config.default_provider == "codex"
        assert config.default_mode == "fast"
        assert config.codex_model == "gpt-5.4-mini"


class TestHelixAgent:
    def test_init_default(self):
        agent = HelixAgent()
        assert agent.config.ollama_host == "http://localhost:11434"
        assert agent.client.host == "http://localhost:11434"

    def test_init_custom_config(self):
        config = AgentConfig(default_provider="codex", default_mode="creative")
        agent = HelixAgent(config)
        assert agent.config.default_provider == "codex"
        assert agent.config.default_mode == "creative"

    @pytest.mark.asyncio
    async def test_providers_show(self):
        agent = HelixAgent()
        result = await agent.providers(action="show")
        assert result["default_provider"] == "auto"
        assert "codex" in result["supported"]

    @pytest.mark.asyncio
    async def test_providers_use(self):
        agent = HelixAgent()
        result = await agent.providers(action="use", provider="codex")
        assert result["updated"] == "default_provider"
        assert agent.config.default_provider == "codex"

    @pytest.mark.asyncio
    async def test_config_show(self):
        agent = HelixAgent()
        result = await agent.config_action(action="show")
        assert "default_provider" in result
        assert "codex_model" in result
        assert "openai_model" in result

    @pytest.mark.asyncio
    async def test_config_set(self):
        agent = HelixAgent()
        result = await agent.config_action(action="set", key="default_provider", value="codex")
        assert result["updated"] == "default_provider"
        assert agent.config.default_provider == "codex"

    @pytest.mark.asyncio
    async def test_config_set_unknown_key(self):
        agent = HelixAgent()
        result = await agent.config_action(action="set", key="nonexistent", value="x")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_config_set_bool(self):
        agent = HelixAgent()
        result = await agent.config_action(action="set", key="result_summary", value="false")
        assert result["updated"] == "result_summary"
        assert agent.config.result_summary is False

    @pytest.mark.asyncio
    async def test_think_explicit_codex(self, monkeypatch):
        agent = HelixAgent()

        async def fake_think(task, **kwargs):
            return {"result": f"codex:{task}", "provider": "codex", "model": "gpt-5.4"}

        monkeypatch.setattr(agent._providers["codex"], "think", fake_think)
        result = await agent.think(task="Review this diff", provider="codex")
        assert result["provider"] == "codex"
        assert result["result"] == "codex:Review this diff"

    @pytest.mark.asyncio
    async def test_think_auto_prefers_codex_for_code(self, monkeypatch):
        agent = HelixAgent()

        class Status:
            def __init__(self, available):
                self.available = available

        async def codex_status():
            return Status(True)

        async def ollama_status():
            return Status(False)

        async def openai_status():
            return Status(False)

        async def fake_codex_think(task, **kwargs):
            return {"result": "ok", "provider": "codex", "model": "gpt-5.4"}

        monkeypatch.setattr(agent._providers["codex"], "status", codex_status)
        monkeypatch.setattr(agent._providers["ollama"], "status", ollama_status)
        monkeypatch.setattr(agent._providers["openai-compatible"], "status", openai_status)
        monkeypatch.setattr(agent._providers["codex"], "think", fake_codex_think)

        result = await agent.think(task="Refactor this Python function")
        assert result["provider"] == "codex"

    @pytest.mark.asyncio
    async def test_models_explicit_provider(self, monkeypatch):
        agent = HelixAgent()

        async def fake_models(action="list", model_name=""):
            return {"provider": "openai-compatible", "models": [{"name": "gpt-4.1"}], "count": 1}

        monkeypatch.setattr(agent._providers["openai-compatible"], "models", fake_models)
        result = await agent.models(provider="openai-compatible")
        assert result["provider"] == "openai-compatible"
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_see_missing_image(self):
        agent = HelixAgent()
        result = await agent.see(image_path="/nonexistent/image.png", provider="ollama")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_background_agent_lifecycle(self, monkeypatch):
        agent = HelixAgent()

        async def fake_run_assignment(**kwargs):
            return {
                "result": f"done:{kwargs['task'][:20]}",
                "provider": "codex",
                "model": "gpt-5.4",
            }

        monkeypatch.setattr(agent, "run_assignment", fake_run_assignment)

        spawned = agent.spawn_background_agent(
            description="Investigate flaky tests",
            provider="codex",
            agent_type="explorer",
        )
        assert spawned["status"] == "idle"

        sent = agent.send_background_agent_input(spawned["agent_id"], "Look at failing pytest output")
        assert sent["status"] == "running"

        waited = await agent.wait_background_agent(spawned["agent_id"], timeout=2)
        assert waited["status"] == "completed"
        assert waited["last_success"] is True

        listed = agent.list_background_agents()
        assert listed["agents"]

        closed = agent.close_background_agent(spawned["agent_id"])
        assert closed["status"] == "closed"
