# Changelog

All notable changes to helix-agent are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.14.0] - 2026-04-06

### Added
- **Self-evolving memory** (inspired by NousResearch/hermes-agent):
  - `evolving_memory_review` tool — reviews conversation turns via local LLM, auto-saves insights
  - `list_learned_skills` tool — lists auto-generated SKILL.md files
  - `get_skill` tool — reads learned skill content
  - Memory nudge every 5 turns, skill nudge every 8 tool calls
  - All review runs on local Ollama ($0 cost)
- **GPU auto-detection and model tiering** (`src/gpu_detect.py`):
  - Detects NVIDIA GPU VRAM via `nvidia-smi`
  - Auto-selects optimal model per task: vision, text, review, reasoning
  - 5 tiers: 8GB (e2b) → 16GB (e4b) → 24GB (26b MoE) → 48GB (31b) → 64GB+ (qwen3.5)
  - GPU info exposed in `helix://status` resource
- **Benchmark script** (`scripts/benchmark_models.py`):
  - Measures latency and output quality across all Gemma 4 variants
  - Results: e2b is 2.7× faster than 31b with comparable compression quality
- `vision_compress` and `dom_compress` now auto-select model based on GPU (no longer hardcoded to 31b)
- **Autonomous screen verification**: `computer_use(action="screenshot", analyze=True)` auto-deletes raw image, returns text summary only (97% token saving)
- **Enhanced MCP instructions**: Server instructions now tell Claude Code to proactively use vision_compress, retry_guard, and local delegation — zero user config needed

### Changed
- README.md: Added autonomous screen verification section, token savings table, GPU tier benchmarks
- README.ja.md: Full Japanese translation including autonomous verification docs
- `computer_use` docstring enhanced with token-saving flow documentation
- MCP server instructions expanded from 6 lines to comprehensive 7-section proactive usage guide
- `helix://status` resource now includes GPU info and recommended models

### Benchmarks (RTX PRO 6000)
| Model | GPU Tier | DOM Compress | Memory Review |
|-------|----------|-------------|---------------|
| gemma4:e2b | 8GB | 10.2s | 9.4s |
| gemma4:e4b | 16GB | 11.8s | 12.3s |
| gemma4:26b | 24GB | 14.7s | 14.4s |
| gemma4:31b | 48GB+ | 27.5s | 18.7s |

### Compatibility
- All 330 tests passing. No breaking changes.

## [0.13.0] - 2026-04-06

### Added
- **MCP 3-Primitive compliance**: Resources and Prompts alongside existing Tools,
  following [Anthropic Academy MCP patterns](https://anthropic.skilljar.com/introduction-to-model-context-protocol).
- **Resources** (App-Controlled, read-only):
  - `helix://status` — runtime state, browser backend, retry-guard stats, provider info
  - `helix://models` — available LLM models across all providers
  - `helix://config` — current helix-agent configuration
- **Prompts** (User-Controlled workflows):
  - `retry_report` — loop detection analysis report (Japanese)
  - `optimize_tokens` — token saving recommendations
  - `setup_guide` — first-run setup walkthrough (Japanese)

### Changed
- Architecture diagram in README updated to show all three primitives
- Added "MCP 3-Primitive" badge to README

### Compatibility
- All 322 existing tests remain green. No breaking changes.

## [0.12.0] - 2026-04-06

### Added
- **Vercel agent-browser backend** for `computer_use` actions. Rust-native CDP
  automation returns an accessibility tree snapshot instead of screenshot+DOM,
  cutting per-action tokens by 82–93% in benchmarks (n=50 flows).
- `AgentBrowserSession` wrapper (`src/agent_browser_session.py`) exposing
  `navigate` / `click` / `type_text` / `fill` / `keyboard_type` /
  `scroll` / `screenshot` / `read_page` / `press`.
- New `prefer_agent_browser: bool = True` kwarg on `ComputerUseHandler`.
- 10 new tests covering agent-browser wrapper behavior.

### Changed
- `computer_use` backend priority is now
  **agent-browser → helix-pilot → playwright** (was pilot → playwright).
- When agent-browser's `fill` is used on React controlled components
  (Wantedly, LinkedIn, Greenhouse), native keyboard events fire and the
  input is accepted. Playwright's `setValue()` path silently reverted
  on these sites.
- Windows subprocess invocation for `agent-browser` uses
  `asyncio.create_subprocess_shell` because npm installs a `.cmd`
  wrapper.

### Compatibility
- All 312 pre-existing tests remain green. Total **322 tests passing**.
- Opt out with `ComputerUseHandler(prefer_agent_browser=False)` to keep
  the pre-v0.12.0 pilot/playwright path.

### Notes
- agent-browser is Apache 2.0 licensed; it ships its own Chromium and
  can use a separate profile directory, so your daily Chrome session
  is untouched.

## [0.11.0] - 2026-04-05

### Added
- `retry_guard_check` / `retry_guard_status` / `retry_guard_reset`
  tools. SHA1-fingerprinted call history with sliding window; pure
  logic, sub-millisecond, no LLM required.
- Background: anthropics/claude-code#41659 and
  https://www.macrumors.com/2026/03/26/claude-code-users-rapid-rate-limit-drain-bug/

## [0.10.0 and earlier]

See git history for earlier releases
(vision/DOM compressors, delegation/ReAct loop, spawn/wait sub-agents,
Qdrant memory, JSONL tracing, PathGuard, OOM auto-fallback,
Japanese input helper).
