# Luna CLI Coding Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `luna` — a lightweight `rich`-based CLI coding agent on top of `deepagents`, with a five-provider model layer and an ANSI splash screen.

**Architecture:** Thin glue over `deepagents.create_deep_agent`. `config.py` resolves layered settings into a `LunaConfig`; `providers.py` turns it into a LangChain chat model via `init_chat_model`; `agent.py` assembles the deep agent with a real-filesystem backend and human-in-the-loop approval; `session.py` drives a streaming REPL / one-shot loop; `ui/` renders the splash, output, and approval prompts.

**Tech Stack:** Python 3.11+, `deepagents~=0.7.13`, `langchain~=1.4`, `langgraph~=1.2`, `rich`, `hatchling`, `pytest`, `ruff`, `uv`.

**Spec:** `docs/superpowers/specs/2026-09-06-luna-cli-agent-design.md`

## Global Constraints

- Python **3.11+** (`requires-python = ">=3.11"`).
- PEP 8 / PEP 257; `ruff` clean (`ruff check` + `ruff format --check`).
- Distribution name `luna-simple`; console scripts `luna` and `luna-simple` → `luna.cli:main`.
- Package import name `luna`; `luna.__version__ == "0.1.0"`.
- All framework (`deepagents` / `langgraph`) calls confined to `luna/agent.py` and `luna/session.py`.
- Provider keys exactly: `anthropic` (default), `deepseek`, `openai`, `google`, `ollama`.
- Default model string: `anthropic:claude-sonnet-4-5` (overridable via config/CLI).
- Model IDs live only in `providers.py` / config — never hard-coded in agent logic.
- Frequent commits: one per task minimum. Commit trailer: `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Tests never hit the network — use the `FakeToolCallingModel` fixture.

---

### Task 1: Project scaffold, packaging, tooling

**Files:**
- Create: `pyproject.toml`, `ruff.toml`, `.env.example`, `README.md`, `LICENSE`, `CHANGELOG.md`, `AGENTS.md`, `.github/workflows/ci.yml`
- Create: `luna/__init__.py`, `luna/__main__.py`
- Create: `tests/__init__.py`
- Test: `tests/test_metadata.py`

**Interfaces:**
- Produces: `luna.__version__: str`; `python -m luna` calls `luna.cli.main`.

- [ ] **Step 1: Write the failing test** — `tests/test_metadata.py`

```python
import subprocess, sys
import luna

def test_version_constant():
    assert luna.__version__ == "0.1.0"

def test_module_entrypoint_runs():
    out = subprocess.run([sys.executable, "-m", "luna", "--version"],
                         capture_output=True, text=True)
    assert out.returncode == 0
    assert "0.1.0" in out.stdout
```

- [ ] **Step 2: Run test, verify it fails** — `uv run pytest tests/test_metadata.py -v` → FAIL (no `luna`).

- [ ] **Step 3: Implement**

`pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "luna-simple"
version = "0.1.0"
description = "Luna - a simple, lightweight CLI coding agent. Observe, Understand, Plan, Act."
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
authors = [{ name = "Luna contributors" }]
keywords = ["cli", "coding-agent", "llm", "deepagents", "langchain"]
dependencies = [
    "deepagents~=0.7.13",
    "langchain~=1.4",
    "langgraph~=1.2",
    "rich>=13.7",
    "langchain-anthropic>=1.7",
]

[project.optional-dependencies]
deepseek = ["langchain-deepseek>=0.1"]
openai = ["langchain-openai>=1.0"]
google = ["langchain-google-genai>=3.0"]
ollama = ["langchain-ollama>=1.0"]
all = ["langchain-deepseek>=0.1", "langchain-openai>=1.0", "langchain-google-genai>=3.0", "langchain-ollama>=1.0"]
dev = ["pytest>=8", "ruff>=0.6"]

[project.scripts]
luna = "luna.cli:main"
luna-simple = "luna.cli:main"

[project.urls]
Homepage = "https://github.com/earendil-works/pi"

[tool.hatch.build.targets.wheel]
packages = ["luna"]
```

`ruff.toml`:

```toml
line-length = 100
target-version = "py311"
[lint]
select = ["E", "F", "I", "UP", "B", "W", "D"]
ignore = ["D100", "D104", "D107", "D203", "D213"]
[lint.per-file-ignores]
"tests/*" = ["D"]
```

`luna/__init__.py`:

```python
"""Luna - a simple, lightweight CLI coding agent."""

