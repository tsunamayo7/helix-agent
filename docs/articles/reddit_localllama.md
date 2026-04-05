# Reddit r/LocalLLaMA Post

**Title:**
I upgraded my Claude Code + Ollama MCP into a multi-provider agent runtime

**Body:**

I originally built `helix-agent` as an MCP server for Claude Code + local Ollama models.

I’ve now pushed it further into **helix-agent**:

- keeps the Ollama path
- adds `codex`
- adds `openai-compatible`
- adds persistent background agents

So it’s no longer just “send one request to a local model”.
It can act more like a routing/orchestration layer depending on the task.

Examples:

- local summarization and vision via `ollama`
- code-heavy work via `codex`
- hosted chat models via `openai-compatible`

And the interaction model is more agent-like now:

- `spawn_agent`
- `send_agent_input`
- `wait_agent`
- `list_agents`
- `close_agent`

Still useful if you mainly care about local models, because the Ollama integration remains first-class.

GitHub:
https://github.com/tsunamayo7/helix-agent
