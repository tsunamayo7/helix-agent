"""YAML-based agent definition loader (Claude Code .claude/agents pattern)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentDefinition:
    """Parsed agent definition from YAML or built-in preset."""

    agent_type: str
    description: str
    when_to_use: str = ""
    tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    model: str = "auto"
    max_steps: int = 10
    system_prompt: str = ""
    source: str = "builtin"  # builtin | user | project

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "tools": self.tools,
            "disallowed_tools": self.disallowed_tools,
            "model": self.model,
            "max_steps": self.max_steps,
            "system_prompt": self.system_prompt[:200] if self.system_prompt else "",
            "source": self.source,
        }


BUILTIN_PRESETS: dict[str, AgentDefinition] = {
    "explorer": AgentDefinition(
        agent_type="explorer",
        description="Read-only codebase investigation agent",
        when_to_use="When searching files or code patterns",
        tools=["read_file", "list_files", "search_in_file", "search_memory", "calculate"],
        disallowed_tools=["run_command", "write_file", "add_memory"],
        model="auto",
        max_steps=5,
        system_prompt=(
            "You are a code exploration specialist. "
            "Never delegate understanding: read and verify results yourself. "
            "Use targeted searches, report concrete findings with file paths and line numbers. "
            "If a search returns nothing, try alternative patterns before concluding."
        ),
        source="builtin",
    ),
    "coder": AgentDefinition(
        agent_type="coder",
        description="Full-capability implementation agent",
        when_to_use="When writing code, making changes, or running commands",
        tools=[],  # empty = all tools
        disallowed_tools=[],
        model="auto",
        max_steps=20,
        system_prompt=(
            "You are an implementation-focused coding agent. "
            "Observe the current state before making changes. "
            "Make targeted, minimal edits. Verify changes compile and work. "
            "If a tool fails, try an alternative approach instead of retrying blindly. "
            "Report results concisely with file paths."
        ),
        source="builtin",
    ),
    "reviewer": AgentDefinition(
        agent_type="reviewer",
        description="Read-only code review agent",
        when_to_use="When reviewing code for issues, bugs, or improvements",
        tools=["read_file", "list_files", "search_in_file", "search_memory", "calculate"],
        disallowed_tools=["run_command", "write_file", "add_memory"],
        model="auto",
        max_steps=10,
        system_prompt=(
            "You are a code review specialist. "
            "Never delegate understanding: read every file yourself before judging. "
            "Check for: bugs, security issues, performance, error handling, type safety. "
            "Provide actionable feedback with specific file and line references. "
            "Prioritize findings by severity."
        ),
        source="builtin",
    ),
}


def _parse_yaml_safe(text: str) -> dict[str, Any]:
    """Minimal YAML-subset parser (avoids PyYAML dependency).

    Handles: scalars, lists (inline [...] and block - items), multiline | strings.
    """
    result: dict[str, Any] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        if ":" not in stripped:
            i += 1
            continue

        colon_idx = stripped.index(":")
        key = stripped[:colon_idx].strip()
        value_part = stripped[colon_idx + 1:].strip()

        if value_part == "|":
            # Multiline block scalar
            block_lines: list[str] = []
            i += 1
            indent = len(line) - len(line.lstrip()) + 2
            while i < len(lines):
                block_line = lines[i]
                if block_line.strip() and (len(block_line) - len(block_line.lstrip())) < indent:
                    break
                block_lines.append(block_line[indent:] if len(block_line) > indent else block_line.strip())
                i += 1
            result[key] = "\n".join(block_lines).rstrip()
            continue

        if value_part.startswith("[") and value_part.endswith("]"):
            # Inline list
            inner = value_part[1:-1].strip()
            if inner:
                result[key] = [item.strip().strip('"').strip("'") for item in inner.split(",")]
            else:
                result[key] = []
            i += 1
            continue

        if not value_part:
            # Could be a block list
            items: list[str] = []
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                if next_line.startswith("- "):
                    items.append(next_line[2:].strip().strip('"').strip("'"))
                    i += 1
                elif not next_line or next_line.startswith("#"):
                    i += 1
                else:
                    break
            if items:
                result[key] = items
            else:
                result[key] = ""
            continue

        # Scalar
        value_part = value_part.strip('"').strip("'")
        if value_part.isdigit():
            result[key] = int(value_part)
        elif value_part in ("true", "True"):
            result[key] = True
        elif value_part in ("false", "False"):
            result[key] = False
        else:
            result[key] = value_part

        i += 1

    return result


def _load_yaml_file(path: Path) -> AgentDefinition | None:
    """Load a single YAML agent definition file."""
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        data = _parse_yaml_safe(text)
    except Exception:
        return None

    agent_type = data.get("agent_type", path.stem)
    tools_raw = data.get("tools", [])
    disallowed_raw = data.get("disallowed_tools", [])

    return AgentDefinition(
        agent_type=str(agent_type),
        description=str(data.get("description", "")),
        when_to_use=str(data.get("when_to_use", "")),
        tools=tools_raw if isinstance(tools_raw, list) else [],
        disallowed_tools=disallowed_raw if isinstance(disallowed_raw, list) else [],
        model=str(data.get("model", "auto")),
        max_steps=int(data.get("max_steps", 10)),
        system_prompt=str(data.get("system_prompt", "")),
        source="user" if "helix-agent" in str(path.parent.parent) else "project",
    )


class AgentLoader:
    """Load agent definitions from YAML files and built-in presets."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}
        self._load_builtins()

    def _load_builtins(self) -> None:
        for name, preset in BUILTIN_PRESETS.items():
            self._agents[name] = preset

    def load_from_directory(self, directory: Path, source: str = "user") -> int:
        """Load all *.yaml files from a directory. Returns count of loaded agents."""
        if not directory.exists() or not directory.is_dir():
            return 0
        count = 0
        for yaml_file in sorted(directory.glob("*.yaml")):
            defn = _load_yaml_file(yaml_file)
            if defn:
                defn.source = source
                self._agents[defn.agent_type] = defn
                count += 1
        return count

    def load_user_agents(self) -> int:
        """Load from ~/.helix-agent/agents/."""
        home = Path.home()
        user_dir = home / ".helix-agent" / "agents"
        return self.load_from_directory(user_dir, source="user")

    def load_project_agents(self, project_root: str | Path | None = None) -> int:
        """Load from <project>/.helix-agent/agents/."""
        if project_root is None:
            project_root = Path.cwd()
        project_dir = Path(project_root) / ".helix-agent" / "agents"
        return self.load_from_directory(project_dir, source="project")

    def get(self, agent_type: str) -> AgentDefinition | None:
        return self._agents.get(agent_type)

    def list_all(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    def list_names(self) -> list[str]:
        return list(self._agents.keys())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": {name: defn.to_dict() for name, defn in self._agents.items()},
            "count": len(self._agents),
        }
