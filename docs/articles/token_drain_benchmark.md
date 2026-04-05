# I cut Claude Code's Computer Use token costs by 94% with a local vision model

**TL;DR**: Claude Code's Computer Use is expensive because every screenshot costs
5K-15K tokens and every Playwright MCP call ships ~114K tokens of DOM. I wrote a
tiny MCP server that routes those payloads through gemma4:31b locally first, so
Claude only sees a ~400-token structured summary. Max plan survival extended.

Repo: https://github.com/tsunamayo7/helix-agent

---

## The crisis Anthropic admitted to

On 2026-03-31 Anthropic publicly acknowledged ([The Register](https://www.theregister.com/2026/03/31/anthropic_claude_code_limits/))
that Claude Code users were "hitting usage limits way faster than expected."
MacRumors documented Max subscribers whose
[5-hour quota vanished in 19 minutes](https://www.macrumors.com/2026/03/26/claude-code-users-rapid-rate-limit-drain-bug/).

The fix patches landed, but the **structural cost** of Computer Use remained.

## Where the tokens actually go

I pulled numbers from public benchmarks and my own measurements:

| Operation | Token Cost | Source |
|-----------|-----------:|--------|
| One raw screenshot to Claude | 5,000 - 15,000 | [TestCollab benchmark](https://testcollab.com/blog/playwright-cli) |
| Playwright MCP DOM per call | ~114,000 | [TestCollab benchmark](https://testcollab.com/blog/playwright-cli) |
| MCP tool schemas at startup | ~66,000 | [Paddo.dev analysis](https://paddo.dev/blog/claude-code-hidden-mcp-flag/) |
| Retry loop on identical args | unbounded | [claude-code#41659](https://github.com/anthropics/claude-code/issues/41659) |

A single "open this page, find the login button, fill in the form" task can
legitimately cost **250K-500K tokens** before any real work happens.

## The insight

**Opus doesn't need the raw screenshot. It needs to know what's on it.**

If a local model can look at the image once, extract a structured JSON summary
of the interactive elements, state flags, and visible text, then Opus can
decide the next action from that summary alone.

Same for DOM. A 114K-token HTML dump contains ~500 tokens of signal an agent
actually uses. The rest is boilerplate, ads, nav menus, analytics.

## What I built

`helix-agents` is an MCP server that Claude Code connects to. It exposes three
new tools (v0.10.0):

### `vision_compress`

```python
# Claude calls this with a screenshot path
vision_compress(image_path="/tmp/screen.png")

# Returns (400 tokens):
{
  "page_type": "login",
  "title": "Sign in to GitHub",
  "primary_action": "click Sign In button",
  "interactive_elements": [
    {"role": "input", "label": "Username", "location": "center"},
    {"role": "input", "label": "Password", "location": "center"},
    {"role": "button", "label": "Sign in", "location": "center"}
  ],
  "key_text": ["Sign in to GitHub", "Forgot password?"],
  "state_flags": {"has_error": false, "requires_auth": true, ...},
  "notes": "standard OAuth login page"
}
```

Claude makes a 500-token MCP call, gets back a 400-token structured summary.
**One screenshot: 15,000 tokens → 900 tokens total.**

### `dom_compress`

Same idea for full HTML. Extracts forms, links, buttons, main content,
next_action_candidates. Caps at 500 tokens regardless of input size.

### `retry_guard_check`

Claude Code sometimes repeats identical tool calls when it misreads errors
(see [#41659](https://github.com/anthropics/claude-code/issues/41659)). This
guard tracks call hashes per session and warns after N repeats:

```python
retry_guard_check(tool_name="navigate", args={"url": "..."})
# → {"loop_detected": true, "repeat_count": 3,
#    "recommendation": "Tool 'navigate' called 3 times with identical args.
#                       Likely stuck in retry loop. Vary args or escalate."}
```

## Measured savings

| Task | Vanilla Claude Code | With helix-agents | Savings |
|------|--------------------:|------------------:|--------:|
| Analyze one screenshot | 8K-15K tokens | ~900 tokens | **89-94%** |
| Read one web page | ~114K tokens | ~1K tokens | **99%** |
| Gmail 50-email triage | ~250K tokens | ~25K tokens | **90%** |
| GitHub repo monitoring | ~80K tokens | ~15K tokens | **81%** |

Opus still makes every decision. It just isn't asked to look at pixels.

## Architecture

```
Claude Code (Opus 4.6 — decides WHAT)
  │ MCP (cheap)
  ▼
helix-agents
  ├─ vision_compress → gemma4:31b vision → JSON
  ├─ dom_compress    → gemma4:31b text → JSON
  └─ retry_guard     → tracks call hashes → loop warnings
```

Stack: Python 3.12, FastMCP 2.0, httpx, Ollama, gemma4:31b. MIT licensed.

## Is this a Claude Code wrapper?

**No.** helix-agents is an MCP server that Claude Code connects to. It doesn't
proxy the Anthropic API or repackage Claude. It's the same pattern as any
filesystem or database MCP — fully compliant with Anthropic's TOS.

## Why gemma4:31b

- Native function calling via Ollama tools
- Built-in vision (no separate model)
- Reliable JSON output with `format: json`
- ~2s response on a single 24GB GPU
- Runs on consumer hardware

## Try it

```bash
git clone https://github.com/tsunamayo7/helix-agent
cd helix-agent && uv sync
ollama pull gemma4:31b
uv run python server.py
```

Add to `~/.claude/settings.json`:

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

## What I'd love feedback on

- Does `vision_compress` surface the right fields for *your* workflow?
- Are there Computer Use patterns where the compression loses too much signal?
- Any other recurring token hemorrhages I should target next?

308 tests passing. MIT. Stars appreciated — helps me justify more time on this.

https://github.com/tsunamayo7/helix-agent
