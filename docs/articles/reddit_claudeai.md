# Reddit r/ClaudeAI Post

**Title:**
I turned my Ollama MCP into a multi-provider Claude Code agent runtime

**Body:**

I’ve been evolving `helix-agent` into **helix-agents**.

The original version was focused on local Ollama models.
The new version can delegate through one MCP server to:

- `ollama`
- `codex`
- `openai-compatible`

The main thing I wanted was not just provider switching, but a more natural Claude Code workflow.

So I added persistent background agent tools too:

- `spawn_agent`
- `send_agent_input`
- `wait_agent`
- `list_agents`
- `close_agent`

That makes it feel less like “one tool call, one dump of text” and more like “delegate to a specialist and continue working”.

Examples:

- use `ollama` for local drafts and vision
- use `codex` for repo-heavy implementation or review
- use `openai-compatible` for API-backed models

The original Ollama path is still there, but the runtime is much more flexible now.

GitHub:
https://github.com/tsunamayo7/helix-agent
