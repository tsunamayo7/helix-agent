# helix-agents

**Stop the Token Drain.** Survive Claude Code's Computer Use token costs by offloading screenshots, DOM, and retry loops to local LLMs — keep Opus 4.6 for decisions that actually matter.

`helix-agents` is an MCP server that turns local models (gemma4, qwen3.5, etc.) into Claude Code subagents with full tool access, vision, memory, and Computer Use.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-10b981.svg)](https://modelcontextprotocol.io)
[![Tests](https://img.shields.io/badge/tests-308%20passing-brightgreen.svg)](#)
[![v0.10.0](https://img.shields.io/badge/version-0.10.0-7c3aed.svg)](#)

## The Token Drain Crisis

Anthropic [officially admitted](https://www.theregister.com/2026/03/31/anthropic_claude_code_limits/) in March 2026 that Claude Code users are "hitting usage limits way faster than expected." Max plan subscribers are [burning through 5-hour quotas in 19 minutes](https://www.macrumors.com/2026/03/26/claude-code-users-rapid-rate-limit-drain-bug/).

**The real culprit is Computer Use**:

| Operation | Token Cost | Source |
|-----------|-----------:|--------|
| One screenshot passed to Claude | **5,000 - 15,000** | [TestCollab](https://testcollab.com/blog/playwright-cli) |
| Playwright MCP per-call DOM | **~114,000** | [TestCollab](https://testcollab.com/blog/playwright-cli) |
| MCP tool schemas at session start | **~66,000** | [Paddo.dev](https://paddo.dev/blog/claude-code-hidden-mcp-flag/) |
| Retry loop burning identical calls | **unbounded** | [claude-code#41659](https://github.com/anthropics/claude-code/issues/41659) |

A single "analyze this page" task can cost **250K-500K tokens** before Claude even makes a decision.

## The Solution: Offload to Local

```
Claude Code (Opus 4.6 — decides WHAT to do)
  ↓ MCP call (~500 tokens in, ~500 tokens out)
helix-agents (local, zero API cost)
  ├── vision_compress    screenshot → ~400-token JSON summary
  ├── dom_compress       full DOM → ~500-token structured extract
  ├── retry_guard        detect repeat-loops before they drain quota
  ├── see / browse       vision + page text via gemma4:31b
  ├── agent_task         ReAct loop with 11 tools
  └── fork_task          inherit context, local LLM continues
```

**Opus stays in control.** It decides *what* to do. gemma4 does the clicking, reading, and pixel-counting — for $0.

## What's New in v0.10.0 — Token Drain Crisis Response

| Tool | What It Does | Typical Saving |
|------|--------------|---------------:|
| `vision_compress` | Screenshot → structured summary (page_type, buttons, state flags) | **~94%** (15K → 400 tokens) |
| `dom_compress` | HTML/DOM → structured summary (forms, links, next-action candidates) | **~96%** (114K → 500 tokens) |
| `retry_guard_check` | Detect repeat-loop tool calls, recommend escalation | stops quota hemorrhage |
| `retry_guard_status` | Session stats: total/unique/max_repeats | observability |
| `retry_guard_reset` | Clear loop-guard history after resolution | cleanup |

Also still there: Qdrant memory, JSON Schema tools, streaming, fork-style context, YAML agents, parallel tool execution, JSONL tracing, OOM auto-fallback, context compression.

## Quick Start

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent
uv sync
ollama pull gemma4:31b
uv run python server.py
```

Add to Claude Code (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "helix-agents": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/helix-agent", "python", "server.py"]
    }
  }
}
```

That's it. Claude Code now has a local subagent runtime.

## Real Token Savings

| Task | Without helix-agents | With helix-agents | Savings |
|------|---------------------:|------------------:|--------:|
| Analyze one screenshot | 8K-15K tokens | ~400 tokens | **94-97%** |
| Read one web page via MCP | ~114K tokens | ~500 tokens | **99%** |
| Explore codebase (50 files) | ~100K tokens | ~2K tokens | **98%** |
| Search + read 10 matches | ~50K tokens | ~1K tokens | **98%** |
| Code review (500 lines) | ~30K tokens | ~1K tokens | **97%** |
| Multi-step research | ~200K tokens | ~3K tokens | **98%** |

*Claude only pays for the MCP tool call + reading the compact result. All heavy lifting is local.*

## The 18 MCP Tools

**Token savers (new in v0.10.0):**
- `vision_compress` — screenshot → structured summary via gemma4 vision
- `dom_compress` — HTML → structured summary via gemma4
- `retry_guard_check` / `retry_guard_status` / `retry_guard_reset` — loop detection

**Core delegation:**
- `think` — single-step reasoning with any local model
- `agent_task` — ReAct agent loop with tool access
- `fork_task` — inherit parent context, run locally
- `see` — vision analysis (screenshots, images)
- `computer_use` — browser/desktop automation
- `browse` — open URL, extract text, analyze

**Memory (Qdrant):**
- `search_memory` — vector search
- `add_memory` — save to shared memory

**Management:**
- `providers` / `models` / `config` / `agent_types`

**Background agents:**
- `spawn_agent` / `send_agent_input` / `wait_agent` / `list_agents` / `close_agent`

## Security: Defense Against Prompt Injection

Claude Code has documented prompt-injection vulnerabilities
([CVE-2025-59536](https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/))
where malicious content in project files can exfiltrate API tokens. helix-agents
provides **PathGuard** — path allowlists and sanitization — so delegated tools
cannot access sensitive locations outside the workspace.

See [SECURITY.md](SECURITY.md).

## Why gemma4:31b?

| Capability | gemma4:31b | Notes |
|-----------|-----------|-------|
| Function Calling | Native support | Ollama `tools` parameter |
| Vision | Built-in | Image analysis without separate model |
| Reasoning | Strong (thinking mode) | Step-by-step with `<thinking>` |
| JSON Output | Reliable | Structured format mode |
| Speed | ~2s response | 19GB VRAM, runs on consumer GPUs |
| Cost | $0 | Fully local |

## Architecture

```
Claude Code (Opus 4.6)
  │
  ├─ Simple tasks → Opus handles directly
  │
  └─ Computer Use / research / exploration → helix-agents MCP
       │
       ├─ vision_compress → gemma4 vision → structured summary
       ├─ dom_compress    → gemma4 text → structured extract
       ├─ retry_guard     → detect repeat-loop quota drain
       ├─ agent_task      → ReAct loop with 11 tools
       ├─ fork_task       → inherit context, local continues
       ├─ computer_use    → Playwright / helix-pilot
       ├─ browse          → URL → text extraction
       └─ see             → vision analysis
            │
            ├─ Qdrant shared memory (persistent)
            ├─ JSONL tracing (observability)
            ├─ Parallel tool execution
            ├─ PathGuard (path safety)
            └─ OOM auto-fallback chain
```

## Not a Claude Code Wrapper

helix-agents is an **MCP server that Claude Code connects to** — it does not
wrap, proxy, or re-host Claude Code or the Anthropic API. This keeps it fully
compliant with Anthropic's Terms of Service while letting you route
screenshot-analysis, DOM-extraction, and retry-loop-prone workflows through
local models.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.ai/) with gemma4:31b (or any model)
- GPU with 20GB+ VRAM (for gemma4:31b)

Optional:
- Qdrant (for shared memory)
- Playwright (for browser automation)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Related Projects

- [helix-ai-studio](https://github.com/tsunamayo7/helix-ai-studio) — All-in-one AI chat studio with 7 providers, RAG, MCP tools, and pipeline
- [helix-pilot](https://github.com/tsunamayo7/helix-pilot) — GUI automation MCP server — AI controls Windows desktop via local Vision LLM
- [claude-code-codex-agents](https://github.com/tsunamayo7/claude-code-codex-agents) — MCP bridge to Codex CLI (GPT-5.4) with structured JSONL traces
- [helix-sandbox](https://github.com/tsunamayo7/helix-sandbox) — Secure sandbox MCP server — Docker + Windows Sandbox

## License

MIT
