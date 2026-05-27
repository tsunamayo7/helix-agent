"""helix-agent MCP server: delegate tasks across multiple LLM providers."""

from __future__ import annotations

import json as _json
import logging
import time as _time

from fastmcp import Context, FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from src.agent import AgentConfig, HelixAgent
from src.agent_loader import AgentLoader
from src.langfuse_tracing import init_tracing, trace_tool_call
from src.mcp_security import check_tool_permission
from src.token_saver import RetryGuard, TokenSaver

_security_log = logging.getLogger("helix.security")


# ---------------------------------------------------------------------------
# Security Middleware — deny-by-default policy enforcement
# ---------------------------------------------------------------------------


class SecurityMiddleware(Middleware):
    """FastMCP middleware that enforces mcp_security policy on every tool call.

    Policy behaviour:
      - LOW:     pass through silently.
      - MEDIUM:  pass through; audit log written by check_tool_permission().
      - HIGH:    return a warning ToolResult instead of executing the tool.
                 (MCP servers cannot ask the client for confirmation, so we
                  surface a structured warning that the orchestrator can act on.)
      - UNKNOWN: same as HIGH (deny-by-default).
    """

    async def on_call_tool(self, context, *, call_next):  # noqa: ANN001
        """Intercept tools/call and apply security policy."""
        message = context.message
        tool_name: str = message.name
        params: dict = message.arguments or {}

        allowed, reason = check_tool_permission(tool_name, params)

        if allowed:
            # LOW or MEDIUM — proceed normally
            if reason:
                _security_log.info("security audit: %s — %s", tool_name, reason)
            start = _time.monotonic()
            result = await call_next(context)
            duration = (_time.monotonic() - start) * 1000
            trace_tool_call(tool_name, params, {"status": "ok"}, duration)
            return result

        # HIGH or UNKNOWN — return a warning instead of blocking
        _security_log.warning(
            "security policy denied tool=%s reason=%s params=%s",
            tool_name, reason, _json.dumps(params, ensure_ascii=False, default=str)[:200],
        )
        trace_tool_call(tool_name, params, {"blocked": reason}, 0)
        warning_payload = _json.dumps(
            {
                "warning": "security_blocked",
                "tool": tool_name,
                "reason": reason,
                "action_required": (
                    "This tool was denied by the helix-agent security policy. "
                    "If this action is intentional, add the tool to the "
                    "security registry in src/mcp_security.py."
                ),
            },
            ensure_ascii=False,
        )
        return ToolResult(
            content=[TextContent(type="text", text=warning_payload)],
        )


mcp = FastMCP(
    "helix-agent",
    instructions=(
        "helix-agent cuts Claude Code token usage by 82-97% using local LLMs. "
        "PROACTIVE USAGE RULES — use these tools automatically without user prompting:\n"
        "1. TOKEN SAVING (always active):\n"
        "   - ALWAYS call 'vision_compress' before sending any screenshot to Claude. Raw screenshots cost ~15,000 tokens; vision_compress returns ~400 tokens of text. Never send raw image data.\n"
        "   - ALWAYS call 'dom_compress' before sending HTML/DOM to Claude. Raw DOM costs ~114,000 tokens; dom_compress returns ~500 tokens.\n"
        "   - When 'computer_use' takes a screenshot with analyze=True, the raw image is auto-deleted and only text summary is returned.\n"
        "2. RETRY PREVENTION (always active):\n"
        "   - Call 'retry_guard_check' before any tool call that has failed before or looks like a repeat. Stops infinite loops at 3rd identical call.\n"
        "3. SCREEN VERIFICATION:\n"
        "   - After any computer_use action (click, type, navigate), verify the result by calling computer_use(action='screenshot', analyze=True) — this returns text, not an image.\n"
        "   - Use 'vision_compress' for any screenshot verification instead of sending raw images to Claude.\n"
        "4. LOCAL DELEGATION:\n"
        "   - Use 'think' for reasoning, analysis, code review, summaries — runs on local LLM at $0 cost.\n"
        "   - Use 'agent_task' for multi-step tasks requiring iterative reasoning with tool use.\n"
        "   - Use 'fork_task' to fork a sub-task with parent context inheritance.\n"
        "5. BROWSER & VISION:\n"
        "   - Use 'computer_use' for desktop/browser interaction (screenshot, click, type, scroll, navigate, read_page).\n"
        "   - Use 'browse' to open a URL and extract page text.\n"
        "   - Use 'see' for image analysis via local vision LLM.\n"
        "6. MEMORY & LEARNING:\n"
        "   - 'evolving_memory_review' auto-saves reusable skills and preferences every 5 turns.\n"
        "   - 'list_learned_skills' and 'get_skill' retrieve saved skills.\n"
        "   - 'search_memory' / 'add_memory' for Qdrant shared memory.\n"
        "7. INFRASTRUCTURE:\n"
        "   - Use 'providers', 'models', 'config', 'agent_types' to inspect routing.\n"
        "   - Use spawn/send/wait/list/close agent tools for persistent workers.\n"
        "   - GPU auto-detected at startup — optimal model selected for 8GB to 96GB+ VRAM."
    ),
)

