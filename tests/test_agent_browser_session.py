"""Tests for agent_browser_session module."""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent_browser_session import AgentBrowserSession, is_available


def test_is_available_returns_bool():
    """is_available should return a boolean."""
    result = is_available()
    assert isinstance(result, bool)


def test_session_init_defaults():
    """Default session name should be 'helix-agent'."""
    s = AgentBrowserSession()
    assert s._session == "helix-agent"
    assert s._profile is None
    assert s._started is False


def test_session_custom_name():
    """Custom session name and profile should be stored."""
    s = AgentBrowserSession(profile="/my/profile", session="test-sess")
    assert s._session == "test-sess"
    assert s._profile == "/my/profile"


@pytest.mark.asyncio
async def test_run_returns_error_on_missing_cli():
    """When agent-browser is not installed, _resolve_cli raises."""
    with patch("src.agent_browser_session.shutil.which", return_value=None):
        from src.agent_browser_session import _resolve_cli
        with pytest.raises(RuntimeError, match="not found"):
            _resolve_cli()


@pytest.mark.asyncio
async def test_navigate_parses_success():
    """navigate() should parse JSON success response."""
    s = AgentBrowserSession()
    mock_result = {"success": True, "data": {"title": "Example", "url": "https://example.com/"}, "error": None}
    with patch.object(s, "_run", new=AsyncMock(return_value=mock_result)):
        result = await s.navigate("https://example.com")
        assert "navigated" in result
        assert "https://example.com" in result


@pytest.mark.asyncio
async def test_navigate_handles_error():
    """navigate() should return error message when _run returns error."""
    s = AgentBrowserSession()
    with patch.object(s, "_run", new=AsyncMock(return_value={"error": "timeout"})):
        result = await s.navigate("https://example.com")
        assert "[nav failed]" in result
        assert "timeout" in result


@pytest.mark.asyncio
async def test_type_text_uses_fill():
    """type_text() should invoke 'fill' command for React-friendly input."""
    s = AgentBrowserSession()
    with patch.object(s, "_run", new=AsyncMock(return_value={"success": True, "error": None})) as mock_run:
        result = await s.type_text("#myfield", "hello world")
        mock_run.assert_called_once_with("fill", "#myfield", "hello world")
        assert "filled" in result
        assert "11 chars" in result


@pytest.mark.asyncio
async def test_keyboard_type_no_selector():
    """keyboard_type() should call 'keyboard type' without selector."""
    s = AgentBrowserSession()
    with patch.object(s, "_run", new=AsyncMock(return_value={"success": True, "error": None})) as mock_run:
        result = await s.keyboard_type("abc")
        mock_run.assert_called_once_with("keyboard", "type", "abc")
        assert "3 chars" in result


@pytest.mark.asyncio
async def test_scroll_direction():
    """scroll() should convert direction to delta."""
    s = AgentBrowserSession()
    with patch.object(s, "_run", new=AsyncMock(return_value={"success": True, "error": None})) as mock_run:
        await s.scroll("down", 500)
        mock_run.assert_called_once_with("scroll", "0", "500")

        mock_run.reset_mock()
        await s.scroll("up", 300)
        mock_run.assert_called_once_with("scroll", "0", "-300")


@pytest.mark.asyncio
async def test_press_key():
    """press() should call 'press' command."""
    s = AgentBrowserSession()
    with patch.object(s, "_run", new=AsyncMock(return_value={"success": True, "error": None})) as mock_run:
        result = await s.press("Enter")
        mock_run.assert_called_once_with("press", "Enter")
        assert "pressed: Enter" in result
