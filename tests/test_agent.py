"""Tests for HelixAgent core logic."""

import pytest

from src.agent import AgentConfig, HelixAgent


class TestAgentConfig:
    def test_default_config(self):
        config = AgentConfig()
        assert config.ollama_host == "http://localhost:11434"
        assert config.default_mode == "quality"
        assert config.max_output_tokens == 4096
        assert config.result_summary is True

    def test_custom_config(self):
        config = AgentConfig(
            ollama_host="http://remote:11434",
            default_mode="fast",
            max_output_tokens=2048,
        )
        assert config.ollama_host == "http://remote:11434"
        assert config.default_mode == "fast"


class TestHelixAgent:
    def test_init_default(self):
        agent = HelixAgent()
        assert agent.config.ollama_host == "http://localhost:11434"

    def test_init_custom_config(self):
        config = AgentConfig(default_mode="creative")
        agent = HelixAgent(config)
        assert agent.config.default_mode == "creative"

    @pytest.mark.asyncio
    async def test_models_status(self):
        agent = HelixAgent()
        if not await agent.client.is_available():
            pytest.skip("Ollama not running")
        result = await agent.models(action="status")
        assert result["status"] == "connected"

    @pytest.mark.asyncio
    async def test_models_list(self):
        agent = HelixAgent()
        if not await agent.client.is_available():
            pytest.skip("Ollama not running")
        result = await agent.models(action="list")
        assert "models" in result
        assert "count" in result

    @pytest.mark.asyncio
    async def test_models_capabilities(self):
        agent = HelixAgent()
        if not await agent.client.is_available():
            pytest.skip("Ollama not running")
        result = await agent.models(action="capabilities")
        assert "capabilities" in result

    @pytest.mark.asyncio
    async def test_config_show(self):
        agent = HelixAgent()
        result = await agent.config_action(action="show")
        assert "default_mode" in result
        assert "ollama_host" in result

    @pytest.mark.asyncio
    async def test_config_set(self):
        agent = HelixAgent()
        result = await agent.config_action(action="set", key="default_mode", value="fast")
        assert result["updated"] == "default_mode"
        assert result["new"] == "fast"
        assert agent.config.default_mode == "fast"

    @pytest.mark.asyncio
    async def test_config_set_unknown_key(self):
        agent = HelixAgent()
        result = await agent.config_action(action="set", key="nonexistent", value="x")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_config_set_bool(self):
        agent = HelixAgent()
        result = await agent.config_action(action="set", key="result_summary", value="false")
        assert agent.config.result_summary is False

    @pytest.mark.asyncio
    async def test_think_no_ollama(self):
        agent = HelixAgent(AgentConfig(ollama_host="http://localhost:99999"))
        result = await agent.think(task="test", model="auto")
        # Should return error since no Ollama at that port
        assert "error" in result

    @pytest.mark.asyncio
    async def test_see_missing_image(self):
        agent = HelixAgent()
        result = await agent.see(image_path="/nonexistent/image.png")
        assert "error" in result

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_think_with_ollama(self):
        agent = HelixAgent()
        if not await agent.client.is_available():
            pytest.skip("Ollama not running")
        models = await agent.client.list_models()
        if not models:
            pytest.skip("No models installed")
        result = await agent.think(task="What is 2+2?", mode="fast")
        assert "result" in result
        assert "model" in result

    def test_build_system_prompt_quality(self):
        agent = HelixAgent()
        prompt = agent._build_system_prompt("quality")
        assert "accuracy" in prompt.lower()

    def test_build_system_prompt_fast(self):
        agent = HelixAgent()
        prompt = agent._build_system_prompt("fast")
        assert "brief" in prompt.lower()

    def test_build_system_prompt_creative(self):
        agent = HelixAgent()
        prompt = agent._build_system_prompt("creative")
        assert "creative" in prompt.lower()
