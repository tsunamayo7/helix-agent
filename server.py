"""helix-agents MCP server: delegate tasks across multiple LLM providers."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from src.agent import AgentConfig, HelixAgent
from src.agent_loader import AgentLoader

mcp = FastMCP(
    "helix-agents",
    instructions=(
        "helix-agents delegates tasks to multiple LLM providers. "
        "Use 'think' for single-step reasoning, analysis, or code generation. "
        "Use 'agent_task' for multi-step tasks requiring iterative reasoning with tool use. "
        "Use 'fork_task' to fork a sub-task with parent context inheritance (Claude Code forkSubagent pattern). "
        "Use 'see' for image analysis. "
        "Use 'computer_use' for desktop/browser interaction (screenshot, click, type, scroll, navigate, read_page). "
        "Use 'browse' to open a URL and extract page text. "
        "Use 'providers' and 'models' to inspect routing and switch providers. "
        "Use 'agent_types' to list available agent definitions. "
        "Use spawn/send/wait/list/close agent tools for persistent Claude Code-style workers."
    ),
)

runtime = HelixAgent(AgentConfig())
agent_loader = AgentLoader()
agent_loader.load_user_agents()
agent_loader.load_project_agents()


@mcp.tool()
async def think(
    task: str,
    context: str = "",
    model: str = "auto",
    mode: str = "quality",
    provider: str = "auto",
    cwd: str = "",
    sandbox: str = "",
    timeout: int = 180,
) -> dict:
    """Delegate a reasoning, analysis, or code task to a selected provider.

    Args:
        task: What you want the delegated model to do.
        context: Additional context like code, logs, or text to analyze.
        model: Model name or "auto" for provider-native selection.
        mode: "quality", "fast", or "creative".
        provider: "auto", "ollama", "codex", or "openai-compatible".
        cwd: Optional working directory for Codex-backed tasks.
        sandbox: Optional Codex sandbox override.
        timeout: Timeout in seconds for Codex-backed requests.
    """
    return await runtime.think(
        task=task,
        context=context,
        model=model,
        mode=mode,
        provider=provider,
        cwd=cwd or None,
        sandbox=sandbox,
        timeout=timeout,
    )


@mcp.tool()
async def agent_task(
    task: str,
    context: str = "",
    model: str = "auto",
    mode: str = "quality",
    provider: str = "auto",
    max_steps: int = 10,
    cwd: str = "",
    sandbox: str = "",
    timeout: int = 180,
    ctx: Context = None,
) -> dict:
    """Run a multi-step delegated task with provider-specific behavior.

    Ollama and OpenAI-compatible providers use the built-in ReAct loop.
    Codex runs as an autonomous implementation/review agent.
    """

    async def on_progress(step: int, total: int, action: str) -> None:
        del action
        if ctx:
            try:
                await ctx.report_progress(step, total)
            except Exception:
                pass

    return await runtime.agent(
        task=task,
        context=context,
        model=model,
        mode=mode,
        provider=provider,
        max_steps=max_steps,
        _on_progress=on_progress,
        cwd=cwd or None,
        sandbox=sandbox,
        timeout=timeout,
    )


@mcp.tool()
async def see(
    image_path: str,
    question: str = "Describe what you see in this image in detail.",
    model: str = "auto",
    provider: str = "auto",
) -> dict:
    """Analyze an image using a provider with vision support."""
    return await runtime.see(
        image_path=image_path,
        question=question,
        model=model,
        provider=provider,
    )


@mcp.tool()
async def providers(action: str = "list", provider: str = "") -> dict:
    """Inspect or switch the default provider.

    Args:
        action: "list", "show", or "use"
        provider: Provider name for "use" or the current default provider
    """
    return await runtime.providers(action=action, provider=provider)


@mcp.tool()
async def models(action: str = "list", model_name: str = "", provider: str = "auto") -> dict:
    """Get information about models for the resolved provider."""
    return await runtime.models(action=action, model_name=model_name, provider=provider)


@mcp.tool()
async def config(action: str = "show", key: str = "", value: str = "") -> dict:
    """View or update helix-agents configuration."""
    return await runtime.config_action(action=action, key=key, value=value)


@mcp.tool()
async def spawn_agent(
    description: str,
    provider: str = "auto",
    model: str = "auto",
    mode: str = "quality",
    agent_type: str = "default",
    sandbox: str = "",
    cwd: str = "",
    initial_task: str = "",
    timeout: int = 180,
) -> dict:
    """Create a persistent background agent and optionally start its first task."""
    return runtime.spawn_background_agent(
        description=description,
        provider=provider,
        model=model,
        mode=mode,
        agent_type=agent_type,
        sandbox=sandbox,
        cwd=cwd or None,
        initial_task=initial_task,
        timeout=timeout,
    )


@mcp.tool()
async def send_agent_input(agent_id: str, message: str, timeout: int = 180) -> dict:
    """Send follow-up work to an existing background agent."""
    return runtime.send_background_agent_input(agent_id, message, timeout=timeout)


@mcp.tool()
async def wait_agent(agent_id: str, timeout: int = 30) -> dict:
    """Wait for a background agent to finish its current task."""
    return await runtime.wait_background_agent(agent_id, timeout=timeout)


@mcp.tool()
async def list_agents() -> dict:
    """List background agents and their current state."""
    return runtime.list_background_agents()


@mcp.tool()
async def close_agent(agent_id: str) -> dict:
    """Close an idle background agent."""
    return runtime.close_background_agent(agent_id)


@mcp.tool()
async def fork_task(
    task: str,
    context: str = "",
    scope: str = "",
    tools: list[str] | None = None,
    model: str = "auto",
    mode: str = "quality",
    provider: str = "auto",
    max_steps: int = 10,
    ctx: Context = None,
) -> dict:
    """Fork a sub-task with parent context inheritance (Claude Code forkSubagent pattern).

    Args:
        task: What the forked agent should do.
        context: Parent context summary to inherit.
        scope: Target files or scope for the investigation.
        tools: Allowed tool names (default: all tools).
        model: Model name or "auto".
        mode: "quality", "fast", or "creative".
        provider: Provider to use.
        max_steps: Max ReAct steps for the forked agent.
    """
    async def on_progress(step: int, total: int, action: str) -> None:
        del action
        if ctx:
            try:
                await ctx.report_progress(step, total)
            except Exception:
                pass

    return await runtime.agent(
        task=task,
        context=f"Parent context:\n{context}\n\nScope: {scope}" if context else f"Scope: {scope}" if scope else "",
        model=model,
        mode=mode,
        provider=provider,
        max_steps=max_steps,
        tools=tools,
        _on_progress=on_progress,
    )


@mcp.tool()
async def computer_use(
    action: str,
    target: str = "",
    value: str = "",
    url: str = "",
    analyze: bool = False,
    prompt: str = "Describe what you see in this image.",
) -> dict:
    """Interact with desktop or browser via helix-pilot or Playwright.

    Args:
        action: One of screenshot, click, type, scroll, read_page, navigate.
        target: CSS selector or element description (for click/type).
        value: Text to type or scroll direction (up/down).
        url: URL for navigate action.
        analyze: If True, run Ollama Vision analysis on screenshot.
        prompt: Vision analysis prompt (used when analyze=True).
    """
    from src.computer_use import ComputerUseHandler

    handler = ComputerUseHandler()
    result = await handler.execute({
        "action": action,
        "target": target,
        "value": value,
        "url": url,
        "analyze": analyze,
        "prompt": prompt,
    })
    return result


@mcp.tool()
async def browse(url: str, task: str = "") -> dict:
    """Open a URL with Playwright, extract page text, and return it.

    Args:
        url: The URL to browse.
        task: Optional description of what to do with the page content.
    """
    from src.computer_use import ComputerUseHandler

    handler = ComputerUseHandler()
    result = await handler.browse(url, task=task)
    return result


@mcp.tool()
async def agent_types(action: str = "list", agent_type: str = "") -> dict:
    """List or show available agent type definitions.

    Args:
        action: "list" or "show"
        agent_type: Agent type name for "show"
    """
    if action == "show" and agent_type:
        defn = agent_loader.get(agent_type)
        if not defn:
            return {"error": f"Unknown agent type: {agent_type}", "available": agent_loader.list_names()}
        return defn.to_dict()
    return agent_loader.to_dict()


if __name__ == "__main__":
    mcp.run()
