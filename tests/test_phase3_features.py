"""Tests for Phase 3 features: Computer Use, Vision, browse tool."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from src.vision import VisionAnalyzer
from src.computer_use import (
    ComputerUseHandler,
    PlaywrightSession,
    _helix_pilot_available,
    _helix_pilot_call,
    HAS_PLAYWRIGHT,
)
from src.builtin_tools import (
    _tool_computer_use,
    _tool_browse,
    create_full_registry,
)


# ── VisionAnalyzer tests ──


class TestVisionAnalyzer:
    @pytest.mark.asyncio
    async def test_analyze_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message": {"content": "A cat sitting on a desk"}}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            va = VisionAnalyzer()
            result = await va.analyze("base64data", prompt="What is this?")
            assert result == "A cat sitting on a desk"
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_connection_error(self):
        import httpx as httpx_mod

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx_mod.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            va = VisionAnalyzer()
            result = await va.analyze("data", prompt="test")
            assert "unavailable" in result.lower()

    @pytest.mark.asyncio
    async def test_analyze_file_not_found(self):
        va = VisionAnalyzer()
        result = await va.analyze_file("/nonexistent/image.png")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_analyze_file_success(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message": {"content": "An image"}}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            va = VisionAnalyzer()
            result = await va.analyze_file(str(img))
            assert result == "An image"

    @pytest.mark.asyncio
    async def test_is_available_true(self):
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            va = VisionAnalyzer()
            assert await va.is_available() is True

    @pytest.mark.asyncio
    async def test_is_available_false(self):
        import httpx as httpx_mod

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx_mod.ConnectError("nope"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            va = VisionAnalyzer()
            assert await va.is_available() is False

    @pytest.mark.asyncio
    async def test_analyze_custom_model(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message": {"content": "custom result"}}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            va = VisionAnalyzer(model="llava:13b")
            result = await va.analyze("data", model="custom-model")
            assert result == "custom result"
            call_args = mock_client.post.call_args
            payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
            assert payload["model"] == "custom-model"


# ── helix-pilot connectivity tests ──


class TestHelixPilotConnectivity:
    @pytest.mark.asyncio
    async def test_pilot_available_true(self):
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _helix_pilot_available("http://localhost:8765")
            assert result is True

    @pytest.mark.asyncio
    async def test_pilot_available_false(self):
        import httpx as httpx_mod

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx_mod.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _helix_pilot_available("http://localhost:8765")
            assert result is False

    @pytest.mark.asyncio
    async def test_pilot_call_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"jsonrpc": "2.0", "result": {"status": "ok"}, "id": 1}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _helix_pilot_call("take_screenshot")
            assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_pilot_call_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"jsonrpc": "2.0", "error": {"message": "not found"}, "id": 1}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _helix_pilot_call("nonexistent_method")
            assert "error" in result


# ── ComputerUseHandler tests ──


class TestComputerUseHandler:
    @pytest.mark.asyncio
    async def test_no_backend_available(self):
        with patch("src.computer_use._helix_pilot_available", new_callable=AsyncMock, return_value=False), \
             patch("src.computer_use.HAS_PLAYWRIGHT", False):
            handler = ComputerUseHandler(prefer_agent_browser=False)
            handler._use_pilot = False
            result = await handler.execute({"action": "screenshot"})
            assert "error" in result

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        handler = ComputerUseHandler(prefer_agent_browser=False)
        handler._use_pilot = True
        result = await handler.execute({"action": "unknown_action"})
        assert "error" in result
        assert "Unknown action" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_action(self):
        handler = ComputerUseHandler(prefer_agent_browser=False)
        handler._use_pilot = True
        result = await handler.execute({})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_screenshot_with_pilot(self):
        with patch("src.computer_use._helix_pilot_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"image": "base64screenshotdata"}
            handler = ComputerUseHandler(prefer_agent_browser=False)
            handler._use_pilot = True
            result = await handler.execute({"action": "screenshot"})
            assert result["backend"] == "helix-pilot"
            assert result["image_base64"] == "base64screenshotdata"

    @pytest.mark.asyncio
    async def test_screenshot_with_vision_analyze(self):
        with patch("src.computer_use._helix_pilot_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"image": "base64data"}
            mock_vision = AsyncMock()
            mock_vision.analyze = AsyncMock(return_value="A web page with buttons")

            handler = ComputerUseHandler(vision_analyzer=mock_vision, prefer_agent_browser=False)
            handler._use_pilot = True
            result = await handler.execute({
                "action": "screenshot",
                "analyze": True,
                "prompt": "What do you see?",
            })
            assert result["analysis"] == "A web page with buttons"
            mock_vision.analyze.assert_called_once_with("base64data", prompt="What do you see?")

    @pytest.mark.asyncio
    async def test_click_requires_target(self):
        handler = ComputerUseHandler(prefer_agent_browser=False)
        handler._use_pilot = True
        result = await handler.execute({"action": "click"})
        assert "error" in result
        assert "target" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_click_with_pilot(self):
        with patch("src.computer_use._helix_pilot_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"status": "clicked"}
            handler = ComputerUseHandler(prefer_agent_browser=False)
            handler._use_pilot = True
            result = await handler.execute({"action": "click", "target": "#submit"})
            assert result["backend"] == "helix-pilot"

    @pytest.mark.asyncio
    async def test_type_requires_target(self):
        handler = ComputerUseHandler(prefer_agent_browser=False)
        handler._use_pilot = True
        result = await handler.execute({"action": "type"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_type_with_pilot(self):
        with patch("src.computer_use._helix_pilot_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"status": "typed"}
            handler = ComputerUseHandler(prefer_agent_browser=False)
            handler._use_pilot = True
            result = await handler.execute({"action": "type", "target": "#input", "value": "hello"})
            assert result["backend"] == "helix-pilot"

    @pytest.mark.asyncio
    async def test_scroll_with_pilot(self):
        with patch("src.computer_use._helix_pilot_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"status": "scrolled"}
            handler = ComputerUseHandler(prefer_agent_browser=False)
            handler._use_pilot = True
            result = await handler.execute({"action": "scroll", "value": "down"})
            assert result["backend"] == "helix-pilot"

    @pytest.mark.asyncio
    async def test_navigate_requires_url(self):
        handler = ComputerUseHandler(prefer_agent_browser=False)
        handler._use_pilot = True
        result = await handler.execute({"action": "navigate"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_navigate_with_pilot(self):
        with patch("src.computer_use._helix_pilot_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"status": "navigated"}
            handler = ComputerUseHandler(prefer_agent_browser=False)
            handler._use_pilot = True
            result = await handler.execute({"action": "navigate", "url": "https://example.com"})
            assert result["backend"] == "helix-pilot"

    @pytest.mark.asyncio
    async def test_read_page_not_supported_by_pilot(self):
        handler = ComputerUseHandler(prefer_agent_browser=False)
        handler._use_pilot = True
        result = await handler.execute({"action": "read_page"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_resolve_backend_pilot(self):
        with patch("src.computer_use._helix_pilot_available", new_callable=AsyncMock, return_value=True):
            handler = ComputerUseHandler(prefer_agent_browser=False)
            backend = await handler._resolve_backend()
            assert backend == "pilot"

    @pytest.mark.asyncio
    async def test_resolve_backend_playwright(self):
        with patch("src.computer_use._helix_pilot_available", new_callable=AsyncMock, return_value=False):
            handler = ComputerUseHandler(prefer_agent_browser=False)
            if HAS_PLAYWRIGHT:
                backend = await handler._resolve_backend()
                assert backend == "playwright"

    @pytest.mark.asyncio
    async def test_browse_no_backend(self):
        with patch("src.computer_use._helix_pilot_available", new_callable=AsyncMock, return_value=False), \
             patch("src.computer_use.HAS_PLAYWRIGHT", False):
            handler = ComputerUseHandler(prefer_agent_browser=False)
            handler._use_pilot = False
            result = await handler.browse("https://example.com")
            assert "error" in result


# ── PlaywrightSession tests (mock-based) ──


class TestPlaywrightSession:
    @pytest.mark.asyncio
    async def test_navigate_without_playwright(self):
        if HAS_PLAYWRIGHT:
            pytest.skip("Playwright is installed, skip not-installed test")
        session = PlaywrightSession()
        with pytest.raises(RuntimeError, match="Playwright is not installed"):
            await session.navigate("https://example.com")

    @pytest.mark.asyncio
    async def test_close_noop(self):
        session = PlaywrightSession()
        await session.close()  # should not raise


# ── Built-in tool wrappers ──


class TestBuiltinToolWrappers:
    @pytest.mark.asyncio
    async def test_computer_use_invalid_json(self):
        result = await _tool_computer_use("not json")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_computer_use_missing_action(self):
        result = await _tool_computer_use('{"target": "#btn"}')
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_computer_use_delegates(self):
        with patch("src.builtin_tools._get_computer_use_handler") as mock_get:
            mock_handler = AsyncMock()
            mock_handler.execute = AsyncMock(return_value={"backend": "pilot", "result": "ok"})
            mock_get.return_value = mock_handler

            result = await _tool_computer_use('{"action": "screenshot"}')
            assert "pilot" in result

    @pytest.mark.asyncio
    async def test_computer_use_error_result(self):
        with patch("src.builtin_tools._get_computer_use_handler") as mock_get:
            mock_handler = AsyncMock()
            mock_handler.execute = AsyncMock(return_value={"error": "no backend"})
            mock_get.return_value = mock_handler

            result = await _tool_computer_use('{"action": "screenshot"}')
            assert "Error" in result

    @pytest.mark.asyncio
    async def test_browse_invalid_json(self):
        result = await _tool_browse("not json")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_browse_missing_url(self):
        result = await _tool_browse('{"task": "summarize"}')
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_browse_delegates(self):
        with patch("src.builtin_tools._get_computer_use_handler") as mock_get:
            mock_handler = AsyncMock()
            mock_handler.browse = AsyncMock(return_value={"backend": "playwright", "text": "Hello"})
            mock_get.return_value = mock_handler

            result = await _tool_browse('{"url": "https://example.com"}')
            assert "Hello" in result

    @pytest.mark.asyncio
    async def test_browse_error_result(self):
        with patch("src.builtin_tools._get_computer_use_handler") as mock_get:
            mock_handler = AsyncMock()
            mock_handler.browse = AsyncMock(return_value={"error": "no backend"})
            mock_get.return_value = mock_handler

            result = await _tool_browse('{"url": "https://example.com"}')
            assert "Error" in result


# ── Registry tests ──


class TestRegistryPhase3:
    def test_computer_use_registered(self):
        registry = create_full_registry()
        tool = registry.get("computer_use")
        assert tool is not None
        assert tool.is_read_only is True
        assert tool.json_schema is not None
        assert "action" in tool.json_schema.get("properties", {})

    def test_browse_registered(self):
        registry = create_full_registry()
        tool = registry.get("browse")
        assert tool is not None
        assert tool.is_read_only is True
        assert tool.json_schema is not None
        assert "url" in tool.json_schema.get("properties", {})

    def test_ollama_tool_format_computer_use(self):
        registry = create_full_registry()
        tool = registry.get("computer_use")
        ollama = tool.to_ollama_tool()
        assert ollama["type"] == "function"
        assert ollama["function"]["name"] == "computer_use"
        assert "action" in ollama["function"]["parameters"]["properties"]

    def test_ollama_tool_format_browse(self):
        registry = create_full_registry()
        tool = registry.get("browse")
        ollama = tool.to_ollama_tool()
        assert ollama["type"] == "function"
        assert ollama["function"]["name"] == "browse"

    def test_all_phase2_tools_still_exist(self):
        registry = create_full_registry()
        expected = [
            "calculate", "read_file", "write_file", "list_files",
            "search_in_file", "run_command", "search_memory",
            "add_memory", "fork_task",
        ]
        for name in expected:
            assert registry.get(name) is not None, f"Tool {name} missing from registry"

    def test_total_tool_count(self):
        registry = create_full_registry()
        names = registry.list_names()
        assert len(names) == 12  # 9 Phase 2 + 2 Phase 3 + 1 web_search

    def test_read_only_flags_phase3(self):
        registry = create_full_registry()
        assert registry.get("computer_use").is_read_only is True
        assert registry.get("browse").is_read_only is True
