"""helix-agent MCP server: delegate tasks to local Ollama models."""

from __future__ import annotations

from fastmcp import FastMCP

from src.agent import AgentConfig, HelixAgent

mcp = FastMCP(
    "helix-agent",
    instructions=(
        "helix-agent delegates tasks to local Ollama LLM models. "
        "Use 'think' for reasoning, analysis, code generation, and summarization. "
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
async def models(action: str = "list") -> dict:
    """Get information about available local Ollama models.

    Args:
        action: "list" (all models + capabilities), "status" (connection check), "capabilities" (capability → model map)
    """
    return await agent.models(action=action)


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
