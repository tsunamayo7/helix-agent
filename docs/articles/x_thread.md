# X (Twitter) Thread

## Tweet 1
I turned `helix-agent` into **helix-agents**.

It started as a Claude Code MCP for local Ollama models.
Now it can route work across:

- Ollama
- Codex
- OpenAI-compatible APIs

## Tweet 2
The real change isn’t just provider switching.

I added Claude Code-style background agent tools:

- spawn_agent
- send_agent_input
- wait_agent
- list_agents
- close_agent

## Tweet 3
That means the workflow is more like:

Claude Code -> delegate to a specialist -> continue -> wait -> inspect -> close

instead of:

Claude Code -> one tool call -> one text dump

## Tweet 4
Practical split:

- Ollama for local drafts + vision
- Codex for repo-heavy coding tasks
- OpenAI-compatible for hosted chat models

## Tweet 5
Repo:
https://github.com/tsunamayo7/helix-agent

Still lightweight, still MCP-first, just much more flexible now.

## Tweet 6
Next thing I want to improve is the provider heuristics and richer cross-agent summaries.

If you’re building around Claude Code + MCP, this direction might be useful.