mcp.add_middleware(SecurityMiddleware())
init_tracing()

runtime = HelixAgent(AgentConfig())
agent_loader = AgentLoader()
agent_loader.load_user_agents()
agent_loader.load_project_agents()
token_saver = TokenSaver()
retry_guard = RetryGuard()


@mcp.tool()
async def think(
    task: str,
    context: str = "",
    model: str = "auto",
    mode: str = "quality",
    provider: str = "auto",
    cwd: str = "",
    sandbox: str = "",
    effort: str = "",
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
        effort: Codex reasoning effort (none/minimal/low/medium/high/xhigh).
                Empty → config default (high). Use xhigh for critical bugs/complex design.
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
        effort=effort,
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
    effort: str = "",
    timeout: int = 180,
    ctx: Context = None,
) -> dict:
    """Run a multi-step delegated task with provider-specific behavior.

    Ollama and OpenAI-compatible providers use the built-in ReAct loop.
    Codex runs as an autonomous implementation/review agent.

    Args:
        effort: Codex reasoning effort (none/minimal/low/medium/high/xhigh).
                Empty → config default (high). Use xhigh for critical debugging/design.
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
        effort=effort,
        timeout=timeout,
    )


@mcp.tool()
async def parallel_tasks(
    tasks: str,
    provider: str = "ollama",
    max_steps: int = 10,
    timeout: int = 180,
) -> dict:
    """Run multiple tasks in parallel on local LLMs. Auto-selects optimal model per task.

    Tasks using e2b/e4b run truly in parallel. Tasks requiring 31b run sequentially
    (31b parallel degrades quality due to GPU contention).

    Args:
        tasks: JSON array of task objects, each with:
               - "task": what to do (required)
               - "type": task type for model selection (optional, default "text")
                         One of: summarize, translate, classify, code_gen, search, review, text
               - "context": additional context (optional)
        provider: LLM provider (default "ollama")
        max_steps: Max ReAct steps per task
        timeout: Timeout per task in seconds

    Example:
        parallel_tasks(tasks='[
            {"task": "Summarize this code", "type": "summarize", "context": "...code..."},
            {"task": "Translate to English: ...", "type": "translate"},
            {"task": "Classify these items", "type": "classify"}
        ]')
    """
    import asyncio as _asyncio
    import json as _json
    from src.gpu_detect import auto_select_model

    try:
        task_list = _json.loads(tasks)
    except _json.JSONDecodeError:
        return {"error": "tasks must be a valid JSON array"}

    if not isinstance(task_list, list) or len(task_list) == 0:
        return {"error": "tasks must be a non-empty JSON array"}

    # Separate tasks by model weight: light (e2b/e4b) vs heavy (31b+)
    light_tasks = []
    heavy_tasks = []
    for i, t in enumerate(task_list):
        task_type = t.get("type", "text")
        input_len = len(t.get("task", "")) + len(t.get("context", ""))
        model = auto_select_model(task_type, input_len=input_len)
        entry = {"index": i, "model": model, **t}
        if "31b" in model or "72b" in model or "122b" in model:
            heavy_tasks.append(entry)
        else:
            light_tasks.append(entry)

    results = [None] * len(task_list)

    # Light tasks: run in parallel
    async def run_one(entry):
        r = await runtime.agent(
            task=entry["task"],
            context=entry.get("context", ""),
            model=entry["model"],
            provider=provider,
            max_steps=max_steps,
            timeout=timeout,
        )
        return entry["index"], r

    if light_tasks:
        light_results = await _asyncio.gather(
            *[run_one(e) for e in light_tasks],
            return_exceptions=True,
        )
        for item in light_results:
            if isinstance(item, Exception):
                continue
            idx, r = item
            results[idx] = r

    # Heavy tasks: run sequentially (31b parallel degrades quality)
    for entry in heavy_tasks:
        try:
            r = await runtime.agent(
                task=entry["task"],
                context=entry.get("context", ""),
                model=entry["model"],
                provider=provider,
                max_steps=max_steps,
                timeout=timeout,
            )
            results[entry["index"]] = r
        except Exception as e:
            results[entry["index"]] = {"error": str(e)}

    return {
        "results": results,
        "parallel_count": len(light_tasks),
        "sequential_count": len(heavy_tasks),
        "models_used": list({e["model"] for e in light_tasks + heavy_tasks}),
    }


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
    """View or update helix-agent configuration."""
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
    """Interact with desktop or browser via agent-browser, helix-pilot, or Playwright.

    TOKEN SAVING: When action='screenshot' and analyze=True, the raw image is
    automatically deleted from the response and only a text summary is returned
    (~400 tokens instead of ~15,000). Always use analyze=True for verification.

    RECOMMENDED FLOW: After any action (click, type, navigate), call this again
    with action='screenshot', analyze=True to verify the result without wasting tokens.

    Args:
        action: One of screenshot, click, type, scroll, read_page, navigate.
        target: CSS selector or element description (for click/type).
        value: Text to type or scroll direction (up/down).
        url: URL for navigate action.
        analyze: If True, run local vision LLM analysis on screenshot (97% token saving).
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
async def vision_compress(
    image_path: str = "",
    image_base64: str = "",
    custom_prompt: str = "",
    model: str = "",
    mode: str = "auto",
) -> dict:
    """Compress an image into a compact text summary using a local vision model — 97% token saving.

    ALWAYS use this instead of sending raw images to Claude. A raw image costs
    ~15,000 tokens; this returns ~400 tokens of structured text. The model is auto-selected
    based on your GPU (8GB: gemma4:e2b, 16GB: e4b, 24GB: 26b, 48GB+: 31b).

    Modes:
    - "auto": Auto-detect image type and apply appropriate analysis (default, recommended)
      Detects: screenshot/illustration/photo/chart/diagram/document
      Then applies type-specific detailed analysis (Japanese output)
    - "ui": Screenshots only — extracts buttons, text, layout, errors

    Use cases:
    - mode="auto": ANY image — auto-detects type, describes composition/subjects/poses
      for illustrations/photos, extracts UI elements for screenshots, reads charts/diagrams
    - mode="ui": Force screenshot-specific extraction only

    Args:
        image_path: Local path to a PNG/JPEG image (alternative to image_base64).
        image_base64: Base64-encoded image data (alternative to image_path).
        custom_prompt: Override the default extraction prompt.
        model: Override the auto-selected vision model.
        mode: "ui" for screenshots, "describe" for illustrations/photos.

    Returns:
        Dict with `summary` (structured text), `raw_response`, `tokens_saved_estimate`.
    """
    return await token_saver.vision_compress(
        image_path=image_path,
        image_base64=image_base64,
        custom_prompt=custom_prompt,
        model=model,
        mode=mode,
    )


