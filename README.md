# helix-agents

**Cut your Claude Code token bill by 60-80%.** Delegate research, exploration, and routine tasks to local LLMs — keep Opus 4.6 for decisions that matter.

`helix-agents` is an MCP server that turns local models (gemma4, qwen3.5, etc.) into Claude Code subagents with full tool access, vision, memory, and Computer Use.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-10b981.svg)](https://modelcontextprotocol.io)
[![Tests](https://img.shields.io/badge/tests-280%20passing-brightgreen.svg)](#)
[![v0.9.0](https://img.shields.io/badge/version-0.9.0-7c3aed.svg)](#)

## The Problem

Claude Code on Max plan ($100-200/mo) burns through token limits fast:

- File exploration: **~2K tokens per read**
- Code search: **~5K tokens per grep**
- Agent subprocesses: **~50K tokens each**
- A single complex task can consume **500K+ tokens**

Heavy users hit their daily limit by afternoon.

## The Solution

```
Claude Code (orchestrator — Opus 4.6)
  ↓ delegates via MCP
helix-agents (local, zero API cost)
  ├── gemma4:31b — reasoning + vision + tools
  ├── qwen3.5:122b — deep analysis
  ├── deckard-uncensored — unrestricted tasks
  └── any Ollama model
```

**Opus stays in control.** It decides *what* to do. Local models do the legwork — reading files, searching code, running commands, browsing pages — at zero token cost.

## What's New in v0.9.0

| Feature | Description |
|---------|-------------|
| **Qdrant Shared Memory** | Persistent vector memory across sessions (search + add) |
| **JSON Schema Tools** | Ollama native function calling — no more ReAct JSON parsing hacks |
| **Streaming** | Token-by-token responses for real-time feedback |
| **Fork-style Context** | Inherit parent context like Claude Code's internal subagents |
| **YAML Agent Definitions** | Define custom agents in `.helix-agent/agents/*.yaml` |
| **Parallel Tool Execution** | Read-only tools run concurrently (up to 10x faster) |
| **Computer Use** | Browser automation via Playwright + desktop GUI via helix-pilot |
| **Vision** | Screenshot analysis with gemma4 / gemma3 |
| **JSONL Tracing** | Full observability — tokens, timing, tool results per step |
| **Error Recovery** | OOM auto-fallback, timeout control, partial result extraction |
| **Context Compression** | Auto-summarize history when approaching context limits |
| **gemma4:31b Default** | Best local model for reasoning + vision + function calling |

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

## Token Savings — Real Examples

| Task | Without helix-agents | With helix-agents | Savings |
|------|---------------------|-------------------|---------|
| Explore codebase (50 files) | ~100K tokens | ~2K tokens (MCP call) | **98%** |
| Search + read 10 matches | ~50K tokens | ~1K tokens | **98%** |
| Code review (500 lines) | ~30K tokens | ~1K tokens | **97%** |
| Multi-step research | ~200K tokens | ~3K tokens | **98%** |
| Vision screenshot analysis | ~10K tokens | ~1K tokens | **90%** |

*Opus only pays for the MCP tool call + reading the result. All heavy lifting is local.*

## Built-in Agent Presets

```yaml
# .helix-agent/agents/explorer.yaml (built-in)
agent_type: explorer
tools: [read_file, list_files, search_in_file]
model: auto  # gemma4:31b
max_steps: 5
```

| Preset | Purpose | Tools | Model |
|--------|---------|-------|-------|
| `explorer` | Fast read-only codebase search | read, list, search | fast (gemma4) |
| `coder` | Full implementation tasks | all tools | quality (gemma4/qwen3.5) |
| `reviewer` | Code review and analysis | read-only | quality |

Create your own in `~/.helix-agent/agents/` or `.helix-agent/agents/`.

## Tools (11 total)

**Core:**
- `think` — Reasoning with any local model
- `agent_task` — ReAct agent loop with tool access
- `fork_task` — Inherit parent context, run locally (Claude Code fork-style)
- `computer_use` — Browser/desktop automation
- `browse` — Open URL, extract text, analyze
- `see` — Vision analysis (screenshots, images)

**Memory:**
- `search_memory` — Qdrant vector search
- `add_memory` — Save to shared memory

**Management:**
- `providers` / `models` / `config` / `agent_types`

**Background agents:**
- `spawn_agent` / `send_agent_input` / `wait_agent` / `list_agents` / `close_agent`

## Providers

```text
helix-agents MCP
  ├── Ollama (local, free, default)
  │     gemma4:31b, qwen3.5:122b, gemma3:27b, etc.
  ├── Codex CLI (repo-aware coding)
  └── OpenAI-compatible (hosted models)
```

| Task | Recommended Provider |
|------|---------------------|
| Research, exploration, summarization | `ollama` (gemma4) |
| Vision, screenshot analysis | `ollama` (gemma4) |
| Repo changes, code implementation | `codex` |
| Hosted model access | `openai-compatible` |

## Architecture

```
Claude Code (Opus 4.6)
  │
  ├─ Simple tasks → Opus handles directly
  │
  └─ Delegatable tasks → helix-agents MCP
       │
       ├─ fork_task → inherit context, local LLM continues
       ├─ agent_task → ReAct loop with 11 tools
       ├─ computer_use → Playwright / helix-pilot
       ├─ browse → URL → text extraction
       └─ see → Vision analysis
            │
            ├─ Qdrant shared memory (persistent)
            ├─ JSONL tracing (observability)
            ├─ Parallel tool execution
            └─ OOM auto-fallback chain
```

## Why gemma4:31b?

| Capability | gemma4:31b | Notes |
|-----------|-----------|-------|
| Function Calling | Native support | Ollama `tools` parameter |
| Vision | Built-in | Image analysis without separate model |
| Reasoning | Strong (thinking mode) | Step-by-step with `<thinking>` |
| JSON Output | Reliable | Structured format mode |
| Speed | ~2s response | 19GB VRAM, runs on consumer GPUs |
| Cost | $0 | Fully local |

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.ai/) with gemma4:31b (or any model)
- GPU with 20GB+ VRAM (for gemma4:31b)

Optional:
- Qdrant (for shared memory)
- Playwright (for browser automation)

## Configuration

```text
config(action="show")  # View all settings
config(action="set", key="default_provider", value="ollama")
```

Key settings: `default_provider`, `ollama_host`, `codex_model`, `openai_base_url`

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md).

## Related Projects

- [helix-ai-studio](https://github.com/tsunamayo7/helix-ai-studio) — All-in-one AI chat studio with 7 providers, RAG, MCP tools, and pipeline
- [helix-pilot](https://github.com/tsunamayo7/helix-pilot) — GUI automation MCP server — AI controls Windows desktop via local Vision LLM
- [claude-code-codex-agents](https://github.com/tsunamayo7/claude-code-codex-agents) — MCP bridge to Codex CLI (GPT-5.4) with structured JSONL traces
- [helix-sandbox](https://github.com/tsunamayo7/helix-sandbox) — Secure sandbox MCP server — Docker + Windows Sandbox

## License

MIT
