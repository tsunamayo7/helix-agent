# Changelog

All notable changes to helix-agent are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.15.1] - 2026-04-08

### Added
- **`parallel_tasks` MCP tool** — execute multiple tasks simultaneously with automatic model routing
- **2-axis automatic model selection** — task type × input complexity → optimal Gemma 4 variant (e2b / e4b / 26b / 31b)
- `asyncio.gather`-based parallel execution for light tasks (e2b/e4b), sequential for heavy tasks (31b+) to avoid GPU contention
- Benchmark: 5 parallel tasks on 501-line input — e2b+e4b mixed **51 s / 10 GB VRAM** (vs 31b single 130 s / 20 GB)

### Changed
- README.md / README.ja.md: added parallel_tasks section with benchmarks and 2-axis model selection matrix

## [0.15.0] - 2026-04-07

### Added
- **4-Layer Code Review Pipeline** (`code_review` MCP tool):
  - Layer 2: gemma4 ReAct review with `web_search` + RAG ($0)
  - Layer 3: Sonnet 4.6 verification + cross-file analysis (~¥10)
  - Layer 4: Opus 4.6 meta-review — summary-only, no source code (~¥5)
  - Codex consultant for P1 issues (on-demand)
  - Empirical: gemma4+RAG ($0) outperforms Codex GPT-5.3 (~¥50) in finding uniqueness
  - `skip_sonnet=True` for daily $0 reviews, `codex_consult=True` for release gates
- **Codex reasoning effort control** (`src/provider_runtime.py`):
  - `VALID_CODEX_EFFORTS = {"none","minimal","low","medium","high","xhigh"}`
  - `codex_effort` parameter on `code_review`, `think(provider="codex")`, `agent_task(provider="codex")`
  - Default: `high`
  - **Auto-escalation**: when the 4-layer pipeline detects ≥3 P1 issues, Codex is invoked with `xhigh` automatically (`_consult_codex`)
- **gemma4 ReAct context expansion** — gemma4 now operates as a 12-tool ReAct agent with `web_search` (Qdrant RAG + SearXNG), filtered `search_memory`, 9-category `add_memory`, and 5-rule injection defense
- **Qwen3-VL 32B Vision/OCR** — dedicated vision model auto-selected on 48 GB+ GPUs, 95%+ OCR accuracy on Japanese text (phone numbers, postal codes, handwritten forms)
- **Department RAG (dept_*)** — per-department Qdrant collections `dept_hr`/`dept_research`/`dept_design`/`dept_build`/`dept_qa` + `mem0_shared`, exposed via `dept_search` / `dept_store` MCP tools
- **Autonomous operations harness** (`scripts/`):
  - `system_auditor.py` — periodic integrity and drift audit across memory, hooks, services
  - `anomaly_dispatcher.py` — routes detected anomalies to the right department / agent
  - `env_self_heal.py` — auto-repairs common environment regressions (services, paths, dependencies)
  - `critical_files_guard.py` — SHA-256 snapshot protection for `CLAUDE.md`, `settings.json`, etc. (30 generations)
  - `helix_overview.py` — single-command 9-domain overview for the operator or Claude itself
  - `dept_feed_bridge.py` / `dept_dataset_builder.py` / `dept_ft_advisor.py` — department RAG growth → dataset → LoRA fine-tuning pipeline
  - `supervisor.py` — watches 9 resident daemons, auto-restart on failure
  - Audit → dispatch → heal chain under Windows Task Scheduler

### Changed
- Total MCP tool count: **23 → 27** (added `parallel_tasks`, `dept_search`, `dept_store`, `code_review`)
- Test suite: **312 → 347 passing**
- README.md / README.ja.md: added 4-Layer Code Review section, Codex effort documentation, autonomous operations section, MCP tool category list

### Fixed
- P1 bugs surfaced via 4-layer review pipeline (cross-file analysis)

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
