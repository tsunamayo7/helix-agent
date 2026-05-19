# helix-agent

**Cut Claude Code token usage 82-97% with local LLMs.**

[![CI](https://github.com/tsunamayo7/helix-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/tsunamayo7/helix-agent/actions/workflows/ci.yml)
[![CodeQL](https://github.com/tsunamayo7/helix-agent/actions/workflows/codeql.yml/badge.svg)](https://github.com/tsunamayo7/helix-agent/actions/workflows/codeql.yml)
[![Tests](https://img.shields.io/badge/tests-367%20passing-brightgreen.svg)](#)
[![v0.15.1](https://img.shields.io/badge/version-0.15.1-7c3aed.svg)](#)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-10b981.svg)](https://modelcontextprotocol.io)
[![Works on 8GB VRAM](https://img.shields.io/badge/GPU-8GB%20VRAM%20OK-green.svg)](#gpu-auto-detection)

## The Problem

Claude Code's Max plan 5-hour quota [can vanish in 19 minutes](https://www.macrumors.com/2026/03/26/claude-code-users-rapid-rate-limit-drain-bug/). Raw screenshots cost ~15,000 tokens. DOM snapshots cost ~114,000. Retry loops burn tokens infinitely with [no built-in detection](https://github.com/anthropics/claude-code/issues/41659). Your actual prompt? Less than 1% of total token spend.

This is the [#1 pain point](https://github.com/anthropics/claude-code/issues/16157) for Claude Code users (666+ upvotes).

## The Solution

helix-agent is an MCP server that compresses screenshots, DOM, and browser output through your local GPU before Claude sees them. It detects retry loops before they drain your quota. Routine tasks run on Ollama at $0 instead of Opus.

Connect it to Claude Code and token savings happen automatically -- no workflow changes needed.

## Measured Results

| What | Without | With helix-agent | Reduction |
|---|---|---|---|
| Screenshot analysis | ~15,000 tokens | ~400 tokens | **97%** |
| DOM/HTML processing | ~114,000 tokens | ~500 tokens | **99%** |
| Browser automation | ~15,000 tokens/action | ~1,000-2,700 | **82-93%** |
| Retry loops | Infinite (until quota dies) | Stopped at 3rd repeat | **100%** |
| Routine tasks | Opus tokens ($$$) | Local LLM ($0) | **100%** |

All compression runs on your local GPU via Ollama. Zero cloud API cost.

## Quick Start

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent && uv sync
ollama pull gemma4:e2b          # 8GB GPU (or e4b/26b/31b for larger)
uv run python server.py
```

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "helix-agent": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/helix-agent", "python", "server.py"]
    }
  }
}
```

Restart Claude Code. Done.

## How It Works

```
Claude Code (Opus)
    |
    +-- helix-agent (MCP server)
           |
           +-- vision_compress ---- Local LLM ----> ~400 tokens  (was 15,000)
           +-- dom_compress ------- Local LLM ----> ~500 tokens  (was 114,000)
           +-- retry_guard -------- Pure logic ----> Loop stopped (sub-ms)
           +-- think / agent_task - Local LLM ----> $0 reasoning
           +-- computer_use ------- agent-browser -> 82-93% saved
           +-- code_review -------- 4-layer LLM --> $0.20 total
```

## Features

- **Vision Compress** -- Screenshot to structured text via local vision LLM. 15,000 tokens to 400. Raw images auto-deleted from responses.
- **DOM Compress** -- HTML/DOM to structured extract via local LLM. 114,000 tokens to 500. Forms, links, buttons, and action candidates preserved.
- **Retry Guard** -- Detects identical tool calls before they loop. SHA1 fingerprints, sliding time window, sub-millisecond. No LLM needed.
- **GPU Auto-Detection** -- Detects your GPU at startup, selects the optimal model from 8GB to 96GB+.
- **Browser Automation** -- Routes through agent-browser (Rust/CDP) with Playwright fallback. Native keyboard events fix React controlled components.
- **4-Layer Code Review** -- gemma4 + Sonnet + Opus + Codex pipeline catches all issues at ~$0.20.
- **Self-Evolving Memory** -- Reviews conversations every 5 turns, saves reusable skills as SKILL.md files. Gets smarter over time, all local.
- **Parallel Tasks** -- Run multiple tasks simultaneously with 2-axis model routing (task type x input size).
- **ReAct Agents** -- Local LLM delegation with tool access, sub-agents, background workers, and JSONL tracing.
- **PathGuard Security** -- Path allowlists prevent delegated tools from accessing sensitive locations. Defends against [CVE-2025-59536](https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/).

## GPU Auto-Detection

helix-agent auto-selects the best model for your hardware:

| Your GPU | VRAM | Model | Compress Speed |
|---|---|---|---|
| RTX 4060 | 8GB | gemma4:e2b | **10.2s** |
| RTX 4070 Ti | 16GB | gemma4:e4b | **11.8s** |
| RTX 4090 / 3090 | 24GB | gemma4:26b | **14.7s** |
| RTX PRO 6000 | 48GB+ | gemma4:31b | 27.5s |

gemma4:e2b on 8GB runs **2.7x faster** than 31b with comparable compression quality. No expensive GPU required.

## Vision Pipeline

```
+--------------+     +-----------------+     +--------------+
| Screenshot   |---->| vision_compress |---->| ~400 tokens  |
| (15K tokens) |     | (local gemma4)  |     | (text only)  |
+--------------+     +-----------------+     +--------------+

+--------------+     +-----------------+     +--------------+
| DOM / HTML   |---->| dom_compress    |---->| ~500 tokens  |
| (114K tokens)|     | (local gemma4)  |     | (text only)  |
+--------------+     +-----------------+     +--------------+
```

Real measurement (RTX PRO 6000):
```
Input:  1920x1048 screenshot of X.com (~15,000 tokens)
Output: "X home feed, Japanese UI, 'For You' tab active..." (~400 tokens)
Saved:  7,362 tokens in one call
```

## 4-Layer Code Review

Automated multi-LLM review at ~$0.20 total:

| Layer | Reviewer | Findings | Cost |
|---|---|---|---|
| 1 | gemma4 + RAG (local) | 7 | **$0** |
| 2 | Sonnet 4.7 | 14 | ~$0.13 |
| 3 | Opus 4.7 (summary only) | 16 | ~$0.03 |
| 4 | Codex (P1 only, on-demand) | 5 | ~$0.33 |
| **Combined** | | **16+** | **~$0.20** |

gemma4 + RAG ($0) outperforms Codex GPT-5.3 (~$0.33) in code review findings.

## What Nothing Else Does

| Capability | helix-agent | Alternatives |
|---|---|---|
| Screenshot to text (97% cut) | Local vision LLM | No MCP server does this |
| DOM to text (99% cut) | Local LLM | Playwright MCP sends raw DOM |
| Retry loop detection | Sub-ms, no LLM | No built-in Claude Code detection |
| GPU auto-detect + model select | 8GB to 96GB+ | Manual config required |
| Self-evolving memory | SKILL.md + Qdrant | Unique to helix-agent |
| All 3 MCP primitives | 27 Tools + 3 Resources + 3 Prompts | Most MCPs implement Tools only |

## MCP Architecture

27 tools organized by function:

| Category | Tools |
|---|---|
| Token saving | `vision_compress`, `dom_compress` |
| Loop prevention | `retry_guard_check`, `retry_guard_status`, `retry_guard_reset` |
| Local delegation | `think`, `agent_task`, `fork_task`, `parallel_tasks` |
| Vision & browser | `see`, `browse`, `computer_use` |
| Background agents | `spawn_agent`, `send_agent_input`, `wait_agent`, `list_agents`, `close_agent` |
| Memory | `evolving_memory_review`, `list_learned_skills`, `get_skill`, `dept_search`, `dept_store` |
| Code quality | `code_review` |
| Meta | `providers`, `models`, `config`, `agent_types` |

Plus 3 Resources (`helix://status`, `helix://models`, `helix://config`) and 3 Prompts (`retry_report`, `optimize_tokens`, `setup_guide`).

## Configuration

helix-agent works with zero configuration. For advanced setups:

```bash
# Environment variables (all optional)
OLLAMA_HOST=http://localhost:11434   # Ollama endpoint
HELIX_PROVIDER=ollama               # LLM provider
HELIX_LOG_LEVEL=INFO                # Logging level
```

Optional dependencies:
- [Qdrant](https://qdrant.tech/) -- shared memory across sessions
- [Playwright](https://playwright.dev/) -- browser automation fallback
- [agent-browser](https://github.com/vercel-labs/agent-browser) -- recommended for 82-93% browser token savings

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.ai/) + any Gemma 4 model:

| GPU VRAM | Command | Model Size |
|---|---|---|
| 8GB | `ollama pull gemma4:e2b` | 4GB |
| 16GB | `ollama pull gemma4:e4b` | 6GB |
| 24GB | `ollama pull gemma4:26b` | 12GB |
| 48GB+ | `ollama pull gemma4:31b` | 20GB |

## Related Projects

- [helix-pilot](https://github.com/tsunamayo7/helix-pilot) -- GUI automation MCP server
- [claude-code-codex-agents](https://github.com/tsunamayo7/claude-code-codex-agents) -- MCP bridge to Codex CLI
- [helix-sandbox](https://github.com/tsunamayo7/helix-sandbox) -- Secure sandbox MCP server

## Not a Claude Code Wrapper

helix-agent is an MCP server that Claude Code connects to. It does not wrap, proxy, or re-host Claude Code or the Anthropic API. Fully compliant with Anthropic's Terms of Service.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