@mcp.tool()
async def dom_compress(
    html: str = "",
    url: str = "",
    text_content: str = "",
    custom_prompt: str = "",
    model: str = "",
) -> dict:
    """Compress a DOM/HTML payload into a compact structured summary.

    Playwright MCP often sends the full DOM to Claude Code (114K+ tokens per call
    in TestCollab's benchmark vs 27K for CLI equivalents). This tool uses a local
    model to extract only the forms, links, buttons, and main content an agent
    needs to take the next action.

    Args:
        html: Raw HTML string (alternative to text_content).
        url: Page URL for context.
        text_content: Pre-extracted page text (alternative to html).
        custom_prompt: Override the default extraction prompt.
        model: Override the default text model.

    Returns:
        Dict with `summary` (structured JSON), `original_char_count`, `tokens_saved_estimate`.
    """
    return await token_saver.dom_compress(
        html=html,
        url=url,
        text_content=text_content,
        custom_prompt=custom_prompt,
        model=model,
    )


@mcp.tool()
async def retry_guard_check(
    tool_name: str,
    args: dict | None = None,
    session_id: str = "default",
) -> dict:
    """Detect when the orchestrator is stuck calling the same tool with identical args.

    Addresses a documented Claude Code pain point (anthropics/claude-code#41659):
    the agent sometimes ignores user corrections and repeats the same failing
    tool call, burning tokens until the Max plan quota is exhausted. This guard
    tracks tool calls per session and warns when a repeat-loop forms.

    Args:
        tool_name: Name of the tool being called.
        args: Arguments passed to the tool.
        session_id: Session identifier for isolating histories.

    Returns:
        Dict with `loop_detected`, `repeat_count`, `recommendation`.
    """
    return retry_guard.check(
        tool_name=tool_name,
        args=args or {},
        session_id=session_id,
    )


