"""Agent-browser session wrapper: subprocess-based CLI integration.

Provides the same interface as PlaywrightSession, using Vercel's agent-browser
CLI (https://github.com/vercel-labs/agent-browser) for React-friendly
browser automation via native keyboard/mouse events.

Advantages over Playwright:
- 82-93% fewer tokens (accessibility tree based)
- Native keyboard events bypass React controlled component rejection
- Rust-native speed via CDP
- Daemon persists between commands for fast subsequent operations
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from typing import Any


def is_available() -> bool:
    """Check if agent-browser CLI is installed."""
    return shutil.which("agent-browser") is not None


def _resolve_cli() -> str:
    """Find the absolute path to the agent-browser executable.

    On Windows, npm installs wrap CLIs as .cmd scripts that can't be invoked
    directly via asyncio.create_subprocess_exec. We resolve to the absolute
    path and rely on shell execution for .cmd files.
    """
    path = shutil.which("agent-browser")
    if path is None:
        raise RuntimeError("agent-browser CLI not found. Install: npm install -g agent-browser")
    return path


class AgentBrowserSession:
    """Manages an agent-browser daemon session via subprocess CLI calls."""

    def __init__(self, profile: str | None = None, session: str = "helix-agent") -> None:
        self._profile = profile
        self._session = session
        self._started = False

    async def _run(self, *args: str, timeout: float = 30.0) -> dict[str, Any]:
        """Run an agent-browser command and return parsed output.

        All commands pass --json for structured output and --session for isolation.
        On Windows, the CLI is a .cmd wrapper so we use shell execution.
        """
        cli = _resolve_cli()
        cli_args = ["--session", self._session, "--json", *args]
        if sys.platform == "win32":
            # Quote args containing spaces for shell
            quoted = " ".join(f'"{a}"' if " " in a or '"' in a else a for a in cli_args)
            cmd_str = f'"{cli}" {quoted}'
            proc = await asyncio.create_subprocess_shell(
                cmd_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                cli, *cli_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": f"agent-browser timeout after {timeout}s"}

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return {"error": f"agent-browser exit {proc.returncode}: {err}"}

        out = stdout.decode("utf-8", errors="replace").strip()
        if not out:
            return {"ok": True}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"raw": out, "ok": True}

    async def navigate(self, url: str) -> str:
        """Open a URL in the browser session."""
        result = await self._run("open", url)
        if result.get("error"):
            return f"[nav failed] {result['error']}"
        return f"navigated: {url}"

    async def click(self, selector: str) -> str:
        """Click an element by CSS selector or @ref index."""
        result = await self._run("click", selector)
        if result.get("error"):
            return f"[click failed] {result['error']}"
        return f"clicked: {selector}"

    async def type_text(self, selector: str, text: str) -> str:
        """Clear a field and fill it with new text (React-friendly).

        Uses 'fill' command which clears the field first, then types with
        native keyboard events. This bypasses React controlled component
        state management that blocks setValue-based approaches.
        """
        result = await self._run("fill", selector, text)
        if result.get("error"):
            return f"[fill failed] {result['error']}"
        return f"filled: {selector} ({len(text)} chars)"

    async def keyboard_type(self, text: str) -> str:
        """Type text with native keystrokes (no selector needed, uses focused element)."""
        result = await self._run("keyboard", "type", text)
        if result.get("error"):
            return f"[keyboard failed] {result['error']}"
        return f"typed: {len(text)} chars"

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        """Scroll the page by pixels."""
        # agent-browser scroll uses y-delta
        delta = amount if direction == "down" else -amount
        result = await self._run("scroll", "0", str(delta))
        if result.get("error"):
            return f"[scroll failed] {result['error']}"
        return f"scrolled: {direction} {amount}px"

    async def screenshot(self) -> str:
        """Capture a screenshot, returns base64-encoded PNG."""
        result = await self._run("screenshot", "--base64")
        if result.get("error"):
            return f"[screenshot failed] {result['error']}"
        return result.get("base64", result.get("image", ""))

    async def read_page(self) -> str:
        """Get accessibility tree snapshot of the current page."""
        result = await self._run("snapshot", timeout=15.0)
        if result.get("error"):
            return f"[read_page failed] {result['error']}"
        return json.dumps(result, ensure_ascii=False)[:8000]

    async def press(self, key: str) -> str:
        """Press a key (Enter, Tab, Control+a, etc.)."""
        result = await self._run("press", key)
        if result.get("error"):
            return f"[press failed] {result['error']}"
        return f"pressed: {key}"

    async def close(self) -> None:
        """Close the browser session."""
        if self._started:
            await self._run("close", timeout=10.0)
            self._started = False
