<p align="center">
  <h1 align="center">helix-agent</h1>
  <p align="center">
    <strong>Extend Claude Code with local Ollama models — intelligent auto-routing, zero config</strong>
  </p>
  <p align="center">
    <a href="https://github.com/tsunamayo7/helix-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="License"></a>
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12+-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+"></a>
    <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-compatible-green?style=flat-square" alt="MCP Compatible"></a>
    <a href="https://ollama.com"><img src="https://img.shields.io/badge/Ollama-local%20LLM-purple?style=flat-square" alt="Ollama"></a>
    <a href="https://github.com/tsunamayo7/helix-agent"><img src="https://img.shields.io/github/stars/tsunamayo7/helix-agent?style=flat-square" alt="Stars"></a>
  </p>
</p>

**English** | [日本語](README_ja.md)

---

helix-agent is an MCP server that lets **Claude Code delegate tasks to local Ollama models** — reasoning, code review, image analysis, and more. It automatically selects the best model for each task from your installed Ollama models.

**No API keys. No cloud. No config files. Just works.**

## Why helix-agent?

| Problem | helix-agent Solution |
|---------|---------------------|
| Claude Code uses your API tokens for everything | Delegate routine tasks to free local models |
| PAL MCP consumes 50% of your context window | **<5% context overhead** — lean tool definitions |
| Existing Ollama MCPs need manual model selection | **Auto-routing** — detects installed models and picks the best one |
| No quality guarantee from local models | **Quality-first** — Claude reviews local LLM output |
| Complex setup with multiple config files | **Zero-config** — `uv run` and go |

## Quick Start

```bash
# 1. Have Ollama running with at least one model
ollama pull gemma3

# 2. Clone and install
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent && uv sync

# 3. Add to Claude Code
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

That's it. Claude Code now has access to your local Ollama models.

## Tools

### `think` — Reasoning, Analysis, Code Generation

Delegate any text task to a local LLM. Auto-selects the best model.

```
Claude Code: "Use helix-agent to summarize this 500-line log file"
-> helix-agent routes to qwen3.5:122b (reasoning model)
-> Returns summary
-> Claude verifies and enhances
```

**Modes:**
- `quality` — Large model, low temperature, thorough (default)
- `fast` — Small model, brief output
- `creative` — High temperature, exploratory

### `agent_task` — Autonomous ReAct Agent

Run multi-step tasks where the local LLM reasons, uses tools, and iterates autonomously.

```
Claude Code: "Use helix-agent agent to read pyproject.toml and summarize the project"
-> Step 1: LLM thinks "I need to read the file" → read_file
-> Step 2: LLM analyzes contents → finish with summary
-> Returns structured result with full reasoning trace
```

**Built-in tools:**
- `read_file` / `write_file` / `list_files` / `search_in_file` — File operations (PathGuard secured)
- `run_command` — Shell execution (allowlist: git, python, uv, ollama)
- `calculate` — Safe math evaluation
- `search_memory` — Qdrant semantic search

**Security:** PathGuard enforces directory allowlists, blocks sensitive files (.env, credentials, SSH keys), and prevents path traversal attacks.

### `see` — Vision & OCR

Analyze images with local Vision models.

```
Claude Code: "Use helix-agent to OCR this screenshot"
-> helix-agent routes to mistral-small3.2 (vision model)
-> Extracts all text from the image
```

### `models` — Model Discovery, Benchmark & Override

Check what's available, benchmark models on your hardware, and lock routing to a specific model.

```
> models(action="capabilities")
{
  "vision": ["mistral-small3.2:latest", "gemma3:27b"],
  "code": ["qwen-coder:7b"],
  "reasoning": ["qwen3.5:122b", "nemotron-cascade-2:latest"],
  "embedding": ["qwen3-embedding:8b"]
}
```

**Benchmark** — Evaluate models on your actual hardware:

```
> models(action="benchmark")                        # Benchmark all unbenchmarked models
> models(action="benchmark", model_name="gemma3:4b") # Benchmark a specific model
> models(action="benchmark_status")                  # View ranking
```

Tests include: code generation (FizzBuzz, string manipulation), reasoning (logic, math), instruction following (JSON output, list format), Japanese (translation, summarization), and speed (tokens/sec). Results are cached in `~/.helix-agent/benchmarks.json` and automatically influence routing priority.

**Model Override** — Lock routing to a specific model:

```
> models(action="use", model_name="qwen3.5:122b")   # Force all tasks to use this model
> models(action="use_auto")                           # Switch back to auto-selection
```

### `config` — Runtime Configuration

Adjust settings without restarting.

## How Auto-Routing Works

```
Task: "Review this Python function for bugs"
  |