@mcp.tool()
async def retry_guard_reset(session_id: str = "default") -> dict:
    """Clear the retry-guard history for a session (call after resolving a loop)."""
    return retry_guard.reset(session_id=session_id)


@mcp.tool()
async def retry_guard_status(session_id: str = "default") -> dict:
    """Get retry-guard session stats: total_calls, unique_calls, max_repeats."""
    return retry_guard.status(session_id=session_id)


# ---------------------------------------------------------------------------
# Resources (App-Controlled) — MCP 3-primitive compliance
# ---------------------------------------------------------------------------


@mcp.resource("helix://status")
async def resource_status() -> str:
    """Current helix-agent runtime status: backend, provider, retry-guard stats."""
    from src.computer_use import ComputerUseHandler

    handler = ComputerUseHandler()
    backend = await handler._resolve_backend()

    guard_stats = retry_guard.status(session_id="default")
    provider_info = await runtime.providers(action="list")

    from src.gpu_detect import gpu_summary

    return __import__("json").dumps(
        {
            "version": "0.14.0",
            "browser_backend": backend,
            "retry_guard": guard_stats,
            "providers": provider_info,
            "agent_loader": agent_loader.to_dict(),
            "gpu": gpu_summary(),
        },
        ensure_ascii=False,
    )


@mcp.resource("helix://models")
async def resource_models() -> str:
    """Available LLM models across all configured providers."""
    result = await runtime.models(action="list")
    return __import__("json").dumps(result, ensure_ascii=False)


@mcp.resource("helix://config")
async def resource_config() -> str:
    """Current helix-agent configuration."""
    result = await runtime.config_action(action="show")
    return __import__("json").dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Prompts (User-Controlled) — pre-crafted workflows
# ---------------------------------------------------------------------------


@mcp.prompt()
async def retry_report() -> str:
    """Generate a retry-loop detection report for the current session."""
    stats = retry_guard.status(session_id="default")
    return (
        f"以下のretry_guard統計を分析し、問題があれば対策を日本語で提案してください:\n\n"
        f"```json\n{__import__('json').dumps(stats, indent=2)}\n```\n\n"
        f"- loop_detected が true の場合、原因と回避策を説明\n"
        f"- repeat_count が高いツールがあれば特定\n"
        f"- トークン節約の推奨アクションを提示"
    )


@mcp.prompt()
async def optimize_tokens() -> str:
    """Suggest token optimization strategies based on current usage patterns."""
    return (
        "helix-agentの以下のツールを使って、トークン最適化の提案を行ってください:\n\n"
        "1. `retry_guard_status` でリトライループの有無を確認\n"
        "2. 現在のブラウザバックエンド（agent-browser / playwright）を確認\n"
        "3. `vision_compress` / `dom_compress` の活用状況を確認\n\n"
        "改善可能なポイントを日本語で箇条書きにしてください。"
    )


@mcp.prompt()
async def setup_guide() -> str:
    """Step-by-step setup guide for helix-agent (Japanese)."""
    return (
        "helix-agentの初回セットアップガイドを実行してください:\n\n"
        "1. `providers` でプロバイダー一覧を確認\n"
        "2. `models` で利用可能モデルを確認\n"
        "3. `config` で現在の設定を表示\n"
        "4. ブラウザバックエンド（agent-browser推奨）の動作確認\n"
        "5. retry_guardのテスト呼び出し\n\n"
        "各ステップの結果を日本語で報告してください。"
    )


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


# ---------------------------------------------------------------------------
# Helix Corp — Department Memory (部門別Qdrant検索/保存)
# ---------------------------------------------------------------------------

VALID_DEPARTMENTS = ["dept_hr", "dept_research", "dept_design", "dept_build", "dept_qa", "mem0_shared"]


@mcp.tool()
async def dept_search(department: str, query: str, top_k: int = 5) -> dict:
    """Search department-specific Qdrant collection. Departments: dept_hr, dept_research, dept_design, dept_build, dept_qa, mem0_shared."""
    if department not in VALID_DEPARTMENTS:
        return {"error": f"Invalid department: {department}. Valid: {VALID_DEPARTMENTS}"}
    try:
        results = await runtime.qdrant_memory.search(query, top_k=top_k, collection=department)
        return {"department": department, "query": query, "results": results, "count": len(results)}
    except Exception as e:
        return {"error": str(e), "department": department}


