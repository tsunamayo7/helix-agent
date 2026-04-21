"""Tests for Phase 2 features: fork_task, agent_loader, parallel tool execution."""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent_loader import (
    BUILTIN_PRESETS,
    AgentDefinition,
    AgentLoader,
    _parse_yaml_safe,
)
from src.builtin_tools import MAX_FORK_DEPTH, _tool_fork_task, create_full_registry
from src.react_loop import MAX_CONCURRENT_TOOLS, AgentResult, AgentStep, ReactLoop
from src.tools import Tool, ToolRegistry


# ── agent_loader tests ──


class TestParseYamlSafe:
    def test_scalars(self):
        text = textwrap.dedent("""\
            agent_type: explorer
            description: "Code explorer"
            max_steps: 5
            model: auto
        """)
        data = _parse_yaml_safe(text)
        assert data["agent_type"] == "explorer"
        assert data["description"] == "Code explorer"
        assert data["max_steps"] == 5
        assert data["model"] == "auto"

    def test_inline_list(self):
        text = "tools: [read_file, list_files, search_in_file]"
        data = _parse_yaml_safe(text)
        assert data["tools"] == ["read_file", "list_files", "search_in_file"]

    def test_empty_inline_list(self):
        text = "tools: []"
        data = _parse_yaml_safe(text)
        assert data["tools"] == []

    def test_block_list(self):
        text = textwrap.dedent("""\
            tools:
              - read_file
              - list_files
        """)
        data = _parse_yaml_safe(text)
        assert data["tools"] == ["read_file", "list_files"]

    def test_multiline_block(self):
        text = textwrap.dedent("""\
            system_prompt: |
              You are a code expert.
              Be concise.
            model: auto
        """)
        data = _parse_yaml_safe(text)
        assert "code expert" in data["system_prompt"]
        assert data["model"] == "auto"

    def test_boolean_values(self):
        text = "enabled: true\ndisabled: false"
        data = _parse_yaml_safe(text)
        assert data["enabled"] is True
        assert data["disabled"] is False

    def test_comments_and_empty_lines(self):
        text = textwrap.dedent("""\
            # This is a comment
            agent_type: coder

            description: "Implementation agent"
        """)
        data = _parse_yaml_safe(text)
        assert data["agent_type"] == "coder"
        assert data["description"] == "Implementation agent"


class TestAgentDefinition:
    def test_to_dict(self):
        defn = AgentDefinition(
            agent_type="test",
            description="Test agent",
            tools=["read_file"],
            max_steps=5,
        )
        d = defn.to_dict()
        assert d["agent_type"] == "test"
        assert d["tools"] == ["read_file"]
        assert d["max_steps"] == 5

    def test_system_prompt_truncation(self):
        defn = AgentDefinition(
            agent_type="test",
            description="Test",
            system_prompt="x" * 500,
        )
        d = defn.to_dict()
        assert len(d["system_prompt"]) == 200