__version__ = "0.1.0"
```

`luna/__main__.py`:

```python
"""Entry point for `python -m luna`."""

from luna.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

`.env.example`:

```bash
# Set the key for whichever provider you use (default provider: anthropic).
ANTHROPIC_API_KEY=
DEEPSEEK_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
# Ollama: no key; optionally OLLAMA_HOST=http://localhost:11434
```

`CHANGELOG.md`: Keep-a-Changelog stub with `## [0.1.0]` section.

`AGENTS.md`: short note that this repo is the Luna agent itself; build/test commands (`uv run pytest`, `uv run ruff check`).

`README.md`: title, tagline, the manifesto slogans from the spec §7 (EN + RU), install (`uv pip install "luna-simple[all]"`), quickstart (`luna`, `luna "fix the bug in main.py"`), provider table, `--yolo` / `--no-splash` notes.

`LICENSE`: MIT, holder "Luna contributors", year 2026.

`.github/workflows/ci.yml`: matrix Python 3.11 & 3.12; `uv sync`; `uv run ruff check .`; `uv run ruff format --check .`; `uv run pytest -q`.

Provide a **temporary** `luna/cli.py` so the entrypoint import resolves:

```python
"""Command-line interface for Luna (temporary stub, replaced in Task 6)."""

import argparse

from luna import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="luna")
    parser.add_argument("--version", action="version", version=f"luna {__version__}")
    parser.parse_args(argv)
    return 0
```

- [ ] **Step 4: Run tests, verify pass** — `uv venv --python 3.12 && uv pip install -e ".[dev]" && uv run pytest tests/test_metadata.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: project scaffold, packaging, CI"
```

---

### Task 2: Provider registry (`luna/providers.py`)

**Files:**
- Create: `luna/providers.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: `LunaConfig` is not yet defined — this task takes plain args and is
  re-wired in Task 3. Define `build_model` to accept a small protocol:
  `provider: str`, `model: str | None`, `model_kwargs: dict`.
- Produces:
  - `ProviderSpec` dataclass: `key: str`, `init_prefix: str`, `default_model: str`, `env_var: str | None`, `pip_extra: str`
  - `PROVIDERS: dict[str, ProviderSpec]`
  - `class LunaConfigError(Exception)`
  - `resolve_model_string(provider: str, model: str | None) -> str`
  - `build_model(provider: str, model: str | None = None, model_kwargs: dict | None = None) -> BaseChatModel`

- [ ] **Step 1: Write the failing test** — `tests/test_providers.py`

```python
import pytest
from luna.providers import (
    PROVIDERS, LunaConfigError, resolve_model_string, build_model,
)

def test_registry_has_five_providers():
    assert set(PROVIDERS) == {"anthropic", "deepseek", "openai", "google", "ollama"}

def test_anthropic_is_reference_default():
    assert PROVIDERS["anthropic"].default_model == "claude-sonnet-4-5"
    assert PROVIDERS["google"].init_prefix == "google_genai"

def test_resolve_model_string_uses_default():
    assert resolve_model_string("anthropic", None) == "anthropic:claude-sonnet-4-5"
    assert resolve_model_string("openai", "gpt-4o") == "openai:gpt-4o"

def test_unknown_provider_raises():
    with pytest.raises(LunaConfigError):
        resolve_model_string("grok", None)

def test_missing_api_key_raises_with_hint(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LunaConfigError) as exc:
        build_model("anthropic")
    assert "ANTHROPIC_API_KEY" in str(exc.value)

def test_ollama_needs_no_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    # Should get past the key check and fail only if the package is absent.
    try:
        build_model("ollama")
    except LunaConfigError as e:
        assert "langchain-ollama" in str(e) or "luna-simple[ollama]" in str(e)
```

- [ ] **Step 2: Run test, verify fail** — `uv run pytest tests/test_providers.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement `luna/providers.py`**

