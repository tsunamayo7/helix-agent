"""Tests for MCP security middleware integration in server.py.

Verifies that SecurityMiddleware correctly applies the deny-by-default
security policy from src/mcp_security.py to all tool calls via FastMCP's
middleware infrastructure.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_security import (
    AUDIT_LOG_PATH,
    RiskLevel,
    _TOOL_REGISTRY,
    check_tool_permission,
    get_risk_level,
)


# ── Unit: check_tool_permission for helix-agent tools ──


class TestCheckToolPermission:
    """Direct tests for check_tool_permission() policy decisions."""

    def test_low_risk_tool_allowed_silently(self):
        allowed, reason = check_tool_permission("think")
        assert allowed is True
        assert reason == ""

    def test_low_risk_tools_include_read_only(self):
        for tool in ("see", "providers", "models", "config", "list_agents",
                      "list_learned_skills", "get_skill", "retry_guard_check",
                      "vision_compress", "dom_compress", "browse",
                      "dept_search", "x_search"):
            allowed, reason = check_tool_permission(tool)
            assert allowed is True, f"{tool} should be allowed (LOW)"
            assert reason == "", f"{tool} should have no reason (LOW)"

    def test_medium_risk_tool_allowed_with_audit(self):
        allowed, reason = check_tool_permission("agent_task")
        assert allowed is True
        assert reason.startswith("audit:")

    def test_medium_risk_tools_all_pass(self):
        medium_tools = [
            "agent_task", "parallel_tasks", "fork_task", "spawn_agent",
            "send_agent_input", "wait_agent", "close_agent",
            "retry_guard_reset", "evolving_memory_review",
            "dept_store", "code_review",
        ]
        for tool in medium_tools:
            allowed, reason = check_tool_permission(tool)
            assert allowed is True, f"{tool} should be allowed (MEDIUM)"
            assert "audit" in reason, f"{tool} should have audit reason"

    def test_high_risk_tool_denied(self):
        allowed, reason = check_tool_permission("computer_use", {"action": "click"})
        assert allowed is False
        assert "confirmation_required" in reason

    def test_unknown_tool_denied(self):
        allowed, reason = check_tool_permission("totally_unknown_tool_xyz")
        assert allowed is False
        assert "unknown_tool" in reason


# ── Parameter escalation ──


class TestParameterEscalation:
    """Tests for parameter-aware risk escalation rules."""

    def test_computer_use_screenshot_analyzed_is_medium(self):
        """computer_use with action=screenshot + analyze=True downgrades to MEDIUM."""
        allowed, reason = check_tool_permission("computer_use", {"action": "screenshot", "analyze": True})
        assert allowed is True
        assert "audit" in reason

    def test_computer_use_screenshot_raw_is_high(self):
        """computer_use with action=screenshot without analyze stays HIGH."""
        allowed, reason = check_tool_permission("computer_use", {"action": "screenshot"})
        assert allowed is False

    def test_computer_use_click_remains_high(self):
        """computer_use with action=click stays HIGH."""
        allowed, reason = check_tool_permission("computer_use", {"action": "click"})
        assert allowed is False
        assert "confirmation_required" in reason

    def test_computer_use_type_remains_high(self):
        allowed, reason = check_tool_permission("computer_use", {"action": "type"})
        assert allowed is False

    def test_agent_task_high_steps_escalates(self):
        """agent_task with max_steps > 20 escalates to HIGH."""
        allowed, reason = check_tool_permission("agent_task", {"max_steps": 25})
        assert allowed is False
        assert "confirmation_required" in reason

    def test_agent_task_normal_steps_stays_medium(self):
        allowed, reason = check_tool_permission("agent_task", {"max_steps": 10})
        assert allowed is True

    def test_write_file_sensitive_path_escalates(self):
        """write_file to .env path escalates to HIGH."""
        allowed, reason = check_tool_permission("write_file", {"path": "/app/.env"})
        assert allowed is False

    def test_write_file_normal_path_stays_medium(self):
        allowed, reason = check_tool_permission("write_file", {"path": "/app/src/main.py"})
        assert allowed is True


# ── Audit log ──


class TestAuditLog:
    """Tests for JSONL audit log writing."""

    def test_medium_tool_writes_audit_log(self, tmp_path):
        log_file = tmp_path / "test_audit.jsonl"
        with patch("src.mcp_security.AUDIT_LOG_PATH", log_file):
            check_tool_permission("agent_task", {"max_steps": 5})

        assert log_file.exists()
        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["tool"] == "agent_task"
        assert entries[0]["risk"] == "medium"
        assert entries[0]["allowed"] is True

    def test_high_tool_writes_audit_log(self, tmp_path):
        log_file = tmp_path / "test_audit.jsonl"
        with patch("src.mcp_security.AUDIT_LOG_PATH", log_file):
            check_tool_permission("computer_use", {"action": "click"})

        assert log_file.exists()
        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["tool"] == "computer_use"
        assert entries[0]["risk"] == "high"
        assert entries[0]["allowed"] is False

    def test_low_tool_does_not_write_log(self, tmp_path):
        log_file = tmp_path / "test_audit.jsonl"
        with patch("src.mcp_security.AUDIT_LOG_PATH", log_file):
            check_tool_permission("think")

        assert not log_file.exists()

    def test_unknown_tool_writes_audit_log(self, tmp_path):
        log_file = tmp_path / "test_audit.jsonl"
        with patch("src.mcp_security.AUDIT_LOG_PATH", log_file):
            check_tool_permission("never_heard_of_this")

        assert log_file.exists()
        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert entries[0]["risk"] == "unknown"
        assert entries[0]["allowed"] is False

    def test_sensitive_params_redacted(self, tmp_path):
        log_file = tmp_path / "test_audit.jsonl"
        with patch("src.mcp_security.AUDIT_LOG_PATH", log_file):
            check_tool_permission("agent_task", {
                "task": "do something",
                "api_key": "sk-secret-12345",
            })

        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert entries[0]["params"]["api_key"] == "[REDACTED]"
        assert entries[0]["params"]["task"] == "do something"


# ── SecurityMiddleware integration ──


class TestSecurityMiddleware:
    """Integration tests for SecurityMiddleware in server.py.

    These tests verify the middleware's on_call_tool behavior by
    creating the middleware and simulating FastMCP call contexts.
    """

    @pytest.fixture
    def middleware(self):
        from server import SecurityMiddleware
        return SecurityMiddleware()

    @pytest.fixture
    def make_context(self):
        """Factory for creating mock MiddlewareContext objects."""
        from fastmcp.server.middleware import MiddlewareContext
        from mcp.types import CallToolRequestParams

        def _make(tool_name: str, arguments: dict | None = None):
            params = CallToolRequestParams(name=tool_name, arguments=arguments)
            return MiddlewareContext(
                message=params,
                method="tools/call",
            )
        return _make

    @pytest.mark.asyncio
    async def test_low_risk_passes_through(self, middleware, make_context):
        """LOW risk tool should call through to the real handler."""
        ctx = make_context("think", {"task": "hello"})
        sentinel = object()
        call_next = AsyncMock(return_value=sentinel)

        result = await middleware.on_call_tool(ctx, call_next=call_next)

        assert result is sentinel
        call_next.assert_awaited_once_with(ctx)

    @pytest.mark.asyncio
    async def test_medium_risk_passes_through(self, middleware, make_context):
        """MEDIUM risk tool should pass through (audit logged by check_tool_permission)."""
        ctx = make_context("agent_task", {"task": "test", "max_steps": 5})
        sentinel = object()
        call_next = AsyncMock(return_value=sentinel)

        result = await middleware.on_call_tool(ctx, call_next=call_next)

        assert result is sentinel
        call_next.assert_awaited_once_with(ctx)

    @pytest.mark.asyncio
    async def test_high_risk_returns_warning(self, middleware, make_context):
        """HIGH risk tool should return a warning ToolResult, not call next."""
        ctx = make_context("computer_use", {"action": "click", "target": "#btn"})
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next=call_next)

        call_next.assert_not_awaited()
        # Result should be a ToolResult with warning content
        from fastmcp.tools.tool import ToolResult
        assert isinstance(result, ToolResult)
        text = result.content[0].text
        payload = json.loads(text)
        assert payload["warning"] == "security_blocked"
        assert payload["tool"] == "computer_use"
        assert "confirmation_required" in payload["reason"]

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_warning(self, middleware, make_context):
        """UNKNOWN tool should return a deny-by-default warning."""
        ctx = make_context("evil_rce_tool", {"cmd": "rm -rf /"})
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next=call_next)

        call_next.assert_not_awaited()
        from fastmcp.tools.tool import ToolResult
        assert isinstance(result, ToolResult)
        text = result.content[0].text
        payload = json.loads(text)
        assert payload["warning"] == "security_blocked"
        assert "unknown_tool" in payload["reason"]

    @pytest.mark.asyncio
    async def test_computer_use_screenshot_passes(self, middleware, make_context):
        """computer_use with action=screenshot should pass (downgraded to MEDIUM)."""
        ctx = make_context("computer_use", {"action": "screenshot", "analyze": True})
        sentinel = object()
        call_next = AsyncMock(return_value=sentinel)

        result = await middleware.on_call_tool(ctx, call_next=call_next)

        assert result is sentinel
        call_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_high_risk_warning_payload_structure(self, middleware, make_context):
        """Verify the full structure of the warning payload."""
        ctx = make_context("computer_use", {"action": "type", "value": "password123"})
        call_next = AsyncMock()

        result = await middleware.on_call_tool(ctx, call_next=call_next)

        text = result.content[0].text
        payload = json.loads(text)
        assert set(payload.keys()) == {"warning", "tool", "reason", "action_required"}
        assert payload["warning"] == "security_blocked"
        assert payload["tool"] == "computer_use"
        assert isinstance(payload["reason"], str)
        assert isinstance(payload["action_required"], str)


# ── Registry completeness ──


class TestRegistryCompleteness:
    """Verify all tools registered in server.py are classified in the security registry."""

    def test_all_helix_tools_classified(self):
        """Every tool defined in server.py should have a security classification."""
        # These are the tool names from @mcp.tool() in server.py
        server_tools = [
            "think", "agent_task", "parallel_tasks", "see", "providers",
            "models", "config", "spawn_agent", "send_agent_input",
            "wait_agent", "list_agents", "close_agent", "fork_task",
            "computer_use", "browse", "vision_compress", "dom_compress",
            "retry_guard_check", "retry_guard_reset", "retry_guard_status",
            "agent_types", "dept_search", "dept_store",
            "evolving_memory_review", "list_learned_skills", "get_skill",
            "code_review", "x_search",
        ]
        for tool in server_tools:
            level = get_risk_level(tool)
            assert level != RiskLevel.UNKNOWN, (
                f"Tool '{tool}' from server.py is not classified in mcp_security.py"
            )

    def test_no_helix_tools_are_unknown(self):
        """Sanity check: verify UNKNOWN is only for truly unregistered tools."""
        assert get_risk_level("some_random_nonexistent") == RiskLevel.UNKNOWN
        assert get_risk_level("think") != RiskLevel.UNKNOWN
