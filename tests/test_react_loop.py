"""Tests for the ReAct agent loop."""

import json
from unittest.mock import AsyncMock

import pytest

from src.ollama_client import ChatResponse
from src.react_loop import AgentResult, AgentStep, ReactLoop
from src.tools import Tool, ToolRegistry, create_default_registry


def _mock_client_with_responses(responses: list[str]) -> AsyncMock:
    """Create a mock OllamaClient that returns ChatResponse objects."""
    client = AsyncMock()
    client.timeout = 60.0
    client._context_lengths = {}
    client.get_context_length = AsyncMock(return_value=8192)
    it = iter(responses)

    async def _chat_usage(**kwargs):
        return ChatResponse(content=next(it), input_tokens=10, output_tokens=5)

    client.chat_with_usage = AsyncMock(side_effect=_chat_usage)
    # Also set chat for backward compat (retry path)
    it2 = iter(responses)
    client.chat = AsyncMock(side_effect=lambda **kwargs: next(it2))
    return client


# --- ToolRegistry tests ---


class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        tool = Tool(
            name="test",
            description="A test tool",
            parameters={"input": "test input"},
            handler=AsyncMock(return_value="result"),
        )
        registry.register(tool)
        assert registry.get("test") is tool
        assert registry.get("nonexistent") is None

    def test_list_names(self):
        registry = create_default_registry()
        names = registry.list_names()
        assert "calculate" in names
        assert "search_memory" in names

    @pytest.mark.asyncio
    async def test_execute_success(self):
        registry = create_default_registry()
        result = await registry.execute("calculate", "2+3")
        assert result == "5"

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        registry = ToolRegistry()
        result = await registry.execute("nonexistent", "input")
        assert "Error: Unknown tool" in result

    @pytest.mark.asyncio
    async def test_execute_error_handling(self):
        registry = ToolRegistry()
        registry.register(Tool(
            name="failing",
            description="Always fails",
            parameters={},
            handler=AsyncMock(side_effect=RuntimeError("boom")),
        ))
        result = await registry.execute("failing", "input")
        assert "Error executing failing" in result

    def test_format_for_prompt(self):
        registry = create_default_registry()
        prompt = registry.format_for_prompt()
        assert "calculate" in prompt
        assert "search_memory" in prompt

    @pytest.mark.asyncio
    async def test_calculate_safe(self):
        registry = create_default_registry()
        result = await registry.execute("calculate", "10 * 5 + 2")
        assert result == "52"

    @pytest.mark.asyncio
    async def test_calculate_unsafe(self):
        registry = create_default_registry()
        result = await registry.execute("calculate", "__import__('os').system('rm -rf /')")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_truncate_long_result(self):
        registry = ToolRegistry()
        registry.register(Tool(
            name="long",
            description="Returns long text",
            parameters={},
            handler=AsyncMock(return_value="x" * 10000),
        ))
        result = await registry.execute("long", "")
        assert len(result) < 5000
        assert "truncated" in result


# --- ReactLoop tests ---


