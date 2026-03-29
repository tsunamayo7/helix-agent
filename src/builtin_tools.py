"""Built-in tools for the ReAct agent loop."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from .pathguard import PathGuard
from .tools import Tool, ToolRegistry

_guard = PathGuard()


# --- File tools ---

async def _tool_read_file(path: str) -> str:
    """Read a file's contents."""
    try:
        resolved = _guard.validate(path)
    except PermissionError as e:
        return f"Error: {e}"
    if not resolved.exists():
        return f"Error: File not found: {path}"
    if not resolved.is_file():
        return f"Error: Not a file: {path}"
    try:
        content = resolved.read_text(encoding="utf-8")
        if len(content) > 8000:
            content = content[:8000] + f"\n... (truncated, total {len(content)} chars)"
        return content
    except Exception as e:
        return f"Error reading file: {e}"


async def _tool_write_file(params: str) -> str:
    """Write content to a file. Input: JSON with 'path' and 'content' keys."""
    try:
        data = json.loads(params)
        path = data.get("path", "")
        content = data.get("content", "")
    except (json.JSONDecodeError, AttributeError):
        return "Error: Input must be JSON with 'path' and 'content' keys"

    if not path or not content:
        return "Error: Both 'path' and 'content' are required"

    try:
        resolved = _guard.validate(path)
    except PermissionError as e:
        return f"Error: {e}"
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {resolved}"
    except Exception as e:
        return f"Error writing file: {e}"


async def _tool_list_files(path: str) -> str:
    """List files in a directory."""
    try:
        resolved = _guard.validate(path)
    except PermissionError as e:
        return f"Error: {e}"
    if not resolved.exists():
        return f"Error: Directory not found: {path}"
    if not resolved.is_dir():
        return f"Error: Not a directory: {path}"
    try:
        entries = sorted(resolved.iterdir())
        lines = []
        for entry in entries[:50]:
            prefix = "[DIR] " if entry.is_dir() else "      "
            lines.append(f"{prefix}{entry.name}")
        result = "\n".join(lines)
        if len(entries) > 50:
            result += f"\n... ({len(entries)} total entries)"
        return result
    except Exception as e:
        return f"Error listing directory: {e}"


async def _tool_search_in_file(params: str) -> str:
    """Search for a pattern in a file. Input: JSON with 'path' and 'pattern' keys."""
    try:
        data = json.loads(params)
        path = data.get("path", "")
        pattern = data.get("pattern", "")
    except (json.JSONDecodeError, AttributeError):
        return "Error: Input must be JSON with 'path' and 'pattern' keys"

    if not path or not pattern:
        return "Error: Both 'path' and 'pattern' are required"

    try:
        resolved = _guard.validate(path)
    except PermissionError as e:
        return f"Error: {e}"
    if not resolved.exists():
        return f"Error: File not found: {path}"
    try:
        import re
        content = resolved.read_text(encoding="utf-8")
        lines = content.split("\n")
        matches = []
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                matches.append(f"L{i}: {line.rstrip()}")
        if not matches:
            return f"No matches for '{pattern}' in {resolved.name}"
        result = "\n".join(matches[:30])
        if len(matches) > 30:
            result += f"\n... ({len(matches)} total matches)"
        return result
    except Exception as e:
        return f"Error searching: {e}"


# --- Calculation ---

async def _tool_calculate(expression: str) -> str:
    """Safely evaluate a math expression."""
    allowed = set("0123456789+-*/().% ")
    if not all(c in allowed for c in expression):
        return "Error: unsafe expression. Only math operators allowed."
    try:
        result = eval(expression, {"__builtins__": {}}, {"math": math})
        return str(result)
    except Exception as e:
        return f"Error: {e}"


# --- Memory (placeholder for Qdrant) ---

async def _tool_search_memory(query: str) -> str:
    """Search shared memory (Qdrant) for relevant information."""
    return f"Memory search not yet connected. Query: {query}"


# --- Shell (restricted) ---

ALLOWED_COMMANDS = {"git", "python", "uv", "ollama", "pip", "ls", "dir", "cat", "head", "wc"}


async def _tool_run_command(command: str) -> str:
    """Run a shell command (restricted to safe commands)."""
    parts = command.strip().split()
    if not parts:
        return "Error: empty command"

    cmd_name = Path(parts[0]).name.lower()
    if cmd_name not in ALLOWED_COMMANDS:
        return f"Error: Command '{cmd_name}' not in allowlist. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
            cwd="C:/Development",
        )
        output = result.stdout
        if result.returncode != 0:
            output += f"\nSTDERR: {result.stderr}"
        if len(output) > 4000:
            output = output[:4000] + "\n... (truncated)"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Command timed out (15s limit)"
    except Exception as e:
        return f"Error: {e}"


def create_full_registry() -> ToolRegistry:
    """Create a registry with all built-in tools (Phase 2)."""
    registry = ToolRegistry()

    registry.register(Tool(
        name="calculate",
        description="Evaluate a math expression (e.g., '2+3*4')",
        parameters={"expression": "math expression string"},
        handler=_tool_calculate,
    ))

    registry.register(Tool(
        name="read_file",
        description="Read the contents of a file",
        parameters={"path": "absolute file path"},
        handler=_tool_read_file,
    ))

    registry.register(Tool(
        name="write_file",
        description="Write content to a file. Input must be JSON: {\"path\": \"...\", \"content\": \"...\"}",
        parameters={"json": "{path, content}"},
        handler=_tool_write_file,
    ))

    registry.register(Tool(
        name="list_files",
        description="List files and directories at a path",
        parameters={"path": "absolute directory path"},
        handler=_tool_list_files,
    ))

    registry.register(Tool(
        name="search_in_file",
        description="Search for a regex pattern in a file. Input: JSON {\"path\": \"...\", \"pattern\": \"...\"}",
        parameters={"json": "{path, pattern}"},
        handler=_tool_search_in_file,
    ))

    registry.register(Tool(
        name="run_command",
        description="Run a shell command (git, python, uv, ollama, ls only)",
        parameters={"command": "shell command string"},
        handler=_tool_run_command,
    ))

    registry.register(Tool(
        name="search_memory",
        description="Search shared memory (Qdrant) for relevant past decisions",
        parameters={"query": "search query string"},
        handler=_tool_search_memory,
    ))

    return registry