Keyword Detection: "function", "bugs" -> CODE capability
  |
Model Filter: installed models with CODE capability
  |
Priority Sort: qwen-coder > deepseek-coder > generic
  |
Selected: qwen-coder:7b
```

The router uses:
1. **Local benchmark scores** — Real performance data from your hardware (v0.3.0)
2. **Name pattern matching** — Detects capabilities from model names
3. **Size-based priority** — Larger models preferred for quality mode
4. **Known model boosting** — Trusted models get priority

## Quality-First Design

helix-agent is designed as a **draft generator**, not a replacement for Claude:

```
User -> Claude Code -> helix-agent.think() -> Local LLM (draft)
                                                |
                                          Claude reviews & enhances
                                                |
                                          High-quality final answer
```

This means:
- Local LLM handles the heavy lifting (token-free)
- Claude adds its superior reasoning (minimal tokens)
- User always gets Claude-quality output

## vs Alternatives

| Feature | helix-agent | PAL MCP | OllamaClaude | ollama-mcp |
|---------|:-----------:|:-------:|:------------:|:----------:|
| Claude Code optimized | **Yes** | Partial | Yes | No |
| Zero-config | **Yes** | No | Partial | Partial |
| Context overhead | **<5%** | ~50% | ~2% | ~10% |
| Auto model selection | **Yes** | Yes | Fallback only | No |
| Vision support | **Yes** | Model-dependent | No | No |
| Quality modes | **3 modes** | No | No | No |
| Ollama-focused | **Yes** | No (all providers) | Yes | Yes |

## Supported Models

helix-agent works with any Ollama model. Auto-routing is optimized for:

| Capability | Recommended Models |
|-----------|-------------------|
| Reasoning | qwen3.5, nemotron-cascade-2, llama3.3, command-a |
| Code | qwen-coder, codestral, devstral, deepseek-coder |
| Vision | mistral-small3.2, gemma3, moondream |
| Embedding | qwen3-embedding, nomic-embed-text, bge |

### v0.2.0: Metadata-Enhanced Routing

Auto-routing now uses `ollama show` metadata for better model selection:
- **Context length** awareness (e.g., 262K for nemotron-cascade-2)
- **Parameter count** extraction for quality estimation
- **Smart fast mode** — penalizes 50GB+ models, prefers <10GB for speed
- Use `models(action="detailed")` to see full metadata

### v0.3.0: Local Benchmark + Model Override

Run benchmarks on your actual hardware to optimize routing:
- **8 automated tests** — code, reasoning, instruction following, Japanese, speed
- **Auto-scoring** — regex + pattern matching validators
- **Persistent cache** — results saved to `~/.helix-agent/benchmarks.json`
- **New model detection** — automatically identifies unbenchmarked models
- **Model override** — lock routing to a user-specified model
- **Benchmark-aware routing** — scores directly influence model selection priority

## Development

```bash
# Run tests (144 tests)
uv run pytest tests/ -v

# Type check
uv run python -m py_compile server.py
```

## Roadmap

- [x] v0.1.0 — Core tools (think, see, models, config) + name-based auto-routing
- [x] v0.2.0 — Metadata-enhanced routing (context length, parameter count, smart fast mode)
- [x] v0.3.0 — Local benchmark engine, model override, benchmark-aware routing
- [x] v0.4.0 — ReAct agent loop, file tools with PathGuard, progress notifications
- [ ] v0.5.0 — Qdrant memory integration, helix-sandbox command execution
- [ ] v1.0.0 — Public release, mcpservers.org listing

## Related Projects

- [helix-pilot](https://github.com/tsunamayo7/helix-pilot) — GUI automation MCP server (Windows desktop control via Vision LLM)
- [helix-sandbox](https://github.com/tsunamayo7/helix-sandbox) — Windows Sandbox MCP server

## License

MIT
