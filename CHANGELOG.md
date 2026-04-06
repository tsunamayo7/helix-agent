# Changelog

All notable changes to helix-agent are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