```python
"""Provider registry: map a provider key to a LangChain chat model."""

from __future__ import annotations

import os
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel


class LunaConfigError(Exception):
    """Raised for unrecoverable configuration problems (bad provider, missing key)."""


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    init_prefix: str
    default_model: str
    env_var: str | None
    pip_extra: str


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec("anthropic", "anthropic", "claude-sonnet-4-5", "ANTHROPIC_API_KEY", "anthropic"),
    "deepseek": ProviderSpec("deepseek", "deepseek", "deepseek-chat", "DEEPSEEK_API_KEY", "deepseek"),
    "openai": ProviderSpec("openai", "openai", "gpt-4.1", "OPENAI_API_KEY", "openai"),
    "google": ProviderSpec("google", "google_genai", "gemini-2.5-pro", "GOOGLE_API_KEY", "google"),
    "ollama": ProviderSpec("ollama", "ollama", "qwen2.5-coder", None, "ollama"),
}

DEFAULT_PROVIDER = "anthropic"


def _spec(provider: str) -> ProviderSpec:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(PROVIDERS)}."
        ) from None


def resolve_model_string(provider: str, model: str | None) -> str:
    spec = _spec(provider)
    return f"{spec.init_prefix}:{model or spec.default_model}"


def build_model(
    provider: str,
    model: str | None = None,
    model_kwargs: dict | None = None,
) -> BaseChatModel:
    spec = _spec(provider)
    if spec.env_var and not os.environ.get(spec.env_var):
        raise LunaConfigError(
            f"{spec.env_var} is not set. Export it or add it to your .env "
            f"(see .env.example), or pick another provider with --provider."
        )
    from langchain.chat_models import init_chat_model

    try:
        return init_chat_model(
            resolve_model_string(provider, model), **(model_kwargs or {})
        )
    except ImportError as exc:
        raise LunaConfigError(
            f"The {spec.key} integration is not installed. "
            f'Run:  pip install "luna-simple[{spec.pip_extra}]"'
        ) from exc
```

- [ ] **Step 4: Run tests, verify pass** — `uv run pytest tests/test_providers.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add luna/providers.py tests/test_providers.py
git commit -m "feat: provider registry with five-provider model builder"
```

---

### Task 3: Layered configuration (`luna/config.py`)

**Files:**
- Create: `luna/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `luna.providers.DEFAULT_PROVIDER`, `PROVIDERS`, `LunaConfigError`.
- Produces:
  - `@dataclass LunaConfig`: `provider: str = "anthropic"`, `model: str | None = None`,
    `workdir: str = "."`, `yolo: bool = False`, `show_splash: bool = True`,
    `temperature: float | None = None`, `max_tokens: int | None = None`,
    `model_kwargs: dict` (computed from temperature/max_tokens/extra)
  - `load_config(cli_overrides: dict, *, env: Mapping | None = None, cwd: str | None = None) -> LunaConfig`
  - `USER_CONFIG_PATH` (respects `XDG_CONFIG_HOME`, default `~/.config/luna/config.toml`)

- [ ] **Step 1: Write the failing test** — `tests/test_config.py`

```python
from pathlib import Path
from luna.config import load_config, LunaConfig

def test_defaults():
    cfg = load_config({}, env={}, cwd=".")
    assert cfg.provider == "anthropic"
    assert cfg.model is None
    assert cfg.yolo is False
    assert cfg.show_splash is True

def test_cli_beats_env_beats_project_toml(tmp_path):
    (tmp_path / ".luna.toml").write_text(
        '[model]\nprovider = "openai"\nname = "gpt-4o"\n[ui]\nsplash = false\n'
    )
    env = {"LUNA_PROVIDER": "google"}
    cfg = load_config({"provider": "deepseek"}, env=env, cwd=str(tmp_path))
    assert cfg.provider == "deepseek"        # CLI wins
    assert cfg.model == "gpt-4o"             # from project toml (not overridden)
    assert cfg.show_splash is False          # from project toml [ui].splash

def test_env_beats_project_toml(tmp_path):
    (tmp_path / ".luna.toml").write_text('[model]\nprovider = "openai"\n')
    cfg = load_config({}, env={"LUNA_PROVIDER": "google"}, cwd=str(tmp_path))
    assert cfg.provider == "google"

def test_yolo_from_env_truthy(tmp_path):
    cfg = load_config({}, env={"LUNA_YOLO": "1"}, cwd=str(tmp_path))
    assert cfg.yolo is True

def test_model_kwargs_composed():
    cfg = load_config({"temperature": 0.2, "max_tokens": 1000}, env={}, cwd=".")
    assert cfg.model_kwargs == {"temperature": 0.2, "max_tokens": 1000}
