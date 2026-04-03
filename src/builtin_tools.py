"""Built-in tools for the ReAct agent loop."""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
from pathlib import Path

from .pathguard import PathGuard
from .qdrant_memory import QdrantMemory, QdrantMemoryConfig
from .tools import Tool, ToolRegistry

_guard = PathGuard()
_memory = QdrantMemory(QdrantMemoryConfig())

MAX_FORK_DEPTH = 2
_current_fork_depth = 0

_computer_use_handler = None


def _get_computer_use_handler():
    global _computer_use_handler
    if _computer_use_handler is None:
        from .computer_use import ComputerUseHandler
        _computer_use_handler = ComputerUseHandler()
    return _computer_use_handler


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


# --- Memory (Qdrant shared memory) ---

async def _tool_search_memory(params: str) -> str:
    """Search shared memory (Qdrant) for relevant information."""
    try:
        data = json.loads(params)
        query = data.get("query", params)
        top_k = data.get("top_k", 5)
    except (json.JSONDecodeError, AttributeError):
        query = params
        top_k = 5

    try:
        hits = await _memory.search(query, top_k=top_k)
    except Exception as e:
        return f"Memory search error: {e}"

    if not hits:
        return f"No memories found for: {query}"

    lines = []
    for i, hit in enumerate(hits, 1):
        text = hit["text"]
        score = hit["score"]
        source = hit.get("source", "")
        lines.append(f"[{i}] (score={score}) {text}")
        if source:
            lines[-1] += f" [source: {source}]"
    return "\n".join(lines)


async def _tool_add_memory(params: str) -> str:
    """Add a memory to shared Qdrant storage."""
    try:
        data = json.loads(params)
        text = data.get("text", "")
        metadata = {k: v for k, v in data.items() if k != "text"}
    except (json.JSONDecodeError, AttributeError):
        text = params
        metadata = {}

    if not text:
        return "Error: 'text' is required"

    try:
        point_id = await _memory.add(text, metadata=metadata or None)
    except Exception as e:
        return f"Memory add error: {e}"

    return f"Memory saved (id={point_id})"


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


async def _tool_fork_task(params: str) -> str:
    """Fork a sub-task with context inheritance (Claude Code forkSubagent pattern)."""
    global _current_fork_depth

    try:
        data = json.loads(params)
    except (json.JSONDecodeError, AttributeError):
        return "Error: Input must be JSON with 'task' key"

    task = data.get("task", "")
    if not task:
        return "Error: 'task' is required"

    if _current_fork_depth >= MAX_FORK_DEPTH:
        return f"Error: Max fork depth ({MAX_FORK_DEPTH}) reached. Cannot fork further."

    context = data.get("context", "")
    scope = data.get("scope", "")
    tools_filter = data.get("tools", [])

    from .ollama_client import OllamaClient
    from .react_loop import ReactLoop
    from .router import ModelRouter

    client = OllamaClient()
    router = ModelRouter(client)
    model = await router.select_for_task(task, mode="fast")
    if not model:
        return "Error: No Ollama models available for fork_task"

    registry = create_full_registry()
    if tools_filter:
        filtered = ToolRegistry()
        for name in tools_filter:
            tool = registry.get(name)
            if tool:
                filtered.register(tool)
        registry = filtered

    fork_context = ""
    if context:
        fork_context += f"Parent context:\n{context}\n\n"
    if scope:
        fork_context += f"Scope: {scope}\n\n"

    loop = ReactLoop(
        client=client,
        tools=registry,
        max_steps=10,
    )

    _current_fork_depth += 1
    try:
        result = await loop.run(
            task=task,
            model=model,
            context=fork_context,
            temperature=0.1,
        )
    finally:
        _current_fork_depth -= 1

    response_parts = []
    if scope:
        response_parts.append(f"Scope: {scope}")
    response_parts.append(f"Result: {result.answer}")

    key_files = set()
    files_changed = set()
    issues = []
    for step in result.steps:
        if step.action == "read_file":
            key_files.add(step.action_input.strip())
        elif step.action == "search_in_file":
            try:
                d = json.loads(step.action_input)
                key_files.add(d.get("path", ""))
            except (json.JSONDecodeError, AttributeError):
                pass
        elif step.action == "write_file":
            try:
                d = json.loads(step.action_input)
                files_changed.add(d.get("path", ""))
            except (json.JSONDecodeError, AttributeError):
                pass
        if step.observation.startswith("Error"):
            issues.append(step.observation[:200])

    if key_files:
        response_parts.append(f"Key files: {', '.join(f for f in key_files if f)}")
    if files_changed:
        response_parts.append(f"Files changed: {', '.join(f for f in files_changed if f)}")
    if issues:
        response_parts.append(f"Issues: {'; '.join(issues[:3])}")

    return "\n".join(response_parts)


