# helix-agent

**The missing retry loop guard for Claude Code.** Detect quota-draining repeat calls, compress screenshots & DOM locally — all through one MCP server.

日本語README: **[README.ja.md](README.ja.md)**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-10b981.svg)](https://modelcontextprotocol.io)
[![Tests](https://img.shields.io/badge/tests-322%20passing-brightgreen.svg)](#)
[![v0.13.0](https://img.shields.io/badge/version-0.13.0-7c3aed.svg)](#)
[![MCP 3-Primitive](https://img.shields.io/badge/MCP-Tools%20%2B%20Resources%20%2B%20Prompts-10b981.svg)](#)

## Why retry_guard?

Claude Code's Opus sometimes gets stuck calling the same tool with identical args when it misreads an error ([anthropics/claude-code#41659](https://github.com/anthropics/claude-code/issues/41659)). A Max plan 5-hour quota [can vanish in 19 minutes](https://www.macrumors.com/2026/03/26/claude-code-users-rapid-rate-limit-drain-bug/).

There is **no built-in loop detection**. The community best practice is "write your own hook". `retry_guard` packages that hook as a reusable MCP tool.

```python
retry_guard_check(tool_name="navigate", args={"url": "..."})
# → {"loop_detected": true, "repeat_count": 3,
#    "recommendation": "Tool 'navigate' called 3 times with identical args.
#                       Likely stuck in retry loop. Vary args or escalate."}
```

Three tools, one purpose:

| Tool | Purpose |
|------|---------|
| `retry_guard_check` | Called before a risky tool — warns if this exact call is looping |
| `retry_guard_status` | Session stats: total_calls / unique_calls / max_repeats |
| `retry_guard_reset` | Clear history after resolving a loop |

Per-session histories, SHA1-hashed call fingerprints, sliding time window. No LLM required for the guard itself — pure logic, sub-millisecond.

## Bundled extras

### Token savers (local gemma4:31b)

When Claude Code is about to consume a 15K-token screenshot or 114K-token DOM payload, route it through `helix-agent` first and hand Claude the ~400-token structured summary instead:

- **`vision_compress`** — screenshot → JSON (page_type, interactive_elements, state_flags)
- **`dom_compress`** — HTML → JSON (forms, links, buttons, next_action_candidates)

### Browser automation (v0.12.0)

`computer_use` routes browser actions through [Vercel's agent-browser](https://github.com/vercel-labs/agent-browser) (Rust/CDP) by default when available, falling back to helix-pilot → Playwright.

Measured on 50 identical automation flows:

| Backend | Tokens per action | React controlled components |
|---------|-------------------|-----------------------------|
| Playwright (screenshot+DOM) | ~15,000 | ⚠️ setValue silently reverts |
| agent-browser (accessibility tree) | ~1,000–2,700 | ✅ native keyboard events work |

Native keyboard events via `fill` finally make Wantedly / LinkedIn / other React SPAs fillable from an agent without extra hacks.

### Delegation & agents

ReAct loop with tool access, context-inheriting sub-agents, background workers, Qdrant shared memory, JSONL tracing, PathGuard safety, OOM auto-fallback.

- `think` / `agent_task` / `fork_task` — local LLM delegation
- `see` / `browse` / `computer_use` — vision + browser
- `spawn_agent` / `send_agent_input` / `wait_agent` / `list_agents` / `close_agent`
- `search_memory` / `add_memory` — Qdrant
- `providers` / `models` / `config` / `agent_types`

## Quick Start

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent
uv sync
ollama pull gemma4:31b   # only needed for vision_compress / dom_compress
uv run python server.py
```

Add to Claude Code (`~/.claude/settings.json`):

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

Restart Claude Code. `retry_guard_check`, `vision_compress`, and friends are now available.

## Japanese users — 日本語ユーザー向け

helix-agent ships opt-in Japanese helpers for Claude Code:

- **`helix-agent-ja-input`** — floating input window for Windows that sidesteps the React Ink + IME incompatibility ([known issue](https://zenn.dev/atu4403/articles/claudecode-japanese-input-solution))
- **`ja_screen_read`** (coming in v1.2) — Japanese UI screenshot parsing via PaddleOCR + gemma4

See [README.ja.md](README.ja.md) for details.

## Security

Claude Code has documented prompt-injection vulnerabilities
([CVE-2025-59536](https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/))
where malicious content in project files can exfiltrate API tokens. helix-agent
ships **PathGuard** — path allowlists and sanitization — so delegated tools
cannot access sensitive locations outside the workspace. See [SECURITY.md](SECURITY.md).

## Not a Claude Code wrapper

helix-agent is an **MCP server that Claude Code connects to** — it does not
wrap, proxy, or re-host Claude Code or the Anthropic API. Fully compliant with
Anthropic's Terms of Service.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- For vision/DOM compress: [Ollama](https://ollama.ai/) + a vision-capable model (gemma4:31b recommended, 20GB+ VRAM)
- For Japanese helpers: nothing extra (`ja-input` uses stdlib tkinter)

Optional:
- Qdrant (shared memory)
- Playwright (browser automation)
- PaddleOCR (`pip install helix-agent[ja]`, for upcoming `ja_screen_read`)

## MCP 3-Primitive Architecture

helix-agent implements all three MCP primitives as defined by [Anthropic Academy](https://anthropic.skilljar.com/introduction-to-model-context-protocol):

| Primitive | Control | Count | Examples |
|-----------|---------|-------|----------|
| **Tools** | Model-controlled (Claude decides) | 20 | `retry_guard_check`, `think`, `computer_use`, `vision_compress` |
| **Resources** | App-controlled (read-only data) | 3 | `helix://status`, `helix://models`, `helix://config` |
| **Prompts** | User-controlled (workflows) | 3 | `retry_report`, `optimize_tokens`, `setup_guide` |

```
Claude Code (Opus 4.6 — decides what to do)
  │
  ├─ Resources (read-only)
  │   ├─ helix://status       → runtime state, backend, retry-guard stats
  │   ├─ helix://models       → available Ollama/provider models
  │   └─ helix://config       → current configuration
  │
  ├─ Prompts (user-triggered workflows)
  │   ├─ retry_report         → loop detection analysis (Japanese)
  │   ├─ optimize_tokens      → token saving recommendations
  │   └─ setup_guide          → first-run setup walkthrough (Japanese)
  │
  ├─ Tools (20 total)
  │   ├─ retry_guard_check    → is this tool call looping? (pure logic, no LLM)
  │   ├─ vision_compress      → gemma4 vision → ~400-token summary
  │   ├─ dom_compress         → gemma4 text → ~500-token structured extract
  │   ├─ think / agent_task   → ReAct loop with local model
  │   ├─ fork_task            → parent-context inheriting sub-agent
  │   ├─ computer_use / browse → agent-browser → helix-pilot → Playwright
  │   └─ spawn/send/wait/list/close → background agent workers
  │
  └─ Infrastructure
      ├─ Qdrant shared memory
      ├─ JSONL tracing
      ├─ PathGuard path safety
      └─ OOM auto-fallback chain
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Related Projects

- [helix-ai-studio](https://github.com/tsunamayo7/helix-ai-studio) — All-in-one AI chat studio with 7 providers, RAG, MCP tools, and pipeline
- [helix-pilot](https://github.com/tsunamayo7/helix-pilot) — GUI automation MCP server — AI controls Windows desktop via local Vision LLM
- [claude-code-codex-agents](https://github.com/tsunamayo7/claude-code-codex-agents) — MCP bridge to Codex CLI with structured JSONL traces
- [helix-sandbox](https://github.com/tsunamayo7/helix-sandbox) — Secure sandbox MCP server — Docker + Windows Sandbox

## License

MIT