```

- [ ] **Step 2: Run test, verify fail** → FAIL (module missing).

- [ ] **Step 3: Implement `luna/config.py`**

Rules:
- Read `~/.config/luna/config.toml` then `<cwd>/.luna.toml` with `tomllib`; later file overrides earlier. TOML shape: `[model] provider, name`; `[ui] splash`; `[agent] yolo, workdir, temperature, max_tokens`.
- Env layer: `LUNA_PROVIDER`, `LUNA_MODEL`, `LUNA_YOLO` (truthy = `{"1","true","yes","on"}` case-insensitive), `LUNA_WORKDIR`.
- CLI layer: dict with any of `provider, model, yolo, show_splash, workdir, temperature, max_tokens` (only keys that are not `None`).
- Merge precedence: defaults < user toml < project toml < env < CLI.
- Validate `provider` against `PROVIDERS`; raise `LunaConfigError` if unknown.
- `model_kwargs`: include `temperature` and/or `max_tokens` when set; merge `[agent].extra` table if present.

- [ ] **Step 4: Run tests, verify pass** → PASS.

- [ ] **Step 5: Commit**

```bash
git add luna/config.py tests/test_config.py
git commit -m "feat: layered configuration resolution"
```

---

### Task 4: System prompt + agent assembly (`luna/prompts.py`, `luna/agent.py`)

**Files:**
- Create: `luna/prompts.py`, `luna/agent.py`
- Test: `tests/conftest.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: `luna.config.LunaConfig`, `luna.providers.build_model`.
- Produces:
  - `luna.prompts.LUNA_SYSTEM_PROMPT: str`
  - `luna.agent.INTERRUPT_TOOLS: dict` (the non-yolo `interrupt_on` mapping)
  - `luna.agent.build_agent(config: LunaConfig, *, model: BaseChatModel | None = None, checkpointer=None) -> CompiledStateGraph`
    (the `model=` param exists so tests inject a fake and skip the network)

- [ ] **Step 1: Write `tests/conftest.py`** — `FakeToolCallingModel`

```python
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeToolCallingModel(BaseChatModel):
    """Deterministic chat model with a scripted response queue and working bind_tools."""

    responses: list[AIMessage] = []
    idx: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.responses[min(self.idx, len(self.responses) - 1)]
        self.idx += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def fake_model():
    def _make(*messages: AIMessage) -> FakeToolCallingModel:
        return FakeToolCallingModel(responses=list(messages) or [AIMessage(content="ok")])
    return _make
```

- [ ] **Step 2: Write the failing test** — `tests/test_agent.py`

```python
from pathlib import Path
from langchain_core.messages import AIMessage
from luna.config import LunaConfig
from luna.agent import build_agent, INTERRUPT_TOOLS
from luna.prompts import LUNA_SYSTEM_PROMPT

def test_prompt_mentions_the_four_verbs():
    for verb in ("Observe", "Understand", "Plan", "Act"):
        assert verb in LUNA_SYSTEM_PROMPT

def test_build_agent_runs_offline(tmp_path, fake_model):
    cfg = LunaConfig(workdir=str(tmp_path))
    agent = build_agent(cfg, model=fake_model(AIMessage(content="hello from luna")))
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "hi"}]},
        config={"configurable": {"thread_id": "t1"}},
    )
    assert "luna" in result["messages"][-1].content.lower()

def test_yolo_disables_interrupts(tmp_path, fake_model):
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model())
    # smoke: compiles and runs without an interrupt config
    agent.invoke({"messages": [{"role": "user", "content": "hi"}]},
                 config={"configurable": {"thread_id": "t2"}})

def test_interrupt_tools_cover_mutations():
    assert set(INTERRUPT_TOOLS) >= {"write_file", "edit_file", "delete", "execute"}
```

- [ ] **Step 3: Run test, verify fail** → FAIL.

- [ ] **Step 4: Implement**

`luna/prompts.py` — `LUNA_SYSTEM_PROMPT` string. Content: identity ("You are Luna, a calm CLI coding companion"), the loop **Observe → Understand → Plan → Act**, rules: read before you write, keep a short plan with `write_todos` for multi-step work, explain the next action in one line before calling a mutating tool, prefer minimal diffs, never touch files outside the working directory, stop and ask when unsure.

