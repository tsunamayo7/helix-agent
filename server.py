"""helix-agents MCP server: delegate tasks across multiple LLM providers."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from src.agent import AgentConfig, HelixAgent

mcp = FastMCP(
    "helix-agents",
    instructions=(
        "helix-agents delegates tasks to multiple LLM providers. "
        "Use 'think' for single-step reasoning, analysis, or code generation. "
        "Use 'agent_task' for multi-step tasks requiring iterative reasoning with tool use. "
        "Use 'see' for image analysis. "
        "Use 'providers' and 'models' to inspect routing and switch providers. "
        "Use spawn/send/wait/list/close agent tools for persistent Claude Code-style workers."
    ),
)

runtime = HelixAgent(AgentConfig())


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


if __name__ == "__main__":
    mcp.run()
