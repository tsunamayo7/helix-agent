# Contributing to helix-agent

## Setup

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent
uv sync
```

## Local Checks

Run the focused test suite:

```bash
python -m pytest tests/test_agent.py tests/test_router.py tests/test_react_loop.py tests/test_ollama_client.py -q
```

Run the full suite if your environment allows temporary file creation cleanly:

```bash
python -m pytest tests -q
```

## Scope

Good contributions include:

- provider improvements
- routing heuristics
- better background agent summaries
- documentation and examples
- additional verification coverage

## Pull Requests

- Keep changes narrow and reviewable
- Include tests when behavior changes
- Update `README.md` when public behavior changes
- Call out provider-specific limitations clearly
