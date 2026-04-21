"""Tests for Phase 4: prompt optimization, tracing, error recovery, context compression."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ollama_client import ChatResponse, OllamaClient
from src.react_loop import (
    AgentResult,
    AgentStep,
    ReactLoop,
    REACT_SYSTEM_PROMPT,
    DEFAULT_STEP_TIMEOUT,
    DEFAULT_TOTAL_TIMEOUT,
    OOM_PATTERNS,
    SMALLER_MODEL_FALLBACKS,
)
from src.tools import ToolRegistry, Tool, create_default_registry
from src.tracing import TraceRecorder


# ── TraceRecorder tests ──


class TestTraceRecorder:
    def test_empty_summary(self):
        tr = TraceRecorder(task_id="test")
        s = tr.summary()
        assert s.total_steps == 0
        assert s.total_tokens_in == 0
        assert s.total_tokens_out == 0

    def test_record_llm_call(self):
        tr = TraceRecorder(task_id="test")
        tr.record_llm_call(
            step=1, model="test:7b",
            input_tokens=100, output_tokens=50,
            duration_ms=500.0, thought="thinking", action="calculate",
        )
        s = tr.summary()
        assert s.total_steps == 1
        assert s.total_tokens_in == 100
        assert s.total_tokens_out == 50

    def test_record_tool_result(self):
        tr = TraceRecorder(task_id="test")
        tr.record_tool_result(
            step=1, tool="calculate", duration_ms=10.0,
            success=True, result_length=5,
        )
        s = tr.summary()
        assert "calculate" in s.tool_stats
        assert s.tool_stats["calculate"]["calls"] == 1
        assert s.tool_stats["calculate"]["successes"] == 1

    def test_tool_failure_tracking(self):
        tr = TraceRecorder(task_id="test")
        tr.record_tool_result(step=1, tool="run_cmd", duration_ms=5, success=False)
        tr.record_tool_result(step=2, tool="run_cmd", duration_ms=3, success=True)
        s = tr.summary()
        assert s.tool_stats["run_cmd"]["failures"] == 1
        assert s.tool_stats["run_cmd"]["successes"] == 1
        assert s.tool_stats["run_cmd"]["calls"] == 2

    def test_multi_step_summary(self):
        tr = TraceRecorder(task_id="test")
        tr.record_llm_call(step=1, model="m", input_tokens=10, output_tokens=5, duration_ms=100)
        tr.record_tool_result(step=1, tool="a", duration_ms=50, success=True)
        tr.record_llm_call(step=2, model="m", input_tokens=20, output_tokens=10, duration_ms=200)
        tr.record_tool_result(step=2, tool="b", duration_ms=30, success=True)
        s = tr.summary()
        assert s.total_steps == 2
        assert s.total_tokens_in == 30
        assert s.total_tokens_out == 15

    def test_save_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tr = TraceRecorder(task_id="test_save", trace_dir=Path(tmpdir))
            tr.record_llm_call(step=1, model="m", input_tokens=1, output_tokens=1, duration_ms=1)
            path = tr.save()
            assert path is not None
            assert path.exists()
            assert path.suffix == ".jsonl"
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["type"] == "llm_call"
            assert data["step"] == 1

    def test_save_empty_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tr = TraceRecorder(task_id="empty", trace_dir=Path(tmpdir))
            assert tr.save() is None

    def test_save_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir) / "nested" / "traces"
            tr = TraceRecorder(task_id="nest", trace_dir=subdir)
            tr.record_llm_call(step=1, model="m", input_tokens=0, output_tokens=0, duration_ms=0)
            path = tr.save()
            assert path is not None
            assert subdir.exists()

    def test_summary_to_dict(self):
        tr = TraceRecorder(task_id="test")
        tr.record_llm_call(step=1, model="m", input_tokens=5, output_tokens=3, duration_ms=100)
        d = tr.summary().to_dict()
        assert "total_steps" in d
        assert "total_tokens_in" in d
        assert "tool_stats" in d

    def test_thought_truncation(self):
        tr = TraceRecorder(task_id="test")
        long_thought = "x" * 1000
        tr.record_llm_call(step=1, model="m", input_tokens=0, output_tokens=0,
                           duration_ms=0, thought=long_thought)
        assert len(tr._entries[0].data["thought"]) == 500


# ── ChatResponse tests ──


class TestChatResponse:
    def test_basic(self):
        resp = ChatResponse(content="hello", input_tokens=10, output_tokens=5)
        assert resp.content == "hello"
        assert resp.input_tokens == 10

    def test_defaults(self):
        resp = ChatResponse(content="hi")
        assert resp.input_tokens == 0
        assert resp.output_tokens == 0


# ── OllamaClient enhancements ──


class TestOllamaClientEnhancements:
    @pytest.mark.asyncio
    async def test_chat_with_usage(self):
        client = OllamaClient()
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"content": "test"},
                "prompt_eval_count": 42,
                "eval_count": 10,
            }
            mock_response.raise_for_status = MagicMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_http

            resp = await client.chat_with_usage(
                model="test:7b", messages=[{"role": "user", "content": "hi"}]
            )
            assert resp.content == "test"
            assert resp.input_tokens == 42
            assert resp.output_tokens == 10

    @pytest.mark.asyncio
    async def test_get_context_length_cached(self):
        client = OllamaClient()
        client._context_lengths["cached:7b"] = 4096
        result = await client.get_context_length("cached:7b")
        assert result == 4096

    @pytest.mark.asyncio
    async def test_get_context_length_default(self):
        client = OllamaClient()
        with patch.object(client, "show_model", side_effect=Exception("no")):
            result = await client.get_context_length("missing:7b")
            assert result == 8192

    @pytest.mark.asyncio
    async def test_get_context_length_from_model_info(self):
        client = OllamaClient()
        with patch.object(client, "show_model", return_value={
            "model_info": {"general.context_length": 32768},
            "parameters": "",
        }):
            result = await client.get_context_length("big:70b")
            assert result == 32768


# ── Improved system prompt tests ──


class TestPromptOptimization:
    def test_prompt_has_principles(self):
        assert "Never delegate understanding" in REACT_SYSTEM_PROMPT

    def test_prompt_has_observe_think_act(self):
        assert "Observe" in REACT_SYSTEM_PROMPT
        assert "Think" in REACT_SYSTEM_PROMPT
        assert "Act" in REACT_SYSTEM_PROMPT

    def test_prompt_has_error_recovery_hint(self):
        assert "alternative approach" in REACT_SYSTEM_PROMPT

    def test_prompt_has_parallel_hint(self):
        assert "parallel" in REACT_SYSTEM_PROMPT

    def test_prompt_is_concise(self):
        # Optimized prompt should be compact
        assert len(REACT_SYSTEM_PROMPT) < 1500


# ── ReactLoop Phase 4 tests ──


def _make_client_mock(responses: list[str] | None = None) -> AsyncMock:
    client = AsyncMock()
    client.timeout = 60.0
    client._context_lengths = {}
    if responses:
        it = iter(responses)

        async def _chat_usage(**kwargs):
            return ChatResponse(content=next(it), input_tokens=10, output_tokens=5)

        client.chat_with_usage = AsyncMock(side_effect=_chat_usage)
        client.chat = AsyncMock(side_effect=lambda **kwargs: next(iter(responses)))
    client.get_context_length = AsyncMock(return_value=8192)
    # chat_stream for streaming path
    client.chat_stream = AsyncMock()
    return client


class TestReactLoopPhase4:
    @pytest.mark.asyncio
    async def test_trace_summary_in_result(self):
        """Result should include trace_summary."""
        responses = [
            json.dumps({"thought": "done", "action": "finish", "action_input": "42"}),
        ]
        client = _make_client_mock()
        it = iter(responses)

        async def _chat_usage(**kwargs):
            return ChatResponse(content=next(it), input_tokens=10, output_tokens=5)

        client.chat_with_usage = AsyncMock(side_effect=_chat_usage)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5)
        result = await loop.run("test", model="test:7b")

        assert result.finished is True
        assert result.trace_summary is not None
        assert "total_steps" in result.trace_summary

    @pytest.mark.asyncio
    async def test_trace_records_tool_usage(self):
        """Trace should record tool calls."""
        responses = [
            json.dumps({"thought": "calc", "action": "calculate", "action_input": "2+3"}),
            json.dumps({"thought": "done", "action": "finish", "action_input": "5"}),
        ]
        client = _make_client_mock()
        it = iter(responses)

        async def _chat_usage(**kwargs):
            return ChatResponse(content=next(it), input_tokens=10, output_tokens=5)

        client.chat_with_usage = AsyncMock(side_effect=_chat_usage)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5)
        result = await loop.run("calc 2+3", model="test:7b")

        assert result.trace_summary is not None
        assert "calculate" in result.trace_summary.get("tool_stats", {})

    @pytest.mark.asyncio
    async def test_step_timeout(self):
        """Step timeout should produce partial result."""
        client = _make_client_mock()

        async def _slow(**kwargs):
            await asyncio.sleep(10)
            return ChatResponse(content="", input_tokens=0, output_tokens=0)

        client.chat_with_usage = AsyncMock(side_effect=_slow)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5, step_timeout_sec=0.1)
        result = await loop.run("slow task", model="test:7b")

        assert result.finished is False
        assert "timed out" in result.answer or "No steps" in result.answer

    @pytest.mark.asyncio
    async def test_total_timeout(self):
        """Total timeout should stop the loop."""
        call_count = 0

        async def _chat(**kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return ChatResponse(
                content=json.dumps({
                    "thought": "keep going",
                    "action": "calculate",
                    "action_input": "1+1",
                }),
                input_tokens=5, output_tokens=5,
            )

        client = _make_client_mock()
        client.chat_with_usage = AsyncMock(side_effect=_chat)

        registry = create_default_registry()
        loop = ReactLoop(
            client=client, tools=registry,
            max_steps=100, total_timeout_sec=0.1,
        )
        result = await loop.run("loop", model="test:7b")
        assert result.finished is False

    @pytest.mark.asyncio
    async def test_oom_fallback(self):
        """OOM error should trigger model fallback."""
        call_count = 0

        async def _chat(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("CUDA error: out of memory")
            return ChatResponse(
                content=json.dumps({"thought": "ok", "action": "finish", "action_input": "done"}),
                input_tokens=5, output_tokens=5,
            )

        client = _make_client_mock()
        client.chat_with_usage = AsyncMock(side_effect=_chat)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5)
        result = await loop.run("task", model="qwen3:32b")

        assert result.finished is True
        assert result.model == "qwen3:14b"  # fallback

    @pytest.mark.asyncio
    async def test_oom_no_fallback_available(self):
        """OOM with no fallback should return error."""
        async def _chat(**kwargs):
            raise Exception("CUDA error: out of memory")

        client = _make_client_mock()
        client.chat_with_usage = AsyncMock(side_effect=_chat)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5)
        result = await loop.run("task", model="unknown:7b")

        assert result.finished is False

    @pytest.mark.asyncio
    async def test_tool_error_structured_feedback(self):
        """Tool error should give structured feedback to LLM."""
        call_count = 0

        async def _chat(**kwargs):
            nonlocal call_count
            call_count += 1
            messages = kwargs.get("messages", [])
            if call_count == 1:
                return ChatResponse(
                    content=json.dumps({
                        "thought": "try bad tool",
                        "action": "nonexistent",
                        "action_input": "x",
                    }),
                    input_tokens=10, output_tokens=5,
                )
            # Second call should have error feedback
            last_user = [m for m in messages if m["role"] == "user"][-1]
            assert "ERROR" in last_user["content"]
            assert "different approach" in last_user["content"]
            return ChatResponse(
                content=json.dumps({"thought": "ok", "action": "finish", "action_input": "done"}),
                input_tokens=10, output_tokens=5,
            )

        client = _make_client_mock()
        client.chat_with_usage = AsyncMock(side_effect=_chat)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5)
        result = await loop.run("do something", model="test:7b")

        assert result.finished is True

    @pytest.mark.asyncio
    async def test_partial_parse_extraction(self):
        """Malformed response with 'final answer' keyword should be extracted."""
        call_count = 0

        async def _chat(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:  # initial + retries
                return ChatResponse(
                    content="The final answer is 42. In conclusion, done.",
                    input_tokens=5, output_tokens=5,
                )
            return ChatResponse(
                content=json.dumps({"thought": "ok", "action": "finish", "action_input": "42"}),
                input_tokens=5, output_tokens=5,
            )

        client = _make_client_mock()
        client.chat_with_usage = AsyncMock(side_effect=_chat)

        registry = create_default_registry()
        loop = ReactLoop(client=client, tools=registry, max_steps=5, max_retries=2)
        result = await loop.run("what is 6*7", model="test:7b")

        # Should extract partial or finish
        assert "42" in result.answer or result.finished

    @pytest.mark.asyncio
    async def test_tool_timeout(self):
        """Tool that times out should report error."""

        async def _slow_handler(inp: str) -> str:
            await asyncio.sleep(10)
            return "done"

        registry = ToolRegistry()
        registry.register(Tool(
            name="slow_tool", description="slow",
            parameters={"input": "x"},
            handler=_slow_handler,
        ))

        responses = [
            json.dumps({"thought": "use slow", "action": "slow_tool", "action_input": "x"}),
            json.dumps({"thought": "done", "action": "finish", "action_input": "recovered"}),
        ]
        client = _make_client_mock()
        it = iter(responses)

        async def _chat(**kwargs):
            return ChatResponse(content=next(it), input_tokens=5, output_tokens=5)

        client.chat_with_usage = AsyncMock(side_effect=_chat)

        loop = ReactLoop(
            client=client, tools=registry,
            max_steps=5, step_timeout_sec=0.1,
        )
        result = await loop.run("test", model="test:7b")
        # Should have error observation about timeout
        assert any("timed out" in s.observation for s in result.steps if s.observation)

    @pytest.mark.asyncio
    async def test_extract_partial_answer(self):
        """_extract_partial_answer should summarize steps."""
        loop = ReactLoop(
            client=AsyncMock(), tools=ToolRegistry(), max_steps=5,
        )
        steps = [
            AgentStep(step=1, thought="t1", action="calculate", action_input="1+1", observation="2"),
            AgentStep(step=2, thought="t2", action="search", action_input="q", observation="found it"),
        ]
        answer = loop._extract_partial_answer(steps)
        assert "Step 1" in answer or "Step 2" in answer

    def test_extract_partial_answer_empty(self):
        loop = ReactLoop(
            client=AsyncMock(), tools=ToolRegistry(), max_steps=5,
        )
        assert "No steps" in loop._extract_partial_answer([])


# ── Context compression tests ──


class TestContextCompression:
    def test_compress_history_basic(self):
        loop = ReactLoop(
            client=AsyncMock(), tools=ToolRegistry(), max_steps=5,
        )
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user1"},
            {"role": "assistant", "content": "reply1"},
            {"role": "user", "content": "user2"},
            {"role": "assistant", "content": "reply2"},
            {"role": "user", "content": "user3"},
            {"role": "assistant", "content": "reply3"},
            {"role": "user", "content": "recent1"},
            {"role": "assistant", "content": "recent2"},
            {"role": "user", "content": "recent3"},
            {"role": "assistant", "content": "recent4"},
        ]
        loop._compress_history(messages)
        # system + compressed + 4 tail
        assert messages[0]["role"] == "system"
        assert len(messages) == 6  # system + summary + 4 tail
        assert "summary" in messages[1]["content"].lower()

    def test_compress_short_history_noop(self):
        loop = ReactLoop(
            client=AsyncMock(), tools=ToolRegistry(), max_steps=5,
        )
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        original_len = len(messages)
        loop._compress_history(messages)
        assert len(messages) == original_len

    @pytest.mark.asyncio
    async def test_maybe_compress_triggers(self):
        """Compression should trigger when token estimate exceeds threshold."""
        client = AsyncMock()
        client.get_context_length = AsyncMock(return_value=100)  # very small

        loop = ReactLoop(client=client, tools=ToolRegistry(), max_steps=5)
        messages = [
            {"role": "system", "content": "s" * 50},
            {"role": "user", "content": "u" * 50},
            {"role": "assistant", "content": "a" * 50},
            {"role": "user", "content": "u" * 50},
            {"role": "assistant", "content": "a" * 50},
            {"role": "user", "content": "u" * 50},
            {"role": "assistant", "content": "a" * 50},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "content": "recent"},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "content": "recent"},
        ]
        await loop._maybe_compress_history(messages, "test:7b")
        # Should have compressed
        assert len(messages) < 11


# ── AgentResult enhancements ──


class TestAgentResultPhase4:
    def test_to_dict_with_trace_summary(self):
        result = AgentResult(
            answer="done", model="test:7b",
            steps=[], finished=True,
            trace_summary={"total_steps": 1, "total_tokens_in": 10},
        )
        d = result.to_dict()
        assert "trace_summary" in d
        assert d["trace_summary"]["total_steps"] == 1

    def test_to_dict_without_trace_summary(self):
        result = AgentResult(answer="done", model="test:7b")
        d = result.to_dict()
        assert "trace_summary" not in d


# ── Fallback map tests ──


class TestFallbackMap:
    def test_known_fallbacks(self):
        assert SMALLER_MODEL_FALLBACKS["qwen3:32b"] == "qwen3:14b"
        assert SMALLER_MODEL_FALLBACKS["gemma3:27b"] == "gemma3:12b"

    def test_oom_patterns(self):
        assert "out of memory" in OOM_PATTERNS
        assert "cuda error" in OOM_PATTERNS


# ── Defaults tests ──


class TestDefaults:
    def test_step_timeout_default(self):
        assert DEFAULT_STEP_TIMEOUT == 120

    def test_total_timeout_default(self):
        assert DEFAULT_TOTAL_TIMEOUT == 600

    def test_loop_timeout_params(self):
        client = AsyncMock()
        loop = ReactLoop(
            client=client, tools=ToolRegistry(),
            step_timeout_sec=30, total_timeout_sec=300,
        )
        assert loop.step_timeout_sec == 30
        assert loop.total_timeout_sec == 300
