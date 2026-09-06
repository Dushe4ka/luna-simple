# AGENTS.md

This repository **is** Luna — a simple, lightweight CLI coding agent built on
the `deepagents` framework. When Luna runs inside this repo, this file is
loaded as memory.

## Build & test

- Environment: `uv venv --python 3.12 && uv pip install -e ".[dev,all]"`
- Tests: `uv run pytest`
- Lint: `uv run ruff check .` and `uv run ruff format --check .`
- Run locally: `uv run luna --no-splash "…"`

## Layout

- `luna/config.py` — layered settings resolution into `LunaConfig`; `config.toml` writer
- `luna/credentials.py` — API keys in `~/.config/luna/credentials.toml` (mode 0600)
- `luna/setup_wizard.py` — the interactive `luna setup` flow
- `luna/providers.py` — provider registry → LangChain chat model
- `luna/prompts.py` — the Luna system prompt
- `luna/agent.py` — `create_deep_agent` assembly (framework calls live here)
- `luna/session.py` — streaming REPL / one-shot loop, approval handling
- `luna/ui/` — `rich` theme, splash, console, approval prompt
- `luna/cli.py` — argparse entry point

## Conventions

- Python 3.11+, PEP 8 / PEP 257, `ruff` clean.
- Keep all `deepagents` / `langgraph` imports inside `luna/agent.py` and
  `luna/session.py`.
- Model IDs belong in `luna/providers.py` or config, never in agent logic.
- Tests must not hit the network — use the `FakeToolCallingModel` fixture.