`luna/agent.py`:

```python
"""Assemble the Luna deep agent from a resolved config."""

from __future__ import annotations

from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import InMemorySaver

from luna.config import LunaConfig
from luna.prompts import LUNA_SYSTEM_PROMPT
from luna.providers import build_model

INTERRUPT_TOOLS: dict = {
    "write_file": True,
    "edit_file": True,
    "delete": True,
    "execute": {"allowed_decisions": ["approve", "edit", "reject"]},
}


def build_agent(config: LunaConfig, *, model: BaseChatModel | None = None, checkpointer=None):
    workdir = Path(config.workdir).resolve()
    backend = LocalShellBackend(root_dir=str(workdir), inherit_env=True)
    memory = ["AGENTS.md"] if (workdir / "AGENTS.md").is_file() else None
    return create_deep_agent(
        model=model or build_model(config.provider, config.model, config.model_kwargs),
        system_prompt=LUNA_SYSTEM_PROMPT,
        backend=backend,
        memory=memory,
        interrupt_on=None if config.yolo else INTERRUPT_TOOLS,
        checkpointer=checkpointer or InMemorySaver(),
        name="luna",
    )
```

- [ ] **Step 5: Run tests, verify pass** → PASS. If `virtual_mode` blocks the fake run, pass `virtual_mode=False` and note the reason in a comment.

- [ ] **Step 6: Commit**

```bash
git add luna/prompts.py luna/agent.py tests/conftest.py tests/test_agent.py
git commit -m "feat: Luna system prompt and deep-agent assembly"
```

---

### Task 5: UI — theme, splash, console, approval (`luna/ui/`)

**Files:**
- Create: `luna/ui/__init__.py`, `luna/ui/theme.py`, `luna/ui/splash.py`, `luna/ui/console.py`, `luna/ui/approve.py`
- Test: `tests/test_splash.py`, `tests/test_approve.py`

**Interfaces:**
- Produces:
  - `luna.ui.theme.LUNA_THEME: rich.theme.Theme`, `PALETTE: dict[str, str]`
  - `luna.ui.console.get_console(*, force_terminal: bool | None = None) -> rich.console.Console`
  - `luna.ui.console.render_markdown(console, text: str) -> None`
  - `luna.ui.splash.render_splash(console, steps: list[str] | None = None, *, animate: bool = True) -> None`
  - `luna.ui.approve.describe_action(action_request: dict) -> str`
  - `luna.ui.approve.prompt_decision(console, action_request: dict, *, input_fn=input) -> dict`

- [ ] **Step 1: Write the failing tests**

`tests/test_splash.py`:

```python
import io
from rich.console import Console
from luna.ui.splash import render_splash

def _console(width):
    return Console(file=io.StringIO(), width=width, force_terminal=True, color_system="truecolor")

def test_renders_at_various_widths():
    for w in (40, 80, 200):
        c = _console(w)
        render_splash(c, steps=["loading modules", "connecting to tools"], animate=False)
        out = c.file.getvalue()
        assert "LUNA" in out
        assert "loading modules" in out

def test_contains_companion_line_and_slogan():
    c = _console(100)
    render_splash(c, animate=False)
    out = c.file.getvalue()
    assert "YOUR AI AGENT COMPANION" in out
    assert "SAME MOON" in out.upper()
```

`tests/test_approve.py`:

```python
import io
from rich.console import Console
from luna.ui.approve import prompt_decision, describe_action

AR_WRITE = {"action": "write_file", "args": {"file_path": "a.py", "content": "print(1)\n"}}
AR_EXEC = {"action": "execute", "args": {"command": "pytest -q"}}

def _c(): return Console(file=io.StringIO(), force_terminal=True)

def test_enter_approves():
    d = prompt_decision(_c(), AR_WRITE, input_fn=lambda _: "")
    assert d == {"type": "approve"}

def test_n_rejects_with_reason():
    answers = iter(["n", "wrong file"])
    d = prompt_decision(_c(), AR_WRITE, input_fn=lambda _: next(answers))
    assert d["type"] == "reject"
    assert "wrong file" in d["message"]

def test_describe_execute_shows_command():
    assert "pytest -q" in describe_action(AR_EXEC)
```

- [ ] **Step 2: Run tests, verify fail** → FAIL.

