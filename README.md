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

### `see` — Vision & OCR

Analyze images with local Vision models.

```
Claude Code: "Use helix-agent to OCR this screenshot"
-> helix-agent routes to mistral-small3.2 (vision model)
-> Extracts all text from the image
```

### `models` — Model Discovery

Check what's available locally.

```
> models(action="capabilities")
{
  "vision": ["mistral-small3.2:latest", "gemma3:27b"],
  "code": ["qwen-coder:7b"],
  "reasoning": ["qwen3.5:122b", "nemotron-cascade-2:latest"],
  "embedding": ["qwen3-embedding:8b"]
}
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
1. **Name pattern matching** — Detects capabilities from model names
2. **Size-based priority** — Larger models preferred for quality mode
3. **Known model boosting** — Trusted models get priority

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
| Reasoning | qwen3.5, nemotron-cascade-2, llama3.3 |
| Code | qwen-coder, codestral, deepseek-coder |
| Vision | mistral-small3.2, gemma3, moondream |
| Embedding | qwen3-embedding, nomic-embed-text |

## Development

```bash
# Run tests
uv run pytest tests/ -v

# Type check
uv run python -m py_compile server.py
```

## Roadmap

- [x] v0.1.0 — Core tools (think, see, models, config) + auto-routing (49 tests passing)
- [ ] v0.2.0 — Qdrant shared memory integration (`remember` tool)
- [ ] v0.3.0 — Benchmark-based routing, parallel inference, streaming
- [ ] v1.0.0 — Public release, mcpservers.org listing

## Related Projects

- [helix-pilot](https://github.com/tsunamayo7/helix-pilot) — GUI automation MCP server (Windows desktop control via Vision LLM)
- [helix-sandbox](https://github.com/tsunamayo7/helix-sandbox) — Windows Sandbox MCP server

## License

MIT
