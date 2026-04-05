---
title: "I Turned helix-agent into helix-agent: One MCP Server for Ollama, Codex, and OpenAI-Compatible Models"
published: true
description: "helix-agent upgrades the original Ollama-focused MCP server into a multi-provider runtime with Codex support and Claude Code-style background agents."
tags: mcp, claudecode, ollama, codex, python
cover_image:
---

If you use Claude Code heavily, you eventually hit the same wall:

- some tasks are cheap enough for local models
- some tasks want a stronger coding agent
- some tasks are better sent to an API model

But most MCP servers still force one provider and one execution style.

So I evolved `helix-agent` into **helix-agent**.

It now lets Claude Code delegate work across:

- `ollama`
- `codex`
- `openai-compatible`

from one MCP server.

## What changed

The original project was good at one thing: sending routine work to local Ollama models with automatic routing.

The new version keeps that path, but adds:

- multi-provider switching
- Codex-backed code delegation
- OpenAI-compatible API support
- Claude Code-style background agents

That means the workflow is no longer:

```text
Claude Code -> one tool call -> one reply
```

It can now be:

```text
Claude Code
  -> spawn a worker
  -> send follow-up instructions
  -> wait for completion
  -> inspect and close
```

## Why this matters

Different providers are good at different things.

- `ollama`: local reasoning, low-cost drafts, vision
- `codex`: code-heavy implementation and repo work
- `openai-compatible`: hosted chat models behind standard APIs

Instead of wiring three separate MCP servers with different interaction models, I wanted one consistent runtime.

## New tools

Core tools:

- `think`
- `agent_task`
- `see`
- `providers`
- `models`
- `config`

Background agent tools:

- `spawn_agent`
- `send_agent_input`
- `wait_agent`
- `list_agents`
- `close_agent`

## Example flows

### 1. Code review via Codex

```text
think(
  task="Review this diff for regressions",
  provider="codex",
  cwd="/repo"
)
```

### 2. Local summarization via Ollama

```text
think(
  task="Summarize this build log",
  provider="ollama"
)
```

### 3. Persistent investigation worker

```text
spawn_agent(
  description="Investigate flaky tests",
  provider="codex",
  agent_type="explorer"
)
```

Then:

```text
send_agent_input(...)
wait_agent(...)
close_agent(...)
```

## Setup

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent
uv sync
uv run python server.py
```

Add to Claude Code:

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

## Notes

- Codex requires `codex` on `PATH`
- OpenAI-compatible mode requires an API key
- Vision is currently centered on the Ollama path

GitHub:
https://github.com/tsunamayo7/helix-agent