async def _tool_computer_use(params: str) -> str:
    """Execute a computer use action (screenshot, click, type, scroll, read_page, navigate)."""
    try:
        data = json.loads(params)
    except (json.JSONDecodeError, AttributeError):
        return 'Error: Input must be JSON with "action" key'

    action = data.get("action", "")
    if not action:
        return "Error: 'action' is required"

    handler = _get_computer_use_handler()
    result = await handler.execute(data)
    if "error" in result:
        return f"Error: {result['error']}"
    return json.dumps(result, ensure_ascii=False)


async def _tool_browse(params: str) -> str:
    """Browse a URL and extract page text."""
    try:
        data = json.loads(params)
    except (json.JSONDecodeError, AttributeError):
        return 'Error: Input must be JSON with "url" key'

    url = data.get("url", "")
    if not url:
        return "Error: 'url' is required"

    task = data.get("task", "")
    handler = _get_computer_use_handler()
    result = await handler.browse(url, task=task)
    if "error" in result:
        return f"Error: {result['error']}"
    return json.dumps(result, ensure_ascii=False)


def create_full_registry() -> ToolRegistry:
    """Create a registry with all built-in tools (Phase 3)."""
    registry = ToolRegistry()

    registry.register(Tool(
        name="calculate",
        description="Evaluate a math expression (e.g., '2+3*4')",
        parameters={"expression": "math expression string"},
        handler=_tool_calculate,
        is_read_only=True,
    ))

    registry.register(Tool(
        name="read_file",
        description="Read the contents of a file",
        parameters={"path": "absolute file path"},
        handler=_tool_read_file,
        is_read_only=True,
    ))

    registry.register(Tool(
        name="write_file",
        description="Write content to a file. Input must be JSON: {\"path\": \"...\", \"content\": \"...\"}",
        parameters={"json": "{path, content}"},
        handler=_tool_write_file,
        is_read_only=False,
    ))

    registry.register(Tool(
        name="list_files",
        description="List files and directories at a path",
        parameters={"path": "absolute directory path"},
        handler=_tool_list_files,
        is_read_only=True,
    ))

    registry.register(Tool(
        name="search_in_file",
        description="Search for a regex pattern in a file. Input: JSON {\"path\": \"...\", \"pattern\": \"...\"}",
        parameters={"json": "{path, pattern}"},
        handler=_tool_search_in_file,
        is_read_only=True,
    ))

    registry.register(Tool(
        name="run_command",
        description="Run a shell command (git, python, uv, ollama, ls only)",
        parameters={"command": "shell command string"},
        handler=_tool_run_command,
        is_read_only=False,
    ))

    registry.register(Tool(
        name="search_memory",
        description="Search shared memory (Qdrant) for relevant past decisions. Input: JSON {\"query\": \"...\", \"top_k\": 5} or plain query string",
        parameters={"json_or_query": "{query, top_k?} or plain string"},
        handler=_tool_search_memory,
        is_read_only=True,
    ))

    registry.register(Tool(
        name="add_memory",
        description="Save information to shared memory (Qdrant). Input: JSON {\"text\": \"...\"}",
        parameters={"json_or_text": "{text} or plain string"},
        handler=_tool_add_memory,
        is_read_only=False,
    ))

    registry.register(Tool(
        name="fork_task",
        description="Fork a sub-task with context inheritance. Input: JSON {\"task\": \"...\", \"context\": \"parent context\", \"scope\": \"target files/range\", \"tools\": [\"read_file\", \"search_in_file\"]}",
        parameters={"json": "{task, context?, scope?, tools?}"},
        handler=_tool_fork_task,
        is_read_only=True,
    ))

    registry.register(Tool(
        name="computer_use",
        description="Interact with desktop/browser: screenshot, click, type, scroll, read_page, navigate. Input: JSON {\"action\": \"...\", \"target\": \"...\", \"value\": \"...\", \"url\": \"...\", \"analyze\": true, \"prompt\": \"...\"}",
        parameters={"json": "{action, target?, value?, url?, analyze?, prompt?}"},
        handler=_tool_computer_use,
        json_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["screenshot", "click", "type", "scroll", "read_page", "navigate"]},
                "target": {"type": "string", "description": "CSS selector or element description"},
                "value": {"type": "string", "description": "Text to type or scroll direction (up/down)"},
                "url": {"type": "string", "description": "URL for navigate action"},
                "analyze": {"type": "boolean", "description": "Run Vision analysis on screenshot"},
                "prompt": {"type": "string", "description": "Vision analysis prompt"},
            },
            "required": ["action"],
        },
        is_read_only=True,
    ))

    registry.register(Tool(
        name="browse",
        description="Open a URL and extract page text. Input: JSON {\"url\": \"...\", \"task\": \"...\"}",
        parameters={"json": "{url, task?}"},
        handler=_tool_browse,
        json_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to browse"},
                "task": {"type": "string", "description": "What to do with the page content"},
            },
            "required": ["url"],
        },
        is_read_only=True,
    ))

    return registry