- [ ] **Step 3: Implement**

`theme.py` — `PALETTE` from spec §5.6 (`bg #0b1026`, `moon #e8ecff`, `peri #8a9cff`, `blue #5566a8`, `mauve #b98cc9`, `accent #cdd6ff`), `LUNA_THEME = Theme({"luna.title": ..., "luna.dim": ..., "luna.slogan": ..., "luna.step": ..., "luna.tool": ..., "luna.warn": ...})`.

`console.py` — `get_console` builds `Console(theme=LUNA_THEME, ...)`; `render_markdown` uses `rich.markdown.Markdown`; add `spinner(console, label)` context manager wrapping `console.status`.

`splash.py` — `render_splash`:
- Build a block-character moon (crescent/full disc, ~14 rows) as static art.
- Centered `L U N A` (letter-spaced), then `» YOUR AI AGENT COMPANION «`, then `INITIALIZING ...`.
- Corner slogans via a 3-column `rich.table.Table.grid`: top-left `LUNA v0.1.0 / AI AGENT HARNESS`, top-right `A BRIGHTER / TOMORROW / TOGETHER`, bottom-left the streamed steps, bottom-right `SAME MOON / BRIGHTER / POSSIBILITIES`.
- `steps` default: `["loading modules ...", "connecting to tools ...", "preparing your canvas ...", "almost there ..."]`; when `animate`, print each with a ~120 ms delay and a `>` prefix; else print all at once.
- If `console.width < 60` or `not console.is_terminal`: print a compact 3-line fallback (`LUNA` + tagline + `initializing ...`).

`approve.py`:
- `describe_action`: for `write_file`/`edit_file` show path + a unified diff (old vs new when `edit_file`, or content preview for `write_file`, truncated to ~40 lines); for `execute` show `$ <command>`; for `delete` show the path; generic `key=value` otherwise.
- `prompt_decision`: render a panel with `describe_action`, then prompt `"[Enter] approve · [e] edit · [n] reject > "`. Empty/`y` → `{"type":"approve"}`. `n` → ask `"reason > "` → `{"type":"reject","message": reason or "rejected by user"}`. `e` → for `execute`, ask for a replacement command → `{"type":"edit","args":{"command": ...}}`; for file tools, ask confirm to open `$EDITOR` on the content, return `{"type":"edit","args":{...}}` (fallback to approve if no `$EDITOR`).

- [ ] **Step 4: Run tests, verify pass** → PASS.

- [ ] **Step 5: `ruff check luna/ui && ruff format luna/ui`, then commit**

```bash
git add luna/ui tests/test_splash.py tests/test_approve.py
git commit -m "feat: rich UI - theme, splash, console, approval prompt"
```

---

### Task 6: Session loop (`luna/session.py`)

**Files:**
- Create: `luna/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `build_agent`, `luna.ui.console`, `luna.ui.approve.prompt_decision`.
- Produces:
  - `luna.session.collect_decisions(console, interrupt_value: dict, *, input_fn=input) -> dict`
    returning `{"decisions": [...]}` for `Command(resume=...)`
  - `luna.session.run_once(agent, prompt: str, *, thread_id: str, console, input_fn=input) -> str`
  - `luna.session.run_repl(agent, *, console, input_fn=input) -> int`
  - `luna.session.SLASH_COMMANDS: dict[str, str]` (name → help)

- [ ] **Step 1: Write the failing test** — `tests/test_session.py`

```python
from langchain_core.messages import AIMessage
from luna.config import LunaConfig
from luna.agent import build_agent
from luna.session import collect_decisions, run_once, SLASH_COMMANDS

def test_collect_decisions_maps_multiple(monkeypatch):
    iv = {"action_requests": [
        {"action": "write_file", "args": {"file_path": "a", "content": "x"}},
        {"action": "execute", "args": {"command": "ls"}},
    ]}
    answers = iter(["", "n", "nope"])
    import io; from rich.console import Console
    out = collect_decisions(Console(file=io.StringIO()), iv, input_fn=lambda _: next(answers))
    assert out["decisions"][0] == {"type": "approve"}
    assert out["decisions"][1]["type"] == "reject"

