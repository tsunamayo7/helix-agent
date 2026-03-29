"""Tests for built-in tools and PathGuard."""

import json
import pytest

from src.pathguard import PathGuard
from src.builtin_tools import (
    _tool_calculate,
    _tool_list_files,
    _tool_read_file,
    _tool_search_in_file,
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
        assert len(names) == 7