class TestReactLoop:
    @pytest.mark.asyncio
    async def test_simple_finish(self):
        """LLM immediately returns a finish action."""
        resp = json.dumps({
            "thought": "The answer is 42",
            "action": "finish",
            "action_input": "42",
        })
        client = _mock_client_with_responses([resp])

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5)

        result = await loop.run("What is the meaning of life?", model="test:7b")

        assert result.answer == "42"
        assert result.finished is True
        assert len(result.steps) == 1
        assert result.model == "test:7b"

    @pytest.mark.asyncio
    async def test_tool_use_then_finish(self):
        """LLM uses a tool, then finishes."""
        responses = [
            json.dumps({
                "thought": "I need to calculate 10 * 5",
                "action": "calculate",
                "action_input": "10 * 5",
            }),
            json.dumps({
                "thought": "The result is 50",
                "action": "finish",
                "action_input": "10 * 5 = 50",
            }),
        ]
        client = _mock_client_with_responses(responses)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5)

        result = await loop.run("Calculate 10 * 5", model="test:7b")

        assert result.finished is True
        assert "50" in result.answer
        assert len(result.steps) == 2
        assert result.steps[0].action == "calculate"
        assert result.steps[0].observation == "50"

    @pytest.mark.asyncio
    async def test_max_steps_reached(self):
        """Loop reaches max_steps without finishing."""
        resp = json.dumps({
            "thought": "Keep going",
            "action": "calculate",
            "action_input": "1+1",
        })
        # Need enough responses for 3 steps
        client = _mock_client_with_responses([resp] * 3)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=3)

        result = await loop.run("Loop forever", model="test:7b")

        assert result.finished is False
        assert len(result.steps) == 3

    @pytest.mark.asyncio
    async def test_unknown_tool_recovery(self):
        """LLM calls unknown tool, gets error, then finishes."""
        responses = [
            json.dumps({
                "thought": "Let me try this tool",
                "action": "nonexistent_tool",
                "action_input": "test",
            }),
            json.dumps({
                "thought": "That tool doesn't exist, let me finish",
                "action": "finish",
                "action_input": "I cannot use that tool",
            }),
        ]
        client = _mock_client_with_responses(responses)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5)

        result = await loop.run("Do something", model="test:7b")

        assert result.finished is True
        assert "Unknown tool" in result.steps[0].observation

    @pytest.mark.asyncio
    async def test_json_parse_retry(self):
        """LLM returns invalid JSON, then valid JSON on retry."""
        responses = [
            "This is not JSON at all",  # Bad response
            json.dumps({  # Retry response
                "thought": "OK",
                "action": "finish",
                "action_input": "done",
            }),
        ]
        client = _mock_client_with_responses(responses)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5, max_retries=2)

        result = await loop.run("Test retry", model="test:7b")

        assert result.answer == "done"
        assert result.finished is True

    @pytest.mark.asyncio
    async def test_json_in_markdown_block(self):
        """LLM wraps JSON in markdown code block."""
        resp = '```json\n{"thought": "thinking", "action": "finish", "action_input": "answer"}\n```'
        client = _mock_client_with_responses([resp])

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5)

        result = await loop.run("Test markdown", model="test:7b")

        assert result.answer == "answer"
        assert result.finished is True

    @pytest.mark.asyncio
    async def test_with_context(self):
        """Task with additional context."""
        resp = json.dumps({
            "thought": "I see the context",
            "action": "finish",
            "action_input": "analyzed",
        })
        client = _mock_client_with_responses([resp])

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5)

        result = await loop.run(
            "Analyze this",
            model="test:7b",
            context="some code here",
        )

        assert result.finished is True
        # Verify context was included in the messages
        call_args = client.chat_with_usage.call_args
        messages = call_args.kwargs.get("messages", [])
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert "some code here" in user_msg["content"]


# --- AgentResult tests ---


class TestAgentResult:
    def test_to_dict(self):
        result = AgentResult(
            answer="42",
            model="test:7b",
            steps=[
                AgentStep(step=1, thought="thinking", action="calculate", action_input="6*7", observation="42"),
                AgentStep(step=2, thought="done", action="finish", action_input="42"),
            ],
            finished=True,
        )
        d = result.to_dict()
        assert d["answer"] == "42"
        assert d["model"] == "test:7b"
        assert d["steps"] == 2
        assert d["finished"] is True
        assert len(d["trace"]) == 2

    def test_to_dict_truncation(self):
        """Long thoughts/observations are truncated in trace."""
        result = AgentResult(
            answer="ok",
            model="test:7b",
            steps=[
                AgentStep(step=1, thought="x" * 500, action="test", action_input="", observation="y" * 500),
            ],
        )
        d = result.to_dict()
        assert len(d["trace"][0]["thought"]) == 200
        assert len(d["trace"][0]["observation"]) == 200
