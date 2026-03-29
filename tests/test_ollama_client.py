"""Tests for Ollama API client."""

import pytest

from src.ollama_client import OllamaClient


class TestOllamaClientInit:
    def test_default_host(self):
        client = OllamaClient()
        assert client.host == "http://localhost:11434"

    def test_custom_host(self):
        client = OllamaClient(host="http://example.com:11434/")
        assert client.host == "http://example.com:11434"

    def test_trailing_slash_removed(self):
        client = OllamaClient(host="http://localhost:11434/")
        assert not client.host.endswith("/")

    def test_custom_timeout(self):
        client = OllamaClient(timeout=60.0)
        assert client.timeout == 60.0


class TestOllamaClientAvailability:
    @pytest.mark.asyncio
    async def test_is_available_when_running(self):
        client = OllamaClient()
        result = await client.is_available()
        # This will be True if Ollama is running locally
        assert isinstance(result, bool)


class TestOllamaClientListModels:
    @pytest.mark.asyncio
    async def test_list_models_returns_list(self):
        client = OllamaClient()
        if not await client.is_available():
            pytest.skip("Ollama not running")
        models = await client.list_models()
        assert isinstance(models, list)
        if models:
            assert "name" in models[0]