@mcp.tool()
async def dept_store(department: str, text: str, category: str = "", source: str = "") -> dict:
    """Store knowledge in a department-specific Qdrant collection. Departments: dept_hr, dept_research, dept_design, dept_build, dept_qa."""
    if department not in VALID_DEPARTMENTS or department == "mem0_shared":
        return {"error": f"Cannot store to '{department}'. Use add_memory for mem0_shared."}
    try:
        metadata = {"department": department}
        if category:
            metadata["category"] = category
        if source:
            metadata["source"] = source
        point_id = await runtime.qdrant_memory.add(text, metadata=metadata, collection=department)
        return {"department": department, "point_id": point_id, "stored": True}
    except Exception as e:
        return {"error": str(e), "department": department}


# ---------------------------------------------------------------------------
# Evolving Memory — self-improving agent (hermes-agent inspired)
# ---------------------------------------------------------------------------

from src.evolving_memory import EvolvingMemory

evolving_memory = EvolvingMemory()


@mcp.tool()
async def evolving_memory_review(
    user_message: str,
    assistant_response: str,
    tool_calls: str = "[]",
) -> dict:
    """Review a conversation turn and auto-save memories/skills if valuable.

    Call this after completing a task. The review runs on a local LLM (gemma4)
    at $0 cost. Memories are saved to Qdrant, skills to ~/.helix-agent/skills/.

    Args:
        user_message: The user's message from the completed turn.
        assistant_response: The assistant's response.
        tool_calls: JSON string of tool calls made during the turn.
    """
    import json as _json
    calls = _json.loads(tool_calls) if tool_calls else []
    return await evolving_memory.on_turn_end(user_message, assistant_response, calls)


@mcp.tool()
async def list_learned_skills() -> dict:
    """List all auto-generated skills from the evolving memory system."""
    return {
        "skills": evolving_memory._skills.list_skills(),
        "stats": evolving_memory.stats(),
    }


@mcp.tool()
async def get_skill(name: str) -> dict:
    """Read the content of a learned skill."""
    content = evolving_memory._skills.get_skill(name)
    if content is None:
        return {"error": f"Skill '{name}' not found", "available": [s["name"] for s in evolving_memory._skills.list_skills()]}
    return {"name": name, "content": content}


@mcp.tool()
async def code_review(
    target: str,
    context: str = "",
    tech_context: str = "",
    web_search: bool = True,
    generate_patch: bool = False,
    skip_sonnet: bool = False,
    codex_consult: bool = True,
    codex_effort: str = "",
    max_files: int = 20,
) -> dict:
    """4-Layer Code Review Pipeline: gemma4($0) + Sonnet + Opus + Codex (Second Lead).

    Reviews code using local LLM (gemma4) with RAG/web_search,
    verifies with Sonnet, and consults Codex (Second Lead, default ON) for P1 issues.

    Args:
        target: File or directory path to review
        context: Additional context (e.g. "VTuber avatar generation tool")
        tech_context: Known libraries to avoid false positives
        web_search: Enable web_search for latest best practices
        generate_patch: Generate unified diff fix patches
        skip_sonnet: Skip Sonnet verification (gemma4 only, fastest)
        codex_consult: Consult Codex as Second Lead when P1 issues are found.
                       Default True (2026-04-11〜, Codex Pro plan).
                       Set False for token-saving $0 gemma4-only review.
        codex_effort: Codex reasoning effort (none/minimal/low/medium/high/xhigh).
                      Empty → default high. Auto-escalate to xhigh if P1>=1.
        max_files: Maximum files to review (default 20)
    """
    from src.code_review import CodeReviewPipeline

    pipeline = CodeReviewPipeline(runtime)
    result = await pipeline.run(
        target=target,
        context=context,
        tech_context=tech_context,
        web_search=web_search,
        generate_patch=generate_patch,
        skip_sonnet=skip_sonnet,
        codex_consult=codex_consult,
        codex_effort=codex_effort,
        max_files=max_files,
    )
    return result.to_dict()


@mcp.tool()
async def x_search(query: str, max_results: int = 5) -> dict:
    """Search X (Twitter) posts in real-time via X Premium+ subscription.

    Uses Hermes Agent x_search with xAI OAuth. Zero additional cost.
    Ideal for: tech trends, company research, community sentiment, OSS promotion.

    Args:
        query: Natural language search query
        max_results: Number of results (default 5)
    """
    from src.x_search import search_x as _search

    return await _search(query, max_results)


if __name__ == "__main__":
    mcp.run()
