"""Tests for built-in tools and PathGuard."""

import json
import os

import pytest
from unittest.mock import AsyncMock, patch

from pathlib import Path

from src.pathguard import PathGuard

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
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
        readme = str(Path(_PROJECT_ROOT) / "README.md")
        resolved = guard.validate(readme)
        assert resolved.name == "README.md"

    def test_blocked_outside_allowlist(self):
        guard = PathGuard()
        outside = "C:/Windows/System32/config" if os.name == "nt" else "/tmp/evil_path"
        with pytest.raises(PermissionError, match="outside allowed"):
            guard.validate(outside)

    def test_blocked_env_file(self):
        guard = PathGuard()
        env_path = str(Path(_PROJECT_ROOT) / ".env")
        with pytest.raises(PermissionError, match="Sensitive file"):
            guard.validate(env_path)

    def test_blocked_ssh_key(self):
        guard = PathGuard()
        ssh_path = str(Path(_PROJECT_ROOT) / "id_rsa")
        with pytest.raises(PermissionError, match="Sensitive file"):
            guard.validate(ssh_path)

    def test_blocked_pem_extension(self):
        guard = PathGuard()
        pem_path = str(Path(_PROJECT_ROOT) / "cert.pem")
        with pytest.raises(PermissionError, match="Sensitive file type"):
            guard.validate(pem_path)

    def test_blocked_credentials(self):
        guard = PathGuard()
        cred_path = str(Path(_PROJECT_ROOT) / "credentials.json")
        with pytest.raises(PermissionError, match="Sensitive file"):
            guard.validate(cred_path)

    def test_custom_roots(self):
        guard = PathGuard(allowed_roots=[Path("/tmp/custom_root")])
        with pytest.raises(PermissionError):
            guard.validate(_PROJECT_ROOT + "/test.txt")

    def test_traversal_attack(self):
        guard = PathGuard()
        traversal = str(Path(_PROJECT_ROOT) / "../../../../../../etc/passwd")
        with pytest.raises(PermissionError):
            guard.validate(traversal)

    def test_blocked_access_json(self):
        guard = PathGuard()
        access_path = str(Path(_PROJECT_ROOT) / "access.json")
        with pytest.raises(PermissionError, match="Sensitive file"):
            guard.validate(access_path)

    def test_blocked_ssh_directory(self):
        guard = PathGuard()
        ssh_config = str(Path(_PROJECT_ROOT) / ".ssh" / "config")
        with pytest.raises(PermissionError, match="Sensitive file"):
            guard.validate(ssh_config)

    def test_blocked_gnupg_directory(self):
        guard = PathGuard()
        gpg_key = str(Path(_PROJECT_ROOT) / ".gnupg" / "pubring.kbx")
        with pytest.raises(PermissionError, match="Sensitive file"):
            guard.validate(gpg_key)


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
        result = await _tool_read_file(str(Path(_PROJECT_ROOT) / "README.md"))
        assert "helix-agent" in result

    @pytest.mark.asyncio
    async def test_read_nonexistent(self):
        result = await _tool_read_file(str(Path(_PROJECT_ROOT) / "nonexistent.txt"))
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_read_blocked_path(self):
        blocked = "C:/Windows/System32/config/SAM" if os.name == "nt" else "/etc/shadow"
        result = await _tool_read_file(blocked)
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
    @pytest.mark.skipif(os.environ.get("CI") == "true", reason="Local path not available in CI")
    async def test_list_directory(self):
        result = await _tool_list_files(_PROJECT_ROOT)
        assert "README.md" in result
        assert "src" in result

    @pytest.mark.asyncio
    async def test_list_nonexistent(self):
        nonexistent = str(Path(_PROJECT_ROOT) / "nonexistent_dir_xyz")
        result = await _tool_list_files(nonexistent)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_list_blocked_path(self):
        blocked = "C:/Windows/System32" if os.name == "nt" else "/etc"
        result = await _tool_list_files(blocked)
        assert "Error" in result or "denied" in result


class TestSearchInFileTool:
    @pytest.mark.asyncio
    @pytest.mark.skipif(os.environ.get("CI") == "true", reason="Local path not available in CI")
    async def test_search_found(self):
        params = json.dumps({
            "path": str(Path(_PROJECT_ROOT) / "README.md"),
            "pattern": "helix-agent"
        })
        result = await _tool_search_in_file(params)
        assert "L" in result  # Line numbers

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.environ.get("CI") == "true", reason="Local path not available in CI")
    async def test_search_not_found(self):
        params = json.dumps({
            "path": str(Path(_PROJECT_ROOT) / "README.md"),
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
            mock_mem.search.assert_called_once_with("test", top_k=3, source=None, category=None)

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
        assert "web_search" in names
        assert len(names) == 12
