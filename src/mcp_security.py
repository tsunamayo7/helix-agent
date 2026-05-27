"""MCP Tool Security Policy — deny-by-default access control for MCP tools.

Design principle: Every MCP tool invocation is an attack surface.
Default policy is DENY; tools must be explicitly classified to be allowed.

This module provides:
  1. Risk-level classification for all known MCP tools
  2. Permission checking with parameter-aware rules
  3. JSONL audit logging for MEDIUM and HIGH risk operations

Usage (integrated via SecurityMiddleware in server.py):
    from src.mcp_security import check_tool_permission

    allowed, reason = check_tool_permission("computer_use", {"action": "click"})
    if not allowed:
        return {"error": reason}

Architecture note:
    This module is a standalone policy engine that does NOT import from
    server.py. Integration is done via FastMCP Middleware in server.py
    (SecurityMiddleware.on_call_tool) which calls check_tool_permission()
    on every tools/call request.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Risk levels
# ---------------------------------------------------------------------------

class RiskLevel(Enum):
    """Tool risk classification. Higher = more dangerous."""
    LOW = "low"          # Auto-allow, no logging
    MEDIUM = "medium"    # Auto-allow, audit-logged
    HIGH = "high"        # Blocked by default, requires explicit approval
    UNKNOWN = "unknown"  # Deny-by-default for unclassified tools


# ---------------------------------------------------------------------------
# Audit log path
# ---------------------------------------------------------------------------

AUDIT_LOG_PATH = Path(os.environ.get(
    "HELIX_MCP_AUDIT_LOG",
    str(Path.home() / ".claude" / "mcp_audit.jsonl"),
))


# ---------------------------------------------------------------------------
# Tool risk classification registry
# ---------------------------------------------------------------------------

# helix-agent internal tools (from server.py @mcp.tool registrations)
_HELIX_TOOLS: dict[str, RiskLevel] = {
    # --- LOW: read-only, inspection, no side effects ---
    "think":                  RiskLevel.LOW,
    "see":                    RiskLevel.LOW,
    "providers":              RiskLevel.LOW,
    "models":                 RiskLevel.LOW,
    "config":                 RiskLevel.LOW,
    "agent_types":            RiskLevel.LOW,
    "list_agents":            RiskLevel.LOW,
    "list_learned_skills":    RiskLevel.LOW,
    "get_skill":              RiskLevel.LOW,
    "retry_guard_check":      RiskLevel.LOW,
    "retry_guard_status":     RiskLevel.LOW,
    "vision_compress":        RiskLevel.LOW,
    "dom_compress":           RiskLevel.LOW,
    "browse":                 RiskLevel.LOW,
    "dept_search":            RiskLevel.LOW,
    "x_search":               RiskLevel.LOW,

    # --- MEDIUM: write/mutate, but within controlled scope ---
    "agent_task":             RiskLevel.MEDIUM,
    "parallel_tasks":         RiskLevel.MEDIUM,
    "fork_task":              RiskLevel.MEDIUM,
    "spawn_agent":            RiskLevel.MEDIUM,
    "send_agent_input":       RiskLevel.MEDIUM,
    "wait_agent":             RiskLevel.MEDIUM,
    "close_agent":            RiskLevel.MEDIUM,
    "retry_guard_reset":      RiskLevel.MEDIUM,
    "evolving_memory_review": RiskLevel.MEDIUM,
    "dept_store":             RiskLevel.MEDIUM,
    "code_review":            RiskLevel.MEDIUM,

    # --- HIGH: direct system interaction, browser control ---
    "computer_use":           RiskLevel.HIGH,
}

# chrome-devtools MCP tools (all are HIGH risk: direct browser manipulation)
_CHROME_DEVTOOLS_TOOLS: dict[str, RiskLevel] = {
    "click":                  RiskLevel.HIGH,
    "close_page":             RiskLevel.HIGH,
    "drag":                   RiskLevel.HIGH,
    "emulate":                RiskLevel.HIGH,
    "evaluate_script":        RiskLevel.HIGH,
    "fill":                   RiskLevel.HIGH,
    "fill_form":              RiskLevel.HIGH,
    "get_console_message":    RiskLevel.MEDIUM,
    "get_network_request":    RiskLevel.MEDIUM,
    "handle_dialog":          RiskLevel.HIGH,
    "hover":                  RiskLevel.HIGH,
    "lighthouse_audit":       RiskLevel.LOW,
    "list_console_messages":  RiskLevel.LOW,
    "list_network_requests":  RiskLevel.LOW,
    "list_pages":             RiskLevel.LOW,
    "navigate_page":          RiskLevel.HIGH,
    "new_page":               RiskLevel.HIGH,
    "press_key":              RiskLevel.HIGH,
    "resize_page":            RiskLevel.MEDIUM,
    "select_page":            RiskLevel.MEDIUM,
    "take_heapsnapshot":      RiskLevel.MEDIUM,
    "take_screenshot":        RiskLevel.LOW,
    "take_snapshot":          RiskLevel.LOW,
    "type_text":              RiskLevel.HIGH,
    "upload_file":            RiskLevel.HIGH,
    "wait_for":               RiskLevel.LOW,
    "performance_start_trace": RiskLevel.MEDIUM,
    "performance_stop_trace":  RiskLevel.MEDIUM,
    "performance_analyze_insight": RiskLevel.LOW,
}

# Qdrant memory operations (from builtin_tools.py)
_QDRANT_TOOLS: dict[str, RiskLevel] = {
    "search_memory":          RiskLevel.LOW,
    "add_memory":             RiskLevel.MEDIUM,
    "delete_memory":          RiskLevel.HIGH,
}

# File system operations (from builtin_tools.py ReAct tools)
_FILE_TOOLS: dict[str, RiskLevel] = {
    "read_file":              RiskLevel.LOW,
    "list_directory":         RiskLevel.LOW,
    "write_file":             RiskLevel.MEDIUM,
    "delete_file":            RiskLevel.HIGH,
}

# Shell / process execution
_SHELL_TOOLS: dict[str, RiskLevel] = {
    "shell":                  RiskLevel.HIGH,
    "bash":                   RiskLevel.HIGH,
    "run_command":            RiskLevel.HIGH,
    "execute":                RiskLevel.HIGH,
}


def _build_registry() -> dict[str, RiskLevel]:
    """Merge all tool classifications into a single lookup.

    For tools with namespace prefixes (e.g. "mcp__chrome-devtools__click"),
    we store both the short name and common prefixed variants.
    """
    registry: dict[str, RiskLevel] = {}

    # helix-agent tools (no prefix needed, they are native)
    registry.update(_HELIX_TOOLS)

    # chrome-devtools (store with and without prefix)
    for name, level in _CHROME_DEVTOOLS_TOOLS.items():
        registry[name] = level
        registry[f"chrome-devtools__{name}"] = level
        registry[f"mcp__chrome-devtools__{name}"] = level

    # Qdrant
    for name, level in _QDRANT_TOOLS.items():
        registry[name] = level

    # File tools
    for name, level in _FILE_TOOLS.items():
        registry[name] = level

    # Shell tools
    for name, level in _SHELL_TOOLS.items():
        registry[name] = level

    return registry

_TOOL_REGISTRY: dict[str, RiskLevel] = _build_registry()


# ---------------------------------------------------------------------------
# Parameter-aware risk escalation rules
# ---------------------------------------------------------------------------

def _check_param_escalation(tool_name: str, params: dict[str, Any]) -> RiskLevel | None:
    """Check if specific parameter values escalate the risk level.

    Returns the escalated RiskLevel, or None if no escalation applies.
    """
    # computer_use: analyzed screenshot is MEDIUM, raw screenshot stays HIGH
    if tool_name == "computer_use":
        action = params.get("action", "")
        if action == "screenshot" and params.get("analyze", False):
            return RiskLevel.MEDIUM
        return None

    # write_file: writing to sensitive paths escalates to HIGH
    if tool_name == "write_file":
        path = str(params.get("path", ""))
        sensitive_patterns = [
            ".env", ".ssh", ".gnupg", "credentials",
            "id_rsa", "id_ed25519", ".npmrc", ".pypirc",
            "/etc/", "/usr/", "/bin/", "/sbin/",
        ]
        for pattern in sensitive_patterns:
            if pattern in path.lower():
                return RiskLevel.HIGH
        return None

    # agent_task / parallel_tasks: large step counts are higher risk
    if tool_name in ("agent_task", "parallel_tasks"):
        max_steps = params.get("max_steps", 10)
        if isinstance(max_steps, int) and max_steps > 20:
            return RiskLevel.HIGH
        return None

    # navigate_page: non-localhost URLs are higher risk
    if tool_name in ("navigate_page", "mcp__chrome-devtools__navigate_page"):
        url = str(params.get("url", ""))
        if url and not any(safe in url for safe in ["localhost", "127.0.0.1", "file://"]):
            # External navigation is already HIGH, but log the URL
            return RiskLevel.HIGH
        return None

    return None


# ---------------------------------------------------------------------------
# Deny reasons for HIGH-risk tools
# ---------------------------------------------------------------------------

_HIGH_RISK_REASONS: dict[str, str] = {
    # helix-agent
    "computer_use":       "desktop/browser direct manipulation",
    # chrome-devtools
    "click":              "browser DOM click",
    "close_page":         "browser tab close",
    "drag":               "browser drag operation",
    "emulate":            "device emulation change",
    "evaluate_script":    "arbitrary JavaScript execution",
    "fill":               "form field injection",
    "fill_form":          "multi-field form injection",
    "handle_dialog":      "browser dialog interaction",
    "hover":              "browser hover (can trigger JS)",
    "navigate_page":      "browser navigation to URL",
    "new_page":           "new browser tab creation",
    "press_key":          "keyboard input injection",
    "type_text":          "text input injection",
    "upload_file":        "file upload to browser",
    # file system
    "delete_file":        "file deletion",
    # shell
    "shell":              "shell command execution",
    "bash":               "bash command execution",
    "run_command":        "command execution",
    "execute":            "command execution",
    # qdrant
    "delete_memory":      "Qdrant memory deletion",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_risk_level(tool_name: str) -> RiskLevel:
    """Look up the risk level for a tool.

    Returns UNKNOWN for unclassified tools (deny-by-default).
    """
    # Normalize: strip common MCP prefixes for lookup
    normalized = tool_name
    for prefix in ("mcp__chrome-devtools__", "chrome-devtools__"):
        if tool_name.startswith(prefix):
            normalized = tool_name[len(prefix):]
            break

    # Try exact match first, then normalized
    if tool_name in _TOOL_REGISTRY:
        return _TOOL_REGISTRY[tool_name]
    if normalized in _TOOL_REGISTRY:
        return _TOOL_REGISTRY[normalized]

    return RiskLevel.UNKNOWN


def check_tool_permission(
    tool_name: str,
    params: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Check whether a tool invocation is permitted.

    Returns:
        (allowed, reason) tuple:
        - LOW:     (True, "")
        - MEDIUM:  (True, "audit: {detail}") -- also writes audit log
        - HIGH:    (False, "confirmation_required: {detail}")
        - UNKNOWN: (False, "unknown_tool: {tool_name}")
    """
    params = params or {}

    # 1. Base risk level from registry
    base_level = get_risk_level(tool_name)

    # 2. Parameter-aware escalation
    escalated = _check_param_escalation(tool_name, params)
    effective_level = escalated if escalated is not None else base_level

    # 3. Decision
    if effective_level == RiskLevel.LOW:
        return True, ""

    if effective_level == RiskLevel.MEDIUM:
        detail = f"audit: {tool_name}"
        _write_audit_log(tool_name, "medium", allowed=True, reason=detail, params=params)
        return True, detail

    if effective_level == RiskLevel.HIGH:
        reason_text = _HIGH_RISK_REASONS.get(tool_name, "high-risk operation")
        detail = f"confirmation_required: {reason_text}"
        _write_audit_log(tool_name, "high", allowed=False, reason=detail, params=params)
        return False, detail

    # UNKNOWN: deny by default
    detail = f"unknown_tool: {tool_name} -- not in security registry, denied by default"
    _write_audit_log(tool_name, "unknown", allowed=False, reason=detail, params=params)
    return False, detail


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def _write_audit_log(
    tool_name: str,
    risk: str,
    allowed: bool,
    reason: str,
    params: dict[str, Any] | None = None,
) -> None:
    """Append audit entry to the JSONL log file.

    Sensitive parameter values (passwords, tokens, keys) are redacted.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "risk": risk,
        "allowed": allowed,
        "reason": reason,
    }

    # Include sanitized params for forensics (redact sensitive values)
    if params:
        safe_params = _redact_sensitive(params)
        if safe_params:
            entry["params"] = safe_params

    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # Audit logging must not break tool execution
        pass


def _redact_sensitive(params: dict[str, Any]) -> dict[str, Any]:
    """Redact values for keys that look like secrets."""
    redacted = {}
    sensitive_keys = {
        "password", "secret", "token", "api_key", "apikey",
        "access_key", "private_key", "credential", "auth",
    }
    for key, value in params.items():
        if any(s in key.lower() for s in sensitive_keys):
            redacted[key] = "[REDACTED]"
        elif isinstance(value, str) and len(value) > 500:
            redacted[key] = value[:100] + f"...[truncated, {len(value)} chars]"
        else:
            redacted[key] = value
    return redacted


# ---------------------------------------------------------------------------
# Utility: list all classified tools
# ---------------------------------------------------------------------------

def list_tool_classifications() -> dict[str, dict[str, Any]]:
    """Return all tool classifications grouped by risk level.

    Useful for documentation and security reviews.
    """
    result: dict[str, list[str]] = {
        "high": [],
        "medium": [],
        "low": [],
    }
    seen: set[str] = set()
    for name, level in sorted(_TOOL_REGISTRY.items()):
        # Skip prefixed duplicates for cleaner output
        if "__" in name:
            continue
        if name in seen:
            continue
        seen.add(name)
        result[level.value].append(name)

    return {
        "high": {"tools": sorted(result["high"]), "policy": "blocked, requires confirmation"},
        "medium": {"tools": sorted(result["medium"]), "policy": "allowed, audit-logged"},
        "low": {"tools": sorted(result["low"]), "policy": "allowed, no logging"},
    }


# ---------------------------------------------------------------------------
# Convenience: summary for server.py integration
# ---------------------------------------------------------------------------

def security_summary() -> str:
    """Human-readable summary of the security policy."""
    classifications = list_tool_classifications()
    lines = [
        "MCP Security Policy: deny-by-default",
        "",
        f"HIGH risk ({len(classifications['high']['tools'])} tools) -- {classifications['high']['policy']}:",
    ]
    for t in classifications["high"]["tools"]:
        reason = _HIGH_RISK_REASONS.get(t, "")
        lines.append(f"  - {t}" + (f" ({reason})" if reason else ""))

    lines.append("")
    lines.append(f"MEDIUM risk ({len(classifications['medium']['tools'])} tools) -- {classifications['medium']['policy']}:")
    for t in classifications["medium"]["tools"]:
        lines.append(f"  - {t}")

    lines.append("")
    lines.append(f"LOW risk ({len(classifications['low']['tools'])} tools) -- {classifications['low']['policy']}:")
    for t in classifications["low"]["tools"]:
        lines.append(f"  - {t}")

    lines.append("")
    lines.append("Unclassified tools: DENIED by default (UNKNOWN risk)")
    lines.append(f"Audit log: {AUDIT_LOG_PATH}")
    return "\n".join(lines)
