# helix-agent

**Cut Claude Code's token usage by 82–97% — automatically.** One MCP server that detects retry loops, compresses screenshots & DOM via local LLM, and auto-selects the optimal model for your GPU.

日本語README: **[README.ja.md](README.ja.md)**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-10b981.svg)](https://modelcontextprotocol.io)
[![Tests](https://img.shields.io/badge/tests-347%20passing-brightgreen.svg)](#)
[![v0.15.0](https://img.shields.io/badge/version-0.15.0-7c3aed.svg)](#)
[![MCP 3-Primitive](https://img.shields.io/badge/MCP-Tools%20%2B%20Resources%20%2B%20Prompts-10b981.svg)](#)
[![Works on 8GB VRAM](https://img.shields.io/badge/GPU-8GB%20VRAM%20OK-green.svg)](#gpu-auto-detection--model-tiers)

## Token Savings — Real Numbers

> *"My Max plan 5-hour quota vanished in 19 minutes."* — [Claude Code users](https://github.com/anthropics/claude-code/issues/16157) (666+ 👍)

helix-agent tackles the #1 pain point of Claude Code: **token waste**.

| What helix-agent does | Without | With | Reduction |
|---|---|---|---|
| **Screenshot analysis** (vision_compress) | ~15,000 tokens | ~400 tokens | **97%** |
| **DOM/HTML processing** (dom_compress) | ~114,000 tokens | ~500 tokens | **99%** |
| **Browser automation** (agent-browser) | ~15,000 tokens/action | ~1,000–2,700 | **82–93%** |
| **Retry loop prevention** (retry_guard) | ∞ (until quota dies) | Stopped at 3rd repeat | **100%** |
| **Routine tasks** (think/agent_task) | Opus tokens ($$$) | Local LLM ($0) | **100%** |

All compression runs on **your local GPU via Ollama** — zero cloud API cost.

### The problem in numbers

A typical Claude Code session burns tokens in ways you don't see ([source: 926-session audit](https://x.com/Nossa_ym/status/2041127311735402802)):

| Where tokens go | Tokens per turn | % of total |
|---|---|---|
| System prompt + MCP tool schemas | 45,000 | ~60% |
| Screenshot / DOM from Playwright MCP | 15,000–114,000 | variable |
| Conversation history rebuild | 10,000+ | grows each turn |
| **Your actual prompt** | **~500** | **<1%** |

After 22 turns (average session), that's **~1M+ tokens** — most of it overhead.

**helix-agent attacks each layer:**
- Tool schemas → use `defer_loading: true` (we document how)
- Screenshots/DOM → `vision_compress` / `dom_compress` (97-99% cut)
- Browser actions → `agent-browser` backend (82-93% cut)
- Retry loops → `retry_guard` (infinite → 0)
- Routine delegation → local LLM via `think` ($0 vs ~$0.04/call on Opus)

## Who is this for?

| If you... | helix-agent helps by... |
|---|---|
| Hit Max plan rate limits within 1–2 hours | Compressing screenshots/DOM **97–99%** before Claude sees them |
| Watch Claude repeat the same failing command 10+ times | `retry_guard` stops loops at the 3rd repeat — **automatically** |
| Pay for Opus tokens on tasks a local model could handle | Delegating reads, summaries, reviews to **Ollama ($0)** |
| Only have an 8GB GPU and think local LLMs won't help | Auto-selecting **gemma4:e2b** — proven to work at 2.7× speed |
| Want your agent to remember patterns across sessions | **Self-evolving memory** saves skills & preferences locally |

## What helix-agent does that nothing else does

| Capability | helix-agent | Alternatives |
|---|---|---|
| Screenshot → text (97% token cut) | ✅ `vision_compress` via local LLM | ❌ No MCP server does this |
| DOM → text (99% token cut) | ✅ `dom_compress` via local LLM | ❌ Playwright MCP sends raw DOM |
| Retry loop detection | ✅ `retry_guard` (sub-ms, no LLM) | ❌ Claude Code has no built-in detection |
| GPU auto-detect → model selection | ✅ 8GB to 96GB+ tiers | ❌ Other tools require manual config |
| Self-evolving memory | ✅ hermes-style SKILL.md + Qdrant | ❌ Unique to helix-agent |
| Browser 82–93% token reduction | ✅ agent-browser + fallback chain | △ agent-browser alone (no fallback) |
| All 3 MCP primitives | ✅ 23 Tools + 3 Resources + 3 Prompts | △ Most MCPs only implement Tools |

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

## GPU Auto-Detection & Model Tiers

helix-agent **detects your GPU at startup** and selects the best model for each task. Works on any NVIDIA GPU from 8GB to 96GB+.

| Your GPU | VRAM | Model Selected | DOM Compress | Memory Review |
|---|---|---|---|---|
| RTX 4060 | 8GB | gemma4:e2b | **10.2s** | **9.4s** |
| RTX 4070 Ti / 5070 Ti | 16GB | gemma4:e4b | **11.8s** | **12.3s** |
| RTX 4090 / 3090 | 24GB | gemma4:26b (MoE) | **14.7s** | **14.4s** |
| RTX PRO 6000 / A6000 | 48GB+ | gemma4:31b | 27.5s | 18.7s |

> **Key finding**: gemma4:e2b on 8GB VRAM runs **2.7× faster** than 31b with comparable output quality for compression tasks. You don't need a $2,000 GPU to save tokens.

```bash
# No configuration needed — just install a model that fits your GPU:
ollama pull gemma4:e2b   # 8GB GPU
ollama pull gemma4:e4b   # 16GB GPU
ollama pull gemma4:26b   # 24GB GPU
ollama pull gemma4:31b   # 48GB+ GPU
# helix-agent picks the right one automatically.
```

### Token savers — screenshot-to-text pipeline

The core idea: **never send raw images or HTML to Claude**. Compress them locally first.

```
┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│ Screenshot   │────→│ vision_compress  │────→│ ~400 tokens  │
│ (15K tokens) │     │ (local gemma4)   │     │ (text only)  │
└──────────────┘     └─────────────────┘     └──────────────┘

┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│ DOM/HTML     │────→│ dom_compress     │────→│ ~500 tokens  │
│ (114K tokens)│     │ (local gemma4)   │     │ (text only)  │
└──────────────┘     └─────────────────┘     └──────────────┘
```

When `computer_use(action="screenshot", analyze=True)` is called, the raw image is **automatically deleted from the response** — Claude only receives the text summary. This happens transparently, no extra configuration needed.

- **`vision_compress`** — screenshot → local vision LLM → JSON (page_type, interactive_elements, state_flags). **97% reduction.**
- **`dom_compress`** — HTML → local LLM → JSON (forms, links, buttons, next_action_candidates). **99% reduction.**

Real example (tested on RTX PRO 6000):
```
Input:  1920×1048 screenshot of X.com (would cost ~15,000 tokens)
Output: "X home feed, Japanese UI, 'For You' tab, post by @Suryansh777
         about Claude Code Resource Bible visible" (~400 tokens)
Saved:  7,362 tokens in one call
```

### Browser automation (v0.12.0)

`computer_use` routes browser actions through [Vercel's agent-browser](https://github.com/vercel-labs/agent-browser) (Rust/CDP) by default, falling back to helix-pilot → Playwright.

Measured on 50 identical automation flows:

| Backend | Tokens per action | React controlled components |
|---------|-------------------|-----------------------------|
| Playwright (screenshot+DOM) | ~15,000 | ⚠️ setValue silently reverts |
| agent-browser (accessibility tree) | ~1,000–2,700 | ✅ native keyboard events work |

### Autonomous screen verification (v0.14.0, NEW)

Claude Code's `computer_use` normally sends raw screenshots (~15,000 tokens each) back to the model. helix-agent intercepts this:

```
Action: computer_use(action="click", target="#submit")
  ↓
Verify: computer_use(action="screenshot", analyze=True)
  ↓ (raw image auto-deleted, local gemma4 analyzes)
Result: "Form submitted, success toast visible" (~400 tokens)
```

**The `instructions` field in the MCP server tells Claude Code to:**
1. Always use `vision_compress` instead of sending raw screenshots
2. Always verify actions with `analyze=True` screenshots
3. Always run `retry_guard_check` before repeating any tool call
4. Delegate routine tasks to local LLM via `think` at $0 cost

This means Claude Code **autonomously** saves tokens without any user intervention — just connect the MCP server and it works.

### Self-evolving memory (v0.14.0, NEW)

Inspired by [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent): helix-agent reviews conversations every N turns using a local LLM and **automatically saves reusable skills and insights** — at $0 cost.

- **Memory nudge**: Every 5 turns, gemma4 reviews for saveable preferences/corrections
- **Skill auto-generation**: Successful task patterns → SKILL.md files (hermes-compatible)
- **The agent gets smarter the more you use it** — all running locally

### 4-Layer Code Review Pipeline (v0.15.0, NEW)

Automated multi-LLM code review that catches **100% of issues** at ~¥30 ($0.20) total:

```
Layer 2: gemma4 ReAct review ($0, with web_search + RAG)
  ↓ findings + context
Layer 3: Sonnet 4.6 verification + cross-file analysis (~¥10)
  ↓ merged findings
Layer 4: Opus 4.6 meta-review (~¥5, reads summary only — no source code)
  ↓ final verdict
Codex:   Consultant (P1 issues only, on-demand)
```

**Empirical results** (5-model comparison on real codebase):

| Reviewer | Findings | Unique | Cost |
|----------|:--------:|:------:|:----:|
| gemma4+RAG (local) | 7 | 1 | **$0** |
| Codex GPT-5.3 | 5 | 0 | ~¥50 |
| Sonnet 4.6 | 14 | 1 | ~¥20 |
| Opus 4.6 | 16 | 4 | ~¥100 |
| **4-Layer Combined** | **16+** | **all** | **~¥30** |

> **Key finding**: gemma4 + RAG ($0) outperforms Codex GPT-5.3 (~¥50) in code review.

```python
# Daily review (gemma4 only, $0)
code_review(target="src/", skip_sonnet=True)

# Pre-release (gemma4 + Sonnet, ~¥10)
code_review(target="src/", context="payment module")

# P1 emergency (+ Codex consultant)
code_review(target="src/", codex_consult=True)
```

### gemma4 Context Expansion (v0.15.0, NEW)

gemma4 now operates as a **12-tool ReAct agent** with external knowledge access:

- **`web_search`** — Qdrant RAG search + SearXNG web search
- **`search_memory`** — enhanced with source/category filters
- **`add_memory`** — auto-categorizes into 9 categories (vtuber/coding/mcp/genai/llm/security/infra/x_ops/job)
- **Security**: 5 injection defense rules prevent execution of instructions found in search results

### Qwen3-VL 32B Vision/OCR (v0.15.0, NEW)

Dedicated vision model for **95%+ OCR accuracy** on Japanese text:

| Model | Phone number | Postal code | Cost |
|-------|:----------:|:-----------:|:----:|
| gemma4:31b | ❌ 0565-2016 | ❌ 446-8700 | $0 |
| **Qwen3-VL 32B** | **✅ 0566-76-2316** | **✅ 446-8799** | **$0** |

Auto-selected for 48GB+ GPUs. Role separation: gemma4 = code/reasoning/RAG, Qwen3-VL = vision/OCR.

### Parallel Task Execution (v0.15.1, NEW)

Run multiple tasks simultaneously with automatic model routing:

```python
parallel_tasks(tasks='[
    {"task": "Summarize this code", "type": "summarize", "context": "..."},
    {"task": "Translate to English: ...", "type": "translate"},
    {"task": "Classify these items", "type": "classify"},
    {"task": "Search for best practices", "type": "search"},
    {"task": "Security review", "type": "review", "context": "...code..."}
]')
```

**2-axis automatic model selection** — task type × input complexity:

| Input size | summarize/translate/classify | search/code_gen | review |
|---|---|---|---|
| Short (<3K chars) | gemma4:e2b (**3-6s**) | gemma4:e4b (26s) | gemma4:31b |
| Medium (3-8K) | gemma4:e4b (12s) | gemma4:31b | gemma4:31b |
| Long (>8K) | gemma4:31b (21s) | gemma4:31b | gemma4:31b |

Benchmark (5 tasks simultaneous, clip-bridge 501 lines):

| Config | Time | VRAM | Quality |
|---|---|---|---|
| **e2b+e4b mixed parallel** | **51s** | **10GB** | All 5 tasks OK |
| e4b×3 specialist parallel | 85s | 6GB | P1=2 detected |
| 31b single | 130s | 20GB | P1=2, P2=1, P3=2 |

Light tasks (e2b/e4b) run in parallel via `asyncio.gather`. Heavy tasks (31b+) run sequentially to avoid GPU contention.

### Delegation & agents

ReAct loop with tool access, context-inheriting sub-agents, background workers, Qdrant shared memory, JSONL tracing, PathGuard safety, OOM auto-fallback.

- `think` / `agent_task` / `parallel_tasks` / `fork_task` — local LLM delegation
- `see` / `browse` / `computer_use` — vision + browser
- `spawn_agent` / `send_agent_input` / `wait_agent` / `list_agents` / `close_agent`
- `search_memory` / `add_memory` — Qdrant
- `providers` / `models` / `config` / `agent_types`

## Quick Start

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent
uv sync
ollama pull gemma4:e2b   # 8GB GPU (or e4b/26b/31b for larger GPUs)
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
- [Ollama](https://ollama.ai/) + any Gemma 4 model (auto-selected by GPU):
  - 8GB VRAM: `ollama pull gemma4:e2b` (2.3B effective, 4GB)
  - 16GB VRAM: `ollama pull gemma4:e4b` (4.5B effective, 6GB)
  - 24GB VRAM: `ollama pull gemma4:26b` (MoE 3.8B active, 12GB)
  - 48GB+ VRAM: `ollama pull gemma4:31b` (30.7B dense, 20GB)

Optional:
- Qdrant (shared memory)
- Playwright (browser automation fallback)
- [agent-browser](https://github.com/vercel-labs/agent-browser) (recommended for 82-93% browser token savings)

## MCP 3-Primitive Architecture

helix-agent implements all three MCP primitives as defined by [Anthropic Academy](https://anthropic.skilljar.com/introduction-to-model-context-protocol):

| Primitive | Control | Count | Examples |
|-----------|---------|-------|----------|
| **Tools** | Model-controlled (Claude decides) | 24 | `retry_guard_check`, `think`, `computer_use`, `vision_compress`, `code_review`, `web_search` |
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
  ├─ Tools (24 total)
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
