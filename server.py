"""helix-agent MCP server: delegate tasks to local Ollama models."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from src.agent import AgentConfig, HelixAgent

mcp = FastMCP(
    "helix-agent",
    instructions=(
        "helix-agent delegates tasks to local Ollama LLM models. "
        "Use 'think' for single-step reasoning, analysis, or code generation. "
        "Use 'agent' for multi-step tasks requiring iterative reasoning with tool use (ReAct loop). "
        "Use 'see' for image analysis and OCR. "
        "Use 'models' to check available local models. "
        "Model selection is automatic — just describe what you need."
    ),
)

agent = HelixAgent(AgentConfig())


@mcp.tool()
async def think(
    task: str,
    context: str = "",
    model: str = "auto",
    mode: str = "quality",
) -> dict:
    """Delegate a reasoning, analysis, or code task to a local Ollama model.

    Args:
        task: What you want the local LLM to do (e.g., "Summarize this log", "Find the bug")
        context: Additional context like code, logs, or text to analyze
        model: Model name or "auto" for intelligent auto-selection
        mode: "quality" (thorough), "fast" (brief), or "creative" (exploratory)
    """
    return await agent.think(task=task, context=context, model=model, mode=mode)


@mcp.tool()
async def agent_task(
    task: str,
    context: str = "",
    model: str = "auto",
    mode: str = "quality",
    max_steps: int = 10,
    ctx: Context = None,
) -> dict:
    """Run a multi-step ReAct agent loop using a local Ollama model.

    The LLM reasons step-by-step, uses tools (read_file, write_file, list_files,
    search_in_file, run_command, calculate, search_memory), observes results,
    and iterates until it reaches a final answer.

    Args:
        task: The objective for the agent (e.g., "Read pyproject.toml and summarize the project")
        context: Additional context like code, data, or text
        model: Model name or "auto" for intelligent auto-selection
        mode: "quality" (thorough) or "fast" (brief)
        max_steps: Maximum reasoning steps (default 10)
    """
    # Progress callback using MCP Context
    async def on_progress(step: int, total: int, action: str) -> None:
        if ctx:
            try:
                await ctx.report_progress(step, total)
            except Exception:
                pass  # Progress reporting is best-effort

    return await agent.agent(
        task=task, context=context, model=model, mode=mode, max_steps=max_steps,
        _on_progress=on_progress,
    )


@mcp.tool()
async def see(
    image_path: str,
    question: str = "Describe what you see in this image in detail.",
    model: str = "auto",
) -> dict:
    """Analyze an image using a local Vision LLM (OCR, description, UI analysis).

    Args:
        image_path: Absolute path to the image file
        question: What to analyze (e.g., "Extract all text", "What UI elements are visible?")
        model: Vision model name or "auto" for auto-selection
    """
    return await agent.see(image_path=image_path, question=question, model=model)


@mcp.tool()
async def models(action: str = "list", model_name: str = "") -> dict:
    """Get information about available local Ollama models.

    Args:
        action: "list" (all models + benchmark scores), "status" (connection check),
                "capabilities" (capability → model map),
                "probe" (test each model's availability and response time),
                "detailed" (metadata-enriched listing),
                "benchmark" (run benchmark with preflight VRAM check, adaptive timeout, warmup, auto-lite for large models),
                "benchmark_status" (show benchmark ranking),
                "use" (lock routing to model_name),
                "use_auto" (switch back to auto-selection)
        model_name: Target model for "benchmark" or "use" actions
    """
    return await agent.models(action=action, model_name=model_name)


@mcp.tool()
async def config(action: str = "show", key: str = "", value: str = "") -> dict:
    """View or update helix-agent configuration.

    Args:
        action: "show" (current config) or "set" (update a setting)
        key: Config key to update (e.g., "default_mode", "ollama_host")
        value: New value for the key
    """
    return await agent.config_action(action=action, key=key, value=value)


if __name__ == "__main__":
    mcp.run()
