"""Tests for built-in tools and PathGuard."""

import json
import pytest
from unittest.mock import AsyncMock, patch

from src.pathguard import PathGuard
from src.builtin_tools import (
    _tool_calculate,
    _tool_list_files,
    _tool_read_file,
    _tool_search_in_file,
    _tool_search_memory,
    _tool_add_memory,
    _tool_write_file,
    create_full_registry,
)


# --- PathGuard tests ---


class TestPathGuard:
    def test_allowed_path(self):
        guard = PathGuard()
        resolved = guard.validate("C:/Development/tools/helix-agent/README.md")
        assert resolved.name == "README.md"

    def test_blocked_outside_allowlist(self):
        guard = PathGuard()
        with pytest.raises(PermissionError, match="outside allowed"):
            guard.validate("C:/Windows/System32/config")

    def test_blocked_env_file(self):
        guard = PathGuard()
        with pytest.raises(PermissionError, match="Sensitive file"):
            guard.validate("C:/Development/.env")

    def test_blocked_ssh_key(self):
        guard = PathGuard()
        with pytest.raises(PermissionError, match="Sensitive file"):
            guard.validate("C:/Development/id_rsa")

    def test_blocked_pem_extension(self):
        guard = PathGuard()
        with pytest.raises(PermissionError, match="Sensitive file type"):
            guard.validate("C:/Development/cert.pem")

    def test_blocked_credentials(self):
        guard = PathGuard()
        with pytest.raises(PermissionError, match="Sensitive file"):
            guard.validate("C:/Development/credentials.json")

    def test_custom_roots(self):
        from pathlib import Path
        guard = PathGuard(allowed_roots=[Path("C:/tmp")])
        with pytest.raises(PermissionError):
            guard.validate("C:/Development/test.txt")

    def test_traversal_attack(self):
        guard = PathGuard()
        with pytest.raises(PermissionError):
            guard.validate("C:/Development/../../Windows/System32/config")

    def test_blocked_access_json(self):
        guard = PathGuard()
        with pytest.raises(PermissionError, match="Sensitive file"):
            guard.validate("C:/Development/access.json")


# --- Tool tests ---


class TestCalculateTool:
    @pytest.mark.asyncio
    async def test_basic_math(self):
        assert await _tool_calculate("2 + 3") == "5"

    @pytest.mark.asyncio
    async def test_complex_math(self):
        assert await _tool_calculate("(10 + 5) * 3") == "45"

    @pytest.mark.asyncio
    async def test_unsafe_expression(self):
        result = await _tool_calculate("import os")
        assert "Error" in result


class TestReadFileTool:
    @pytest.mark.asyncio
    async def test_read_existing_file(self):
        result = await _tool_read_file("C:/Development/tools/helix-agent/README.md")
        assert "helix-agent" in result

    @pytest.mark.asyncio
    async def test_read_nonexistent(self):
        result = await _tool_read_file("C:/Development/tools/helix-agent/nonexistent.txt")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_read_blocked_path(self):
        result = await _tool_read_file("C:/Windows/System32/config/SAM")
        assert "Error" in result or "denied" in result


class TestWriteFileTool:
    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path):
        # tmp_path is outside allowlist, so this should fail with PermissionError
        path = str(tmp_path / "test.txt")
        params = json.dumps({"path": path, "content": "hello"})
        result = await _tool_write_file(params)
        assert "Error" in result or "denied" in result

    @pytest.mark.asyncio
    async def test_write_bad_json(self):
        result = await _tool_write_file("not json")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_write_missing_fields(self):
        result = await _tool_write_file(json.dumps({"path": "test.txt"}))
        assert "Error" in result


class TestListFilesTool:
    @pytest.mark.asyncio
    async def test_list_directory(self):
        result = await _tool_list_files("C:/Development/tools/helix-agent")
        assert "README.md" in result
        assert "src" in result

    @pytest.mark.asyncio
    async def test_list_nonexistent(self):
        result = await _tool_list_files("C:/Development/nonexistent_dir_xyz")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_list_blocked_path(self):
        result = await _tool_list_files("C:/Windows/System32")
        assert "Error" in result or "denied" in result


class TestSearchInFileTool:
    @pytest.mark.asyncio
    async def test_search_found(self):
        params = json.dumps({
            "path": "C:/Development/tools/helix-agent/README.md",
            "pattern": "helix-agent"
        })
        result = await _tool_search_in_file(params)
        assert "L" in result  # Line numbers

    @pytest.mark.asyncio
    async def test_search_not_found(self):
        params = json.dumps({
            "path": "C:/Development/tools/helix-agent/README.md",
            "pattern": "zzz_nonexistent_pattern_zzz"
        })
        result = await _tool_search_in_file(params)
        assert "No matches" in result

    @pytest.mark.asyncio
    async def test_search_bad_json(self):
        result = await _tool_search_in_file("not json")
        assert "Error" in result


class TestMemoryTools:
    @pytest.mark.asyncio
    async def test_search_memory_with_json(self):
        mock_hits = [
            {"text": "test memory", "score": 0.95, "created_at": "", "source": "test"}
        ]
        with patch("src.builtin_tools._memory") as mock_mem:
            mock_mem.search = AsyncMock(return_value=mock_hits)
            result = await _tool_search_memory(json.dumps({"query": "test", "top_k": 3}))
            assert "test memory" in result
            assert "0.95" in result
            mock_mem.search.assert_called_once_with("test", top_k=3)

    @pytest.mark.asyncio
    async def test_search_memory_with_plain_string(self):
        with patch("src.builtin_tools._memory") as mock_mem:
            mock_mem.search = AsyncMock(return_value=[])
            result = await _tool_search_memory("test query")
            assert "No memories found" in result

    @pytest.mark.asyncio
    async def test_search_memory_error(self):
        with patch("src.builtin_tools._memory") as mock_mem:
            mock_mem.search = AsyncMock(side_effect=RuntimeError("connection failed"))
            result = await _tool_search_memory("test")
            assert "Memory search error" in result

    @pytest.mark.asyncio
    async def test_add_memory_with_json(self):
        with patch("src.builtin_tools._memory") as mock_mem:
            mock_mem.add = AsyncMock(return_value="abc-123")
            result = await _tool_add_memory(json.dumps({"text": "remember this"}))
            assert "abc-123" in result
            mock_mem.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_memory_with_plain_string(self):
        with patch("src.builtin_tools._memory") as mock_mem:
            mock_mem.add = AsyncMock(return_value="xyz-789")
            result = await _tool_add_memory("plain text memory")
            assert "xyz-789" in result

    @pytest.mark.asyncio
    async def test_add_memory_empty_text(self):
        result = await _tool_add_memory(json.dumps({"text": ""}))
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_add_memory_error(self):
        with patch("src.builtin_tools._memory") as mock_mem:
            mock_mem.add = AsyncMock(side_effect=RuntimeError("failed"))
            result = await _tool_add_memory("test")
            assert "Memory add error" in result


class TestFullRegistry:
    def test_all_tools_registered(self):
        registry = create_full_registry()
        names = registry.list_names()
        assert "calculate" in names
        assert "read_file" in names
        assert "write_file" in names
        assert "list_files" in names
        assert "search_in_file" in names
        assert "run_command" in names
        assert "search_memory" in names
        assert "add_memory" in names
        assert "fork_task" in names
        assert "computer_use" in names
        assert "browse" in names
        assert len(names) == 11
