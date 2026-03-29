"""Tool registry for the ReAct agent loop."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class Tool:
    """A tool that the local LLM can invoke during an agent loop."""

    name: str
    description: str
    parameters: dict[str, str]  # param_name -> description
    handler: Callable[..., Awaitable[str]]

    def schema_for_prompt(self) -> str:
        """Format tool info for inclusion in the system prompt."""
        params = ", ".join(f"{k}: {v}" for k, v in self.parameters.items())
        return f"- {self.name}({params}): {self.description}"


class ToolRegistry:
    """Registry of tools available to the agent."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    async def execute(self, name: str, action_input: str) -> str:
        """Execute a tool by name with the given input string."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: Unknown tool '{name}'. Available: {', '.join(self._tools)}"
        try:
            result = await tool.handler(action_input)
            # Truncate very long results
            if len(result) > 4000:
                result = result[:4000] + "\n... (truncated)"
            return result
        except Exception as e:
            return f"Error executing {name}: {e}"

    def format_for_prompt(self) -> str:
        """Generate tool descriptions for the system prompt."""
        if not self._tools:
            return "No tools available."
        lines = [t.schema_for_prompt() for t in self._tools.values()]
        return "\n".join(lines)


# --- Built-in tools (minimal set for Phase 1) ---

async def _tool_calculate(expression: str) -> str:
    """Safely evaluate a math expression."""
    allowed = set("0123456789+-*/().% ")
    if not all(c in allowed for c in expression):
        return f"Error: unsafe expression. Only math operators allowed."
    try:
        result = eval(expression, {"__builtins__": {}}, {"math": math})
        return str(result)
    except Exception as e:
        return f"Error: {e}"


async def _tool_search_text(query: str) -> str:
    """Placeholder for text search (to be extended with Qdrant)."""
    return f"Search not yet implemented. Query was: {query}"


def create_default_registry() -> ToolRegistry:
    """Create a registry with the default built-in tools."""
    registry = ToolRegistry()

    registry.register(Tool(
        name="calculate",
        description="Evaluate a math expression (e.g., '2+3*4')",
        parameters={"expression": "math expression string"},
        handler=_tool_calculate,
    ))

    registry.register(Tool(
        name="search_memory",
        description="Search shared memory (Qdrant) for relevant past decisions or knowledge",
        parameters={"query": "search query string"},
        handler=_tool_search_text,
    ))

    return registry
