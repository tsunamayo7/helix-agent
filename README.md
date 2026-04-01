# helix-agents

Claude Code-oriented MCP server for delegating work to multiple LLM providers.

`helix-agents` evolves the original Ollama-focused `helix-agent` into a provider-switchable runtime that can route work to:

- `ollama` for local models and vision tasks
- `codex` for code-heavy autonomous work through Codex CLI
- `openai-compatible` for API-based chat models

## What Changed

- Multi-provider routing with `provider="auto" | "ollama" | "codex" | "openai-compatible"`
- Claude Code-style background agent lifecycle
- Provider inspection and switching
- Codex support without losing the existing Ollama path

## Tools

### `think`

Single-step delegation for reasoning, analysis, code generation, review, or drafting.

### `agent_task`

Multi-step task execution.

- `ollama` and `openai-compatible` use the built-in ReAct loop
- `codex` runs as an autonomous implementation/review worker

### `see`

Image analysis. Currently best supported through `ollama`.

### `providers`

Inspect provider availability or switch the default provider.

### `models`

List provider-specific models and set provider-specific model overrides.

### Background agent tools

- `spawn_agent`
- `send_agent_input`
- `wait_agent`
- `list_agents`
- `close_agent`

These make the server behave more like a persistent Claude Code sub-agent runtime instead of a one-shot tool bridge.

## Quick Start

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent
uv sync
uv run python server.py
```

Add it to Claude Code:

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

## Provider Configuration

Use `config(action="show")` to inspect runtime settings.

Important keys:

- `default_provider`
- `ollama_host`
- `codex_model`
- `codex_sandbox`
- `openai_base_url`
- `openai_api_key_env`
- `openai_model`

Examples:

```text
providers(action="use", provider="codex")
models(action="list", provider="ollama")
config(action="set", key="openai_model", value="gpt-4.1")
think(task="Review this diff", provider="codex", cwd="/repo")
spawn_agent(description="Investigate flaky tests", provider="codex", agent_type="explorer")
```

## Notes

- Codex execution requires `codex` on `PATH`
- OpenAI-compatible execution requires a valid API key in the configured env var
- Vision support is currently implemented only on the Ollama path