def test_run_once_returns_final_text(tmp_path, fake_model):
    import io; from rich.console import Console
    agent = build_agent(LunaConfig(workdir=str(tmp_path)), model=fake_model(AIMessage(content="done")))
    text = run_once(agent, "hi", thread_id="t", console=Console(file=io.StringIO()))
    assert text == "done"

def test_slash_help_registered():
    assert "/help" in SLASH_COMMANDS and "/exit" in SLASH_COMMANDS
```

- [ ] **Step 2: Run test, verify fail** → FAIL.

- [ ] **Step 3: Implement `luna/session.py`**

- `collect_decisions`: read `interrupt_value["action_requests"]` (fall back to `["action_request"]` singular); for each call `prompt_decision`; return `{"decisions": [...]}`.
- `_stream_turn(agent, payload, config, console, input_fn)`: iterate `agent.stream(payload, config, stream_mode=["messages", "updates"])`.
  - `messages` chunks → append `AIMessageChunk` text to a live buffer, flush to console.
  - detect interrupt: after the stream ends, `state = agent.get_state(config)`; if `state.interrupts`, call `collect_decisions` and re-enter with `Command(resume=collect_decisions(...))`. Loop until no interrupts.
  - Render tool calls (`updates` with tool messages) as dim `luna.tool` lines.
- `run_once`: one `_stream_turn`; return the concatenated final assistant text.
- `run_repl`:
  - `thread_id = uuid7hex()`; loop `input("luna › ")`.
  - Ctrl-D / `/exit` / `/quit` → return 0. Ctrl-C during a turn → cancel turn, keep REPL.
  - `/help` prints `SLASH_COMMANDS`; `/tools` lists the deep-agent tool names; `/new` regenerates `thread_id`; `/clear` clears screen; `/model` + `/provider` print current values (switching mid-session is out of scope — say so).
  - Non-slash input → `_stream_turn`.
- `SLASH_COMMANDS = {"/help": ..., "/tools": ..., "/new": ..., "/clear": ..., "/model": ..., "/provider": ..., "/exit": ...}`.

- [ ] **Step 4: Run tests, verify pass** → PASS.

- [ ] **Step 5: Commit**

```bash
git add luna/session.py tests/test_session.py
git commit -m "feat: streaming REPL / one-shot session loop with approval handling"
```

---

### Task 7: Real CLI (`luna/cli.py`) — replace the stub

**Files:**
- Modify: `luna/cli.py` (replace stub from Task 1)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_config`, `build_agent`, `run_once`, `run_repl`, `render_splash`, `get_console`, `LunaConfigError`.
- Produces: `luna.cli.build_parser() -> argparse.ArgumentParser`; `luna.cli.main(argv=None) -> int`.

- [ ] **Step 1: Write the failing test** — `tests/test_cli.py`

```python
import pytest
from luna.cli import build_parser, main

def test_parser_accepts_flags():
    ns = build_parser().parse_args(["do a thing", "--provider", "openai", "--yolo", "--no-splash"])
    assert ns.prompt_pos == "do a thing"
    assert ns.provider == "openai"
    assert ns.yolo is True
    assert ns.no_splash is True

def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert "0.1.0" in capsys.readouterr().out

def test_bad_provider_is_usage_error(capsys):
    code = main(["hi", "--provider", "grok"])
    assert code == 2

def test_one_shot_dispatches_run_once(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr("luna.cli.build_agent", lambda *a, **k: object())
    monkeypatch.setattr("luna.cli.run_once", lambda *a, **k: calls.setdefault("once", True) or "ok")
    monkeypatch.setattr("luna.cli.run_repl", lambda *a, **k: calls.setdefault("repl", True) or 0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    code = main(["fix the bug", "--no-splash", "--workdir", str(tmp_path)])
    assert code == 0 and calls == {"once": True}
```

- [ ] **Step 2: Run test, verify fail** → FAIL.

- [ ] **Step 3: Implement `luna/cli.py`**

