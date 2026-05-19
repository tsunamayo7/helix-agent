"""Tests for Phase 1 features: JSON Schema tools, streaming, native tool calling."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ollama_client import OllamaClient
from src.qdrant_memory import QdrantMemory, QdrantMemoryConfig
from src.react_loop import ReactLoop
from src.tools import Tool, ToolRegistry


# --- JSON Schema tool tests ---


class TestToolJsonSchema:
    def test_to_ollama_tool_auto_generated(self):
        tool = Tool(
            name="calculate",
            description="Evaluate math",
            parameters={"expression": "math expression"},
            handler=AsyncMock(),
        )
        schema = tool.to_ollama_tool()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "calculate"
        props = schema["function"]["parameters"]["properties"]
        assert "expression" in props
        assert props["expression"]["type"] == "string"

    def test_to_ollama_tool_custom_schema(self):
        custom = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search query"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        }
        tool = Tool(
            name="search",
            description="Search memory",
            parameters={"query": "search query"},
            handler=AsyncMock(),
            json_schema=custom,
        )
        schema = tool.to_ollama_tool()
        assert schema["function"]["parameters"] == custom

    def test_registry_to_ollama_tools(self):
        registry = ToolRegistry()
        registry.register(Tool(
            name="a",
            description="Tool A",
            parameters={"x": "input"},
            handler=AsyncMock(),
        ))
        registry.register(Tool(
            name="b",
            description="Tool B",
            parameters={"y": "input"},
            handler=AsyncMock(),
        ))
        tools = registry.to_ollama_tools()
        assert len(tools) == 2
        names = {t["function"]["name"] for t in tools}
        assert names == {"a", "b"}


# --- Streaming tests ---


class TestChatStream:
    @pytest.mark.asyncio
    async def test_chat_stream_collects_chunks(self):
        client = OllamaClient()

        class FakeStreamResponse:
            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                yield json.dumps({"message": {"content": "Hello"}, "done": False})
                yield json.dumps({"message": {"content": " world"}, "done": True})

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            def stream(self, *a, **kw):
                return FakeStreamResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("httpx.AsyncClient", FakeAsyncClient):
            chunks = []
            async for chunk in client.chat_stream("test", [{"role": "user", "content": "hi"}]):
                chunks.append(chunk)
            assert chunks == ["Hello", " world"]


# --- Native tool calling loop tests ---


class TestNativeToolLoop:
    @pytest.mark.asyncio
    async def test_native_tools_finish_without_tool_call(self):
        client = AsyncMock()
        client.timeout = 60.0
        client.get_context_length = AsyncMock(return_value=8192)
        client.chat_with_tools = AsyncMock(return_value={
            "content": "The answer is 42",
            "tool_calls": [],
        })

        registry = ToolRegistry()
        registry.register(Tool(
            name="calculate",
            description="Math",
            parameters={"expression": "expr"},
            handler=AsyncMock(return_value="42"),
        ))

        loop = ReactLoop(client=client, tools=registry, max_steps=5, use_native_tools=True)
        result = await loop.run("What is 6*7?", model="test:7b")

        assert result.finished is True
        assert result.answer == "The answer is 42"

    @pytest.mark.asyncio
    async def test_native_tools_with_tool_call_then_finish(self):
        client = AsyncMock()
        client.timeout = 60.0
        client.get_context_length = AsyncMock(return_value=8192)

        responses = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "calculate",
                            "arguments": {"expression": "6*7"},
                        }
                    }
                ],
            },
            {
                "content": "The result is 42",
                "tool_calls": [],
            },
        ]
        client.chat_with_tools = AsyncMock(side_effect=responses)

        registry = ToolRegistry()
        registry.register(Tool(
            name="calculate",
            description="Math",
            parameters={"expression": "expr"},
            handler=AsyncMock(return_value="42"),
        ))

        loop = ReactLoop(client=client, tools=registry, max_steps=5, use_native_tools=True)
        result = await loop.run("Calculate 6*7", model="test:7b")

        assert result.finished is True
        assert "42" in result.answer
        assert any(s.action == "calculate" for s in result.steps)

    @pytest.mark.asyncio
    async def test_native_tools_max_steps(self):
        client = AsyncMock()
        client.timeout = 60.0
        client.get_context_length = AsyncMock(return_value=8192)
        client.chat_with_tools = AsyncMock(return_value={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "calculate", "arguments": {"expression": "1+1"}}}
            ],
        })

        registry = ToolRegistry()
        registry.register(Tool(
            name="calculate",
            description="Math",
            parameters={"expression": "expr"},
            handler=AsyncMock(return_value="2"),
        ))

        loop = ReactLoop(client=client, tools=registry, max_steps=3, use_native_tools=True)
        result = await loop.run("Loop", model="test:7b")

        assert result.finished is False
        assert len(result.steps) == 3


# --- Streaming ReactLoop tests ---


class TestStreamingReactLoop:
    @pytest.mark.asyncio
    async def test_stream_mode_collects_response(self):
        client = AsyncMock()
        client.timeout = 60.0
        client.get_context_length = AsyncMock(return_value=8192)

        async def fake_stream(*args, **kwargs):
            for chunk in ['{"thought": "ok",', ' "action": "finish",', ' "action_input": "done"}']:
                yield chunk

        client.chat_stream = MagicMock(return_value=fake_stream())

        registry = ToolRegistry()
        registry.register(Tool(
            name="calculate",
            description="Math",
            parameters={"expression": "expr"},
            handler=AsyncMock(return_value="42"),
        ))

        loop = ReactLoop(client=client, tools=registry, max_steps=5, stream=True)
        result = await loop.run("Test", model="test:7b")

        assert result.finished is True
        assert result.answer == "done"


# --- QdrantMemory unit tests ---


class TestQdrantMemory:
    def test_config_defaults(self):
        config = QdrantMemoryConfig()
        assert config.qdrant_url == "http://localhost:6333"
        assert config.collection == "mem0_shared"
        assert config.embedding_model == "qwen3-embedding:8b"
        assert config.embedding_dim == 4096
        assert config.user_id == "default"

    @pytest.mark.asyncio
    async def test_search_formats_results(self):
        memory = QdrantMemory()
        mock_vector = [0.1] * 4096

        with patch.object(memory, "_embed", return_value=mock_vector):
            with patch.object(memory, "_qdrant_post", return_value={
                "result": [
                    {"payload": {"data": "test memory", "source": "test"}, "score": 0.9},
                    {"payload": {"data": "another one", "source": ""}, "score": 0.7},
                ]
            }):
                hits = await memory.search("query")
                assert len(hits) == 2
                assert hits[0]["text"] == "test memory"
                assert hits[0]["score"] == 0.9

    @pytest.mark.asyncio
    async def test_add_returns_point_id(self):
        memory = QdrantMemory()
        mock_vector = [0.1] * 4096

        with patch.object(memory, "_embed", return_value=mock_vector):
            with patch.object(memory, "_qdrant_post", return_value={"status": "ok"}):
                point_id = await memory.add("test memory")
                assert isinstance(point_id, str)
                assert len(point_id) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_add_with_metadata(self):
        memory = QdrantMemory()
        mock_vector = [0.1] * 4096

        with patch.object(memory, "_embed", return_value=mock_vector):
            with patch.object(memory, "_qdrant_post", return_value={"status": "ok"}) as mock_post:
                await memory.add("test", metadata={"category": "design"})
                call_args = mock_post.call_args
                payload = call_args[1]["payload"] if "payload" in call_args[1] else call_args[0][1]
                point = payload["points"][0]["payload"]
                assert point["category"] == "design"
                assert point["user_id"] == "default"