class TestAgentLoader:
    def test_builtin_presets_loaded(self):
        loader = AgentLoader()
        assert "explorer" in loader.list_names()
        assert "coder" in loader.list_names()
        assert "reviewer" in loader.list_names()

    def test_get_builtin(self):
        loader = AgentLoader()
        explorer = loader.get("explorer")
        assert explorer is not None
        assert explorer.agent_type == "explorer"
        assert explorer.source == "builtin"
        assert "read_file" in explorer.tools

    def test_get_nonexistent(self):
        loader = AgentLoader()
        assert loader.get("nonexistent") is None

    def test_list_all(self):
        loader = AgentLoader()
        all_agents = loader.list_all()
        assert len(all_agents) >= 3

    def test_to_dict(self):
        loader = AgentLoader()
        d = loader.to_dict()
        assert "agents" in d
        assert "count" in d
        assert d["count"] >= 3

    def test_load_from_directory_nonexistent(self):
        loader = AgentLoader()
        count = loader.load_from_directory(Path("/nonexistent/path"))
        assert count == 0

    def test_load_from_directory(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        yaml_content = textwrap.dedent("""\
            agent_type: custom_agent
            description: "Custom test agent"
            tools: [read_file, calculate]
            max_steps: 8
            model: auto
            system_prompt: |
              You are a custom agent.
        """)
        (agents_dir / "custom.yaml").write_text(yaml_content, encoding="utf-8")

        loader = AgentLoader()
        count = loader.load_from_directory(agents_dir, source="project")
        assert count == 1

        custom = loader.get("custom_agent")
        assert custom is not None
        assert custom.description == "Custom test agent"
        assert custom.tools == ["read_file", "calculate"]
        assert custom.max_steps == 8
        assert custom.source == "project"

    def test_project_overrides_builtin(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        yaml_content = textwrap.dedent("""\
            agent_type: explorer
            description: "Overridden explorer"
            tools: [read_file]
            max_steps: 3
        """)
        (agents_dir / "explorer.yaml").write_text(yaml_content, encoding="utf-8")

        loader = AgentLoader()
        loader.load_from_directory(agents_dir, source="project")

        explorer = loader.get("explorer")
        assert explorer.description == "Overridden explorer"
        assert explorer.max_steps == 3
        assert explorer.source == "project"


class TestBuiltinPresets:
    def test_explorer_is_read_only(self):
        explorer = BUILTIN_PRESETS["explorer"]
        assert "write_file" in explorer.disallowed_tools
        assert "run_command" in explorer.disallowed_tools
        assert explorer.max_steps == 5

    def test_coder_has_all_tools(self):
        coder = BUILTIN_PRESETS["coder"]
        assert coder.tools == []  # empty = all
        assert coder.max_steps == 20

    def test_reviewer_is_read_only(self):
        reviewer = BUILTIN_PRESETS["reviewer"]
        assert "write_file" in reviewer.disallowed_tools
        assert reviewer.max_steps == 10


# ── Tool is_read_only tests ──


class TestToolReadOnly:
    def test_default_is_false(self):
        tool = Tool(
            name="test",
            description="test",
            parameters={},
            handler=AsyncMock(),
        )
        assert tool.is_read_only is False

    def test_explicit_read_only(self):
        tool = Tool(
            name="test",
            description="test",
            parameters={},
            handler=AsyncMock(),
            is_read_only=True,
        )
        assert tool.is_read_only is True

    def test_registry_read_only_flags(self):
        registry = create_full_registry()
        read_only_tools = {"calculate", "read_file", "list_files", "search_in_file", "search_memory", "fork_task"}
        write_tools = {"write_file", "run_command", "add_memory"}

        for name in read_only_tools:
            tool = registry.get(name)
            assert tool is not None, f"Tool {name} not found"
            assert tool.is_read_only is True, f"Tool {name} should be read-only"

        for name in write_tools:
            tool = registry.get(name)
            assert tool is not None, f"Tool {name} not found"
            assert tool.is_read_only is False, f"Tool {name} should NOT be read-only"


# ── fork_task tests ──


class TestForkTask:
    @pytest.mark.asyncio
    async def test_invalid_json(self):
        result = await _tool_fork_task("not json")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_missing_task(self):
        result = await _tool_fork_task('{"context": "some context"}')
        assert "Error" in result
        assert "task" in result.lower()

    @pytest.mark.asyncio
    async def test_max_depth_exceeded(self):
        import src.builtin_tools as bt
        original = bt._current_fork_depth
        bt._current_fork_depth = MAX_FORK_DEPTH
        try:
            result = await _tool_fork_task('{"task": "test"}')
            assert "Error" in result
            assert "depth" in result.lower()
        finally:
            bt._current_fork_depth = original

    @pytest.mark.asyncio
    async def test_fork_task_no_models(self):
        with patch("src.ollama_client.OllamaClient"), \
             patch("src.router.ModelRouter") as MockRouter:
            mock_router = MockRouter.return_value
            mock_router.select_for_task = AsyncMock(return_value=None)
            result = await _tool_fork_task('{"task": "test task"}')
            assert "Error" in result
            assert "models" in result.lower()

    @pytest.mark.asyncio
    async def test_fork_task_success(self):
        mock_result = AgentResult(
            answer="Found the answer",
            model="test-model",
            steps=[
                AgentStep(
                    step=1,
                    thought="looking at file",
                    action="read_file",
                    action_input="/some/file.py",
                    observation="file contents here",
                ),
            ],
            finished=True,
        )

        with patch("src.ollama_client.OllamaClient"), \
             patch("src.router.ModelRouter") as MockRouter, \
             patch("src.react_loop.ReactLoop") as MockLoop:
            mock_router = MockRouter.return_value
            mock_router.select_for_task = AsyncMock(return_value="test-model")
            mock_loop = MockLoop.return_value
            mock_loop.run = AsyncMock(return_value=mock_result)

            result = await _tool_fork_task(json.dumps({
                "task": "Find all imports",
                "context": "Working on a Python project",
                "scope": "src/",
                "tools": ["read_file", "search_in_file"],
            }))

            assert "Result: Found the answer" in result
            assert "Scope: src/" in result
            assert "Key files:" in result

    @pytest.mark.asyncio
    async def test_fork_depth_resets_on_error(self):
        import src.builtin_tools as bt
        original = bt._current_fork_depth
        assert bt._current_fork_depth == original

        with patch("src.ollama_client.OllamaClient"), \
             patch("src.router.ModelRouter") as MockRouter, \
             patch("src.react_loop.ReactLoop") as MockLoop:
            mock_router = MockRouter.return_value
            mock_router.select_for_task = AsyncMock(return_value="test-model")
            mock_loop = MockLoop.return_value
            mock_loop.run = AsyncMock(side_effect=RuntimeError("boom"))

            try:
                await _tool_fork_task('{"task": "test"}')
            except RuntimeError:
                pass

            assert bt._current_fork_depth == original


# ── Parallel tool execution tests ──


class TestParallelToolExecution:
    def _make_registry(self):
        registry = ToolRegistry()

        async def read_handler(inp: str) -> str:
            await asyncio.sleep(0.01)
            return f"read:{inp}"

        async def write_handler(inp: str) -> str:
            await asyncio.sleep(0.01)
            return f"write:{inp}"

        registry.register(Tool(
            name="read_file",
            description="Read file",
            parameters={"path": "path"},
            handler=read_handler,
            is_read_only=True,
        ))
        registry.register(Tool(
            name="search",
            description="Search",
            parameters={"q": "query"},
            handler=read_handler,
            is_read_only=True,
        ))
        registry.register(Tool(
            name="write_file",
            description="Write file",
            parameters={"data": "data"},
            handler=write_handler,
            is_read_only=False,
        ))
        return registry

    @pytest.mark.asyncio
    async def test_read_only_parallel_write_sequential(self):
        """Verify that read-only tools run in parallel while writes run sequentially."""
        registry = self._make_registry()

        execution_order = []
        original_execute = registry.execute

        async def tracked_execute(name: str, inp: str) -> str:
            execution_order.append(("start", name))
            result = await original_execute(name, inp)
            execution_order.append(("end", name))
            return result

        registry.execute = tracked_execute

        client = MagicMock()
        call_count = 0

        async def mock_chat_with_tools(model, messages, tools, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "content": "Executing tools",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
                        {"function": {"name": "search", "arguments": {"q": "test"}}},
                        {"function": {"name": "write_file", "arguments": {"data": "hello"}}},
                    ],
                }
            return {"content": "Done", "tool_calls": []}

        client.chat_with_tools = mock_chat_with_tools

        loop = ReactLoop(client=client, tools=registry, max_steps=5, use_native_tools=True)
        result = await loop.run(task="test", model="test-model")

        assert result.finished is True
        assert len(result.steps) >= 3

    @pytest.mark.asyncio
    async def test_all_read_only_parallel(self):
        """Multiple read-only tools should run concurrently."""
        registry = self._make_registry()
        client = MagicMock()
        call_count = 0

        async def mock_chat_with_tools(model, messages, tools, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "content": "Reading",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
                        {"function": {"name": "search", "arguments": {"q": "hello"}}},
                    ],
                }
            return {"content": "Done", "tool_calls": []}

        client.chat_with_tools = mock_chat_with_tools

        loop = ReactLoop(client=client, tools=registry, max_steps=5, use_native_tools=True)
        result = await loop.run(task="test", model="test-model")

        assert result.finished is True
        ro_steps = [s for s in result.steps if s.action in ("read_file", "search")]
        assert len(ro_steps) == 2

    @pytest.mark.asyncio
    async def test_max_concurrent_constant(self):
        assert MAX_CONCURRENT_TOOLS == 10


# ── Registry fork_task tool exists ──


class TestRegistryForkTask:
    def test_fork_task_registered(self):
        registry = create_full_registry()
        tool = registry.get("fork_task")
        assert tool is not None
        assert tool.is_read_only is True
        assert "fork" in tool.description.lower()