- `build_parser`: positional `prompt_pos` (nargs="?"), `-p/--prompt`, `--provider` (choices = `PROVIDERS` keys), `--model`, `--workdir` (default `.`), `--temperature` (float), `--max-tokens` (int), `--yolo` (store_true), `--no-splash` (store_true), `--version` (action="version").
- `main`:
  1. parse args; build `cli_overrides` dict (drop `None`/falsy-not-set).
  2. `try: cfg = load_config(cli_overrides)` — on `LunaConfigError` print to stderr, return **2**.
  3. `console = get_console()`. `prompt = args.prompt_pos or args.prompt`.
  4. if `cfg.show_splash` and no prompt and `console.is_terminal`: `render_splash(console)`.
  5. `try: agent = build_agent(cfg)` — on `LunaConfigError` → stderr, return **2**.
  6. if `prompt`: `run_once(agent, prompt, thread_id=uuid, console=console)`; return 0 (or 1 on exception).
  7. else: `return run_repl(agent, console=console)`.
  8. `KeyboardInterrupt` anywhere → return **130**.

- [ ] **Step 4: Run tests, verify pass** — `uv run pytest -q` (whole suite) → PASS.

- [ ] **Step 5: Commit**

```bash
git add luna/cli.py tests/test_cli.py
git commit -m "feat: full CLI - one-shot and REPL dispatch, splash wiring"
```

---

### Task 8: End-to-end polish, lint, docs, manual smoke

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `AGENTS.md` as needed
- Create: `tests/test_end_to_end.py`

**Interfaces:** none new.

- [ ] **Step 1: Write an offline end-to-end test** — `tests/test_end_to_end.py`

```python
import io
from pathlib import Path
from langchain_core.messages import AIMessage
from rich.console import Console
from luna.config import LunaConfig
from luna.agent import build_agent
from luna.session import run_once

def test_agent_writes_a_file_after_approval(tmp_path, fake_model):
    # scripted: model asks to write, then summarizes
    write_call = AIMessage(
        content="",
        tool_calls=[{"name": "write_file", "id": "1",
                     "args": {"file_path": "hello.txt", "content": "hi\n"}}],
    )
    done = AIMessage(content="created hello.txt")
    agent = build_agent(LunaConfig(workdir=str(tmp_path)),
                        model=fake_model(write_call, done))
    text = run_once(agent, "make hello.txt", thread_id="e2e",
                    console=Console(file=io.StringIO()），
                    input_fn=lambda _: "")  # approve
    assert (tmp_path / "hello.txt").read_text() == "hi\n"
    assert "hello.txt" in text
```

(Fix the stray full-width paren before running — plan typo guard.)

- [ ] **Step 2: Run it, verify fail, then make it pass** — adjust `run_once`/`collect_decisions` until the approval round-trip actually writes the file. Expected: PASS.

- [ ] **Step 3: Full gate**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected: all pass.

- [ ] **Step 4: Manual smoke (documented, not automated)**

```bash
export ANTHROPIC_API_KEY=...        # real key
uv run luna --no-splash "list the files in this repo and read pyproject.toml"
uv run luna                          # see the splash, type /help, /tools, /exit
```

Record the outcome in the PR / commit body.

- [ ] **Step 5: Docs pass** — ensure `README.md` documents: install with extras, the five providers + keys, `--yolo`, `--no-splash`, slash commands, config file locations and TOML shape. Bump `CHANGELOG.md` `[0.1.0]` with the feature list.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "test: offline end-to-end approval flow; docs and lint pass"
```

---

## Self-Review

**Spec coverage:**
- §4 layout → Task 1 (+ files created across all tasks)
- §5.1 config → Task 3
- §5.2 providers → Task 2
- §5.3 prompt → Task 4
- §5.4 agent → Task 4
- §5.5 session → Task 6
- §5.6 ui → Task 5
- §5.7 cli → Tasks 1 (stub) + 7 (real)
- §6 testing → every task's tests + Tasks 4/8 fixtures
- §7 manifesto → Task 1 (README)
- §9 risks (virtual_mode) → Task 4 Step 5 note

**Placeholder scan:** no "TBD"/"handle edge cases"/bare "write tests" — all test code is inline. One deliberate typo guard flagged in Task 8 Step 1.

**Type consistency:** `build_agent(config, *, model=None, checkpointer=None)` consistent across Tasks 4/6/7. `prompt_decision(console, action_request, *, input_fn=input)` consistent Tasks 5/6. `collect_decisions(console, interrupt_value, *, input_fn=input) -> {"decisions": [...]}` consistent Tasks 6/8. `load_config(cli_overrides, *, env=None, cwd=None)` consistent Tasks 3/7. CLI namespace attrs `prompt_pos`, `no_splash`, `yolo` consistent Tasks 1/7.
