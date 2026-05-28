# helix-agent

**MCP server that cuts Claude Code token usage by 82-97% using local LLMs.**

![Demo](docs/demo.gif)

[![GitHub Stars](https://img.shields.io/github/stars/tsunamayo7/helix-agent?style=social)](https://github.com/tsunamayo7/helix-agent)
[![CI](https://github.com/tsunamayo7/helix-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/tsunamayo7/helix-agent/actions/workflows/ci.yml)
[![CodeQL](https://github.com/tsunamayo7/helix-agent/actions/workflows/codeql.yml/badge.svg)](https://github.com/tsunamayo7/helix-agent/actions/workflows/codeql.yml)
[![Tests](https://img.shields.io/badge/tests-471%20passing-brightgreen.svg)](#testing)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-10b981.svg)](https://modelcontextprotocol.io)

## The Problem

Claude Code burns tokens on things that don't need a frontier model. A single screenshot costs **~15,000 tokens**. One DOM snapshot costs **~114,000 tokens**. Retry loops repeat failing calls until your quota is gone — the [#1 reported pain point](https://github.com/anthropics/claude-code/issues/16157) (600+ upvotes), with [no built-in fix](https://github.com/anthropics/claude-code/issues/41659).

## Before / After

| | Without helix-agent | With helix-agent |
|---|---|---|
| Screenshot | 15,000 tokens (raw image) | **400 tokens** (structured text) |
| DOM snapshot | 114,000 tokens (raw HTML) | **500 tokens** (action summary) |
| Retry loop | Runs until quota dies | **Stopped at 3rd repeat** |
| Reasoning task | Opus tokens | **$0** (local Ollama) |

## Quick Start

**1. Install and run:**

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent && uv sync
ollama pull gemma4:e2b    # 8GB VRAM minimum
```

**2. Add to Claude Code** (`~/.claude/settings.json`):

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

**3. Restart Claude Code.** Token savings happen automatically — no workflow changes needed.

## Measured Results

| Operation | Before | After | Reduction |
|---|---|---|---|
| Screenshot analysis | ~15,000 tokens | ~400 tokens | **97%** |
| DOM/HTML processing | ~114,000 tokens | ~500 tokens | **99.6%** |
| Browser automation | ~15,000 tokens/action | ~1,000-2,700 | **82-93%** |
| Retry loops | Infinite | Stopped at 3rd repeat | **100%** |
| Reasoning / summaries | Opus ($$$) | Local LLM | **$0** |

All compression runs on your local GPU via [Ollama](https://ollama.ai/). Zero cloud API cost.

## Key Features

- **vision_compress** — Screenshots to structured text via local vision LLM. 15,000 → 400 tokens (97% reduction).
- **dom_compress** — HTML/DOM to action summaries via local LLM. 114,000 → 500 tokens (99.6% reduction).
- **retry_guard** — Detects identical tool calls and stops loops at the 3rd repeat. Sub-millisecond, no LLM needed.
- **think / agent_task** — Delegate reasoning, analysis, and multi-step tasks to local Ollama at $0.
- **SecurityMiddleware** — Deny-by-default policy on every MCP tool call. JSONL audit trail.
- **GPU auto-detection** — Selects the optimal model for your hardware, from 8GB to 96GB+ VRAM.
- **Self-evolving memory** — Auto-extracts reusable skills from conversations. Qdrant hybrid search (dense + sparse vectors).

## Architecture

```
Claude Code (Opus/Sonnet)
    │
    └── helix-agent (MCP server, FastMCP)
            │
            ├── SecurityMiddleware ── deny-by-default policy check
            │
            ├── vision_compress ──── local VLM (gemma4) ──→  ~400 tokens (was 15,000)
            ├── dom_compress ─────── local LLM (gemma4) ──→  ~500 tokens (was 114,000)
            ├── retry_guard ──────── pure logic ───────────→  loop stopped (sub-ms)
            ├── think ────────────── local LLM (gemma4) ──→  $0 reasoning
            ├── agent_task ───────── ReAct loop + tools ──→  $0 multi-step
            ├── computer_use ─────── CDP / Playwright ────→  82-93% saved
            ├── code_review ──────── 4-layer pipeline ────→  ~$0.20 total
            │
            ├── Qdrant (optional) ── hybrid search (dense + sparse vectors)
            └── Langfuse (optional)─ OTLP tracing
```

## MCP Coverage

28 tools, 3 resources, 3 prompts — full MCP spec coverage.

| Category | Tools |
|---|---|
| Token saving | `vision_compress`, `dom_compress` |
| Loop prevention | `retry_guard_check`, `retry_guard_status`, `retry_guard_reset` |
| Local delegation | `think`, `agent_task`, `fork_task`, `parallel_tasks` |
| Browser & vision | `see`, `browse`, `computer_use` |
| Background agents | `spawn_agent`, `send_agent_input`, `wait_agent`, `list_agents`, `close_agent` |
| Memory & learning | `evolving_memory_review`, `list_learned_skills`, `get_skill`, `dept_search`, `dept_store` |
| Code quality | `code_review`, `x_search` |
| Infrastructure | `providers`, `models`, `config`, `agent_types` |

Resources: `helix://status`, `helix://models`, `helix://config`
Prompts: `retry_report`, `optimize_tokens`, `setup_guide`

## Benchmarks

### Token Compression

```
Input:  1920x1048 screenshot (X.com, Japanese UI)
Before: ~15,000 tokens (raw image sent to Claude)
After:  ~400 tokens ("X home feed, 'For You' tab active, 3 posts visible...")
Saved:  14,600 tokens per screenshot
```

### GPU Speed

| GPU | VRAM | Model | Compress Speed |
|---|---|---|---|
| RTX 4060 / M1 8GB | 8GB | gemma4:e2b | **10.2s** |
| RTX 4070 Ti / M2 Pro | 16GB | gemma4:e4b | **11.8s** |
| RTX 4090 / M4 Max | 24GB+ | gemma4:26b | **14.7s** |
| RTX PRO 6000 | 48GB+ | gemma4:31b | 27.5s |

gemma4:e2b on 8GB VRAM runs **2.7x faster** than 31b with comparable compression quality.

### 4-Layer Code Review

| Layer | Model | Cost |
|---|---|---|
| 1 | gemma4 + RAG (local) | **$0** |
| 2 | Sonnet 4 | ~$0.13 |
| 3 | Opus 4 (summary) | ~$0.03 |
| 4 | Codex (P1 escalation) | ~$0.33 |
| **Total** | | **~$0.20** |

## Security

### SecurityMiddleware (deny-by-default)

Every MCP tool call passes through SecurityMiddleware before execution. Unknown tools are denied by default. Risk levels (LOW / MEDIUM / HIGH) are assigned per tool with parameter-aware rules.

```python
# Enforced automatically via FastMCP Middleware
allowed, reason = check_tool_permission("computer_use", {"action": "click"})
# HIGH risk actions return a structured warning instead of executing
```

### PathGuard

Delegated tools can only read/write directories you explicitly permit. Defends against [CVE-2025-59536](https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/) (RCE via Claude Code project files).

```bash
HELIX_ALLOWED_PATHS=/home/user/projects,/tmp
```

## Platform Support

| Platform | GPU | Status |
|---|---|---|
| macOS (Apple Silicon) | Metal / M1-M4 | Tested daily |
| Linux | NVIDIA CUDA | Primary dev environment |
| Windows (WSL2 / native) | NVIDIA CUDA | Supported via Ollama |
| CPU-only | None | Works (slower) |

Anywhere Ollama runs, helix-agent runs.

## Configuration

Zero configuration required. All settings have sensible defaults.

```bash
# Environment variables (all optional)
OLLAMA_HOST=http://localhost:11434    # Ollama endpoint
HELIX_ALLOWED_PATHS=/home/user/code  # PathGuard allowlist
HELIX_LOG_LEVEL=INFO                 # Logging verbosity
```

Optional integrations:
- [Qdrant](https://qdrant.tech/) — persistent memory with hybrid search (dense + sparse vectors)
- [Langfuse](https://langfuse.com/) — OTLP tracing for tool call observability
- [Playwright](https://playwright.dev/) — browser automation fallback

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- [Ollama](https://ollama.ai/) with a Gemma 4 model:

| GPU VRAM | Command | Model Size |
|---|---|---|
| 8GB | `ollama pull gemma4:e2b` | 4GB |
| 16GB | `ollama pull gemma4:e4b` | 6GB |
| 24GB | `ollama pull gemma4:26b` | 12GB |
| 48GB+ | `ollama pull gemma4:31b` | 20GB |

## Testing

471 tests, all passing. Ollama calls are fully mocked — no GPU required to run the test suite.

```bash
uv run pytest tests/ -q          # Run all tests
uv run pytest tests/ --cov=src   # With coverage
uv run ruff check src/ tests/    # Linting
```

## How It Compares

| Capability | helix-agent | Alternatives |
|---|---|---|
| Screenshot → text (97% reduction) | Local vision LLM | No MCP server does this |
| DOM → text (99.6% reduction) | Local LLM | Playwright MCP sends raw DOM |
| Retry loop detection | Sub-ms, no LLM | No built-in Claude Code detection |
| GPU auto-detection | 8GB to 96GB+ | Manual config elsewhere |
| Deny-by-default security | SecurityMiddleware + PathGuard | Most MCPs have no security layer |
| Self-evolving memory | Skills + Qdrant hybrid search | Unique to helix-agent |
| Full MCP spec | 28 Tools + 3 Resources + 3 Prompts | Most MCPs implement Tools only |

## Related Projects

- [helix-pilot](https://github.com/tsunamayo7/helix-pilot) — GUI automation MCP server
- [claude-code-codex-agents](https://github.com/tsunamayo7/claude-code-codex-agents) — MCP bridge to Codex CLI

## Not a Claude Code Wrapper

helix-agent is a standard MCP server that Claude Code connects to. It does not wrap, proxy, or re-host Claude Code or the Anthropic API.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome.

## License

[MIT](LICENSE)
