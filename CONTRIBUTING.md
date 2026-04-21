# Contributing to helix-agent

## Getting Started

1. Fork the repository
2. Clone your fork
3. Install dependencies: `uv sync --all-extras`
4. Run tests: `uv run pytest -q`

## Development Workflow

- Create a feature branch from `master`
- Write tests first (TDD encouraged)
- Run `uv run ruff check src/ tests/` before committing
- Ensure all tests pass: `uv run pytest -q`
- Open a Pull Request with a clear description

## Code Style

- Follow PEP 8 (enforced by ruff)
- Type hints on public functions
- Docstrings on modules and classes

## Reporting Issues

Use GitHub Issues with the provided templates.
