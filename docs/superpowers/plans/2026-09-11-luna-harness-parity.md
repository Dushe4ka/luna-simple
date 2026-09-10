# Luna Harness Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt eight opencode/field-parity capabilities: LSP diagnostics + navigation, formatters, custom slash commands, a git-based undo/redo rework, a model price/window registry, plan mode, `@agent` mentions, and `--json` output.

**Architecture:** Each capability is 1-3 small modules. `deepagents`/`langgraph` imports stay confined to `luna/agent.py`, `luna/session.py`, `luna/persistence.py`, `luna/toolguard.py`. The post-mutation hook (format → diagnose → verify) and the undo rework both live in `luna/session.py`, which already owns the turn loop.

**Tech Stack:** Python 3.11+, deepagents ~=0.7.13, langchain ~=1.4, langgraph ~=1.2, `multilspy` (optional, `lsp` extra), rich, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-09-luna-parity-design.md` — read it alongside this plan.

## Plan-level deviation from the spec (read this first)

The spec's §3.1/§3.3 propose tracking "touched files" by extending `_stream_turn`'s
return to a 5-tuple. This plan does **not** do that: it reuses the already-tested
`gitinfo.dirty_paths(workdir)` (returns `[]` outside a git repo) to scope the
format/diagnose commands to files the turn actually changed. This avoids a
multi-call-site signature change to a function three call sites already depend
on, is simpler, and produces the same externally-visible behavior the spec
asks for (format/diagnose act on what changed, whole-project when not a git
repo or the tool needs no path list). Task 2 introduces the shared
`_format_and_diagnose` helper on this basis; Task 3 extends it.

## Global Constraints

- Python **3.11+**, PEP 8 / PEP 257. `uv run ruff check .` and `uv run ruff format --check .` must pass.
- `deepagents` / `langgraph` / `langchain_mcp_adapters` imports live ONLY in `luna/agent.py`, `luna/session.py`, `luna/persistence.py`, `luna/toolguard.py`.
- Model IDs and prices live in `luna/models.toml` / config — never hard-coded in logic.
- Tests never hit the network. Use `FakeToolCallingModel` (`tests/conftest.py`); the autouse `isolated_config_home` fixture redirects `XDG_CONFIG_HOME` / `Path.home()`. Any shelled-out command a test scripts uses `sys.executable`, never a bare `python` (0.2.1 lesson).
- The `lsp` extra (`multilspy`) is NOT assumed installed — `lspnav` tests must `pytest.importorskip("multilspy")` or test pure logic (detection, formatting) without it.
- `uv run pytest -q` must be fully green after every task. Version target: **0.3.0**.
- Commit after every task; message ends with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Work on branch `feat/harness-parity` (already checked out).

---

### Task 1: Model registry (`luna/models.toml`) + cost in `usage.py`

**Files:**
- Create: `luna/models.toml`
- Modify: `luna/usage.py`, `luna/config.py` (`pricing` field), `luna/commands.py` (`_usage` cost line)
- Test: `tests/test_usage.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `luna/models.toml` — packaged data file, one `[<id-substring>]` table per model with `window` (int), `input` / `output` (float, USD per 1M tokens). At least these entries: `claude-sonnet-4`, `claude-opus-4`, `claude-haiku`, `gpt-4.1`, `gpt-4o`, `gpt-5`, `o1`, `o3`, `gemini-2.5`, `gemini-1.5`, `deepseek`, `qwen2.5-coder`.
  - `usage.context_window(provider, model, overrides=None) -> int` — registry-aware, same signature shape plus a new optional `overrides` dict (a `{substring: {"window":...}}` mapping checked first).
  - `usage.price(provider, model, overrides=None) -> tuple[float, float] | None`.
  - `SessionUsage.cost(provider, model, overrides=None) -> float | None`.
  - `indicator_line(session, provider, model, overrides=None) -> str` — appends `` · $0.0042`` when a cost is known.
  - `LunaConfig.pricing: dict = {}` from `[model.pricing]` in TOML (a table of tables, not `_SETTABLE`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_usage.py  (add)
def test_registry_window_and_price():
    from luna.usage import context_window, price

    assert context_window("anthropic", "claude-sonnet-4-5") == 200_000
    p = price("anthropic", "claude-sonnet-4-5")
    assert p is not None and p[0] > 0 and p[1] > 0


def test_registry_overrides_win_over_the_packaged_file():
    from luna.usage import context_window, price

    overrides = {"my-model": {"window": 128_000, "input": 2.0, "output": 6.0}}
    assert context_window("x", "my-model-v1", overrides) == 128_000
    assert price("x", "my-model-v1", overrides) == (2.0, 6.0)


def test_unknown_model_has_no_price():
    from luna.usage import price

    assert price("x", "totally-unknown-model-id") is None


def test_session_cost_uses_totals():
    from luna.usage import SessionUsage, TurnUsage

    s = SessionUsage()
    t = TurnUsage()
    t.merge({"input_tokens": 1_000_000, "output_tokens": 1_000_000, "total_tokens": 2_000_000})
    s.add_turn(t)
    cost = s.cost("anthropic", "claude-sonnet-4-5")
    assert cost is not None and cost > 0


def test_indicator_line_shows_cost_when_known():
    from luna.usage import SessionUsage, TurnUsage, indicator_line

    s = SessionUsage()
    t = TurnUsage()
    t.merge({"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200})
    s.add_turn(t)
    line = indicator_line(s, "anthropic", "claude-sonnet-4-5")
    assert "$" in line
```

```python
# tests/test_config.py  (add)
def test_pricing_table_is_loaded(isolated_config_home):
    from luna.config import config_dir, load_config

    (config_dir() ).mkdir(parents=True, exist_ok=True)
    (config_dir() / "config.toml").write_text(
        '[model.pricing."my-model"]\ninput = 2.0\noutput = 6.0\nwindow = 128000\n'
    )
    cfg = load_config({})
    assert cfg.pricing.get("my-model") == {"input": 2.0, "output": 6.0, "window": 128000}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_usage.py tests/test_config.py -q`
Expected: FAIL — `luna/models.toml` doesn't exist, `price`/`overrides` unknown, `LunaConfig.pricing` unknown.

- [ ] **Step 3: Create `luna/models.toml`**

```toml
["claude-opus-4"]
window = 200000
input  = 15.0
output = 75.0

["claude-sonnet-4"]
window = 200000
input  = 3.0
output = 15.0

["claude-haiku"]
window = 200000
input  = 0.8
output = 4.0

["gpt-5"]
window = 400000
input  = 1.25
output = 10.0

["gpt-4.1"]
window = 1000000
input  = 2.0
output = 8.0

["gpt-4o"]
window = 128000
input  = 2.5
output = 10.0

["o3"]
window = 200000
input  = 2.0
output = 8.0

["o1"]
window = 200000
input  = 15.0
output = 60.0

["gemini-2.5"]
window = 1000000
input  = 1.25
output = 10.0

["gemini-1.5"]
window = 1000000
input  = 1.25
output = 5.0

["deepseek"]
window = 64000
input  = 0.28
output = 0.42

["qwen2.5-coder"]
window = 32000
input  = 0.0
output = 0.0
```

- [ ] **Step 4: Extend `luna/usage.py`**

Add near the top (after `_WINDOWS`/`_FALLBACK`):

```python
import tomllib
from importlib import resources

_REGISTRY_CACHE: dict[str, dict] | None = None


def _load_registry() -> dict[str, dict]:
    """Read the packaged model registry once, caching the result."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE
    try:
        text = resources.files("luna").joinpath("models.toml").read_text()
        _REGISTRY_CACHE = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError, ModuleNotFoundError):
        _REGISTRY_CACHE = {}
    return _REGISTRY_CACHE


def _match(entries: dict, needle: str) -> dict | None:
    for key, entry in entries.items():
        if key.lower() in needle:
            return entry
    return None
```

Replace `context_window` and add `price`:

```python
def context_window(provider: str, model: str | None, overrides: dict | None = None) -> int:
    """Best-effort context-window size for ``model`` (fallback 200k)."""
    needle = (model or provider or "").lower()
    entry = _match(overrides, needle) if overrides else None
    if entry is None:
        entry = _match(_load_registry(), needle)
    if entry and "window" in entry:
        return int(entry["window"])
    for key, size in _WINDOWS.items():
        if key in needle:
            return size
    return _FALLBACK


def price(
    provider: str, model: str | None, overrides: dict | None = None
) -> tuple[float, float] | None:
    """``(input_usd_per_1m, output_usd_per_1m)`` for ``model``, or ``None``."""
    needle = (model or provider or "").lower()
    entry = _match(overrides, needle) if overrides else None
    if entry is None:
        entry = _match(_load_registry(), needle)
    if entry and "input" in entry and "output" in entry:
        return float(entry["input"]), float(entry["output"])
    return None
```

Add `cost` to `SessionUsage`:

```python
    def cost(
        self, provider: str, model: str | None, overrides: dict | None = None
    ) -> float | None:
        """Total USD for every recorded turn, or ``None`` when no price is known."""
        p = price(provider, model, overrides)
        if p is None:
            return None
        in_price, out_price = p
        in_tok, out_tok, _ = self.totals
        return in_tok / 1_000_000 * in_price + out_tok / 1_000_000 * out_price
```

Update `indicator_line`:

```python
def indicator_line(
    session: SessionUsage, provider: str, model: str | None, overrides: dict | None = None
) -> str:
    """One-line dim summary printed after each model turn."""
    win = context_window(provider, model, overrides)
    used = session.last_prompt_tokens
    _, _, total = session.totals
    last = session.turns[-1] if session.turns else TurnUsage()
    line = (
        f"ctx ~{_k(used)}/{_k(win)} · turn {_k(last.input_tokens)} in / "
        f"{_k(last.output_tokens)} out · session {_k(total)}"
    )
    cost = session.cost(provider, model, overrides)
    if cost is not None:
        line += f" · ${cost:.4f}"
    return line
```

Add `"price"` to `__all__`.

- [ ] **Step 5: `luna/config.py`**

`LunaConfig`: add `pricing: dict = field(default_factory=dict)`.
`_apply_toml`: in the `model` block, after the `fast` check:

```python
    if isinstance(model.get("pricing"), dict):
        into["pricing"] = {k: dict(v) for k, v in model["pricing"].items() if isinstance(v, dict)}
```

`load_config`: add `pricing=dict(merged.get("pricing", {}))` to the returned `LunaConfig(...)`.

- [ ] **Step 6: Wire the indicator + `/usage` call sites**

`luna/session.py` — both places that call `indicator_line(session_usage, config.provider, config.model)` → add `, config.pricing`.
`luna/commands.py` `_usage`: after the existing totals line, add:

```python
    cost = session.cost(ctx.config.provider, ctx.config.model, ctx.config.pricing)
    if cost is not None:
        ctx.console.print(f"  cost: ${cost:.4f}")
```

- [ ] **Step 7: Packaging check**

Run: `uv pip install -e . && uv run python -c "from importlib import resources; print(len(resources.files('luna').joinpath('models.toml').read_text()))"`
Expected: prints a positive number (the file is readable as packaged data). If it raises `FileNotFoundError`, add to `pyproject.toml` under `[tool.hatch.build.targets.wheel]`:
```toml
artifacts = ["luna/models.toml"]
```
and re-run the check.

- [ ] **Step 8: Run tests**

Run: `uv run pytest -q`
Expected: PASS (was 202 → 202 + 5 new usage + 1 new config = 208).

- [ ] **Step 9: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/models.toml luna/usage.py luna/config.py luna/commands.py tests/test_usage.py tests/test_config.py pyproject.toml
git commit -m "feat: vendored model registry — context window + $ cost in /usage"
```

---

### Task 2: Formatters (`luna/fmt.py`) + the shared post-mutation hook

**Files:**
- Create: `luna/fmt.py`
- Modify: `luna/config.py` (`format_command`), `luna/session.py` (new `_format_and_diagnose` helper, wired after `_stream_turn`)
- Test: `tests/test_fmt.py`, `tests/test_session_reload.py` (or a new focused test file)

**Interfaces:**
- Consumes: `gitinfo.dirty_paths`, `gitinfo.is_git_repo`.
- Produces:
  - `fmt.detect(workdir: str) -> str` — returns a formatter command or `""`.
  - `fmt.run(command: str, workdir: str, paths: list[str]) -> list[str]` — runs the formatter, returns the list of paths it was pointed at (or `[]` if the command was empty/failed); never raises.
  - `LunaConfig.format_command: str = "auto"`; `_SETTABLE["agent.format_command"] = "str"`.
  - `session._format_and_diagnose(console, cfg) -> str` — runs format (this task) then returns `""` (diagnose is Task 3's job; the function is created now so Task 3 extends it in place rather than introducing a second hook point).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fmt.py
import sys


def test_detect_returns_empty_with_no_markers(tmp_path):
    from luna.fmt import detect

    assert detect(str(tmp_path)) == ""


def test_run_executes_and_reports_paths(tmp_path):
    from luna.fmt import run

    f = tmp_path / "a.txt"
    f.write_text("x")
    marker = tmp_path / "ran.txt"
    cmd = f'{sys.executable} -c "open(\'{marker}\', \'w\').close()"'
    touched = run(cmd, str(tmp_path), ["a.txt"])
    assert marker.exists()
    assert touched == ["a.txt"]


def test_run_never_raises_on_a_bad_command(tmp_path):
    from luna.fmt import run

    assert run("this-command-does-not-exist-xyz", str(tmp_path), ["a.txt"]) == []


def test_run_with_empty_command_is_a_noop(tmp_path):
    from luna.fmt import run

    assert run("", str(tmp_path), ["a.txt"]) == []
```

```python
# tests/test_config.py  (add)
def test_format_command_settable(isolated_config_home):
    from luna.config import load_config, set_config_values

    set_config_values({"agent.format_command": "ruff format"})
    assert load_config({}).format_command == "ruff format"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_fmt.py -q`
Expected: FAIL — `luna.fmt` doesn't exist.

- [ ] **Step 3: Implement `luna/fmt.py`**

```python
"""Auto-format touched files after a mutating turn (best-effort, never raises)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_TIMEOUT = 60


def detect(workdir: str) -> str:
    """Guess a formatter command from the project's markers and installed tools."""
    root = Path(workdir)
    if shutil.which("ruff") and (
        (root / "pyproject.toml").is_file() or (root / "ruff.toml").is_file()
    ):
        return "ruff format"
    if shutil.which("black") and (root / "pyproject.toml").is_file():
        return "black -q"
    if shutil.which("prettier") and (root / "package.json").is_file():
        return "prettier -w"
    if shutil.which("gofmt") and (root / "go.mod").is_file():
        return "gofmt -w"
    if shutil.which("rustfmt") and (root / "Cargo.toml").is_file():
        return "rustfmt"
    return ""


def run(command: str, workdir: str, paths: list[str]) -> list[str]:
    """Run ``command`` over ``paths`` (or the whole project when ``paths`` is empty).

    Returns the list of paths the command was pointed at (empty on failure or a
    blank command) so the caller can report how many files were touched.
    """
    if not command.strip():
        return []
    full = f"{command} {' '.join(paths)}" if paths else command
    try:
        subprocess.run(
            full, shell=True, cwd=workdir, capture_output=True, text=True, timeout=_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return list(paths)
```

- [ ] **Step 4: `config.py`**

`_SETTABLE["agent.format_command"] = "str"`. `LunaConfig.format_command: str = "auto"`. `_apply_toml`: add `"format_command"` to the `agent` key list. `load_config`: `format_command=str(merged.get("format_command", "auto"))`.

- [ ] **Step 5: `session.py` — the shared hook**

Add near the top (with the other `from luna import ...` lines): `from luna import fmt, gitinfo`.

```python
def _format_and_diagnose(console: Console, cfg: LunaConfig) -> str:
    """Run the format step for files this turn changed. Returns diagnose output.

    Diagnose is wired in by Task 3; this task's version always returns ``""``.
    """
    changed = gitinfo.dirty_paths(cfg.workdir) if gitinfo.is_git_repo(cfg.workdir) else []
    fmt_cmd = cfg.format_command
    if fmt_cmd == "auto":
        fmt_cmd = fmt.detect(cfg.workdir)
    if fmt_cmd:
        touched = fmt.run(fmt_cmd, cfg.workdir, changed)
        if touched:
            console.print(f"[dim]⌁ formatted {len(touched)} file(s)[/]")
    return ""
```

Call it from `run_repl` right after the `session_usage`/indicator block (still inside `if tool_names & _MUTATING:`), before `_run_verification`:

```python
        if tool_names & _MUTATING:
            _format_and_diagnose(console, config)
            try:
                _run_verification(agent, turn_config, console, config, input_fn, rules=rules)
```

Do the same in `run_once` (before its `_run_verification` call, guarded the same `if cfg is not None and tool_names & _MUTATING:`).

- [ ] **Step 6: Run tests**

Run: `uv run pytest -q`
Expected: PASS (208 → 212: +4 fmt, +1 config).

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/fmt.py luna/config.py luna/session.py tests/test_fmt.py tests/test_config.py
git commit -m "feat: auto-format touched files after a mutating turn"
```

---

### Task 3: LSP diagnostics (`luna/diagnose.py`) + `<diagnostics>` injection + `/diagnose`

**Files:**
- Create: `luna/diagnose.py`
- Modify: `luna/config.py` (`diagnose_command`), `luna/session.py` (`_format_and_diagnose` extended, `pending_diagnostics` in `run_repl`), `luna/commands.py` (`/diagnose`)
- Test: `tests/test_diagnose.py`, `tests/test_repl_flow.py` (extend)

**Interfaces:**
- Consumes: `_format_and_diagnose` (Task 2), `gitinfo.dirty_paths`.
- Produces:
  - `diagnose.detect(workdir: str) -> str`.
  - `diagnose.run(command: str, workdir: str, paths: list[str]) -> str` — compact `path:line: message` text, capped ~40 lines, `""` on a clean run; never raises.
  - `LunaConfig.diagnose_command: str = "auto"`; `_SETTABLE["agent.diagnose_command"]`.
  - `_format_and_diagnose` now returns the diagnose text (non-empty when there are findings).
  - `run_repl` prepends `<diagnostics>\n…\n</diagnostics>\n\n` to the *next* turn's payload once, then clears it.
  - `/diagnose` — run on demand.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_diagnose.py
import sys


def test_detect_returns_empty_with_no_markers(tmp_path):
    from luna.diagnose import detect

    assert detect(str(tmp_path)) == ""


def test_run_reports_command_output(tmp_path):
    from luna.diagnose import run

    cmd = f'{sys.executable} -c "print(\'a.py:3: undefined name x\')"'
    text = run(cmd, str(tmp_path), ["a.py"])
    assert "a.py:3" in text


def test_run_clean_output_is_empty(tmp_path):
    from luna.diagnose import run

    cmd = f'{sys.executable} -c "pass"'
    assert run(cmd, str(tmp_path), ["a.py"]) == ""


def test_run_caps_output_length(tmp_path):
    from luna.diagnose import run

    cmd = f'{sys.executable} -c "[print(i) for i in range(200)]"'
    text = run(cmd, str(tmp_path), [])
    assert len(text.splitlines()) <= 40


def test_run_never_raises_on_a_bad_command(tmp_path):
    from luna.diagnose import run

    assert run("this-command-does-not-exist-xyz", str(tmp_path), []) == ""


def test_run_disabled_when_command_empty(tmp_path):
    from luna.diagnose import run

    assert run("", str(tmp_path), ["a.py"]) == ""
```

```python
# tests/test_repl_flow.py  (add)
def test_diagnostics_are_injected_into_the_next_turn(tmp_path, fake_model):
    import io
    from langchain_core.messages import AIMessage
    from rich.console import Console
    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.session import run_repl

    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "1",
                    "args": {"file_path": "/a.py", "content": "x = 1\n"},
                }
            ],
        ),
        AIMessage(content="wrote a.py"),
        AIMessage(content="saw the diagnostics"),
    ]
    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls), session_id="sid"
    )
    seen_payloads: list[str] = []
    real_invoke = agent.invoke

    def _spy(payload, *a, **kw):
        if isinstance(payload, dict):
            msgs = payload.get("messages", [])
            if msgs:
                seen_payloads.append(msgs[-1].get("content", ""))
        return real_invoke(payload, *a, **kw)

    agent.invoke = _spy
    console = Console(file=io.StringIO(), force_terminal=True)
    lines = iter(["write a.py", "second turn", "/exit"])
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True, diagnose_command="echo a.py:1: fake finding")
    run_repl(
        agent,
        console=console,
        input_fn=lambda _: next(lines),
        rebuild=lambda: agent,
        workdir=str(tmp_path),
        config=cfg,
        thread_id="t",
        session_id="sid",
    )
    assert any("<diagnostics>" in p and "fake finding" in p for p in seen_payloads)
```

(If `agent.invoke` cannot be monkeypatched this way because `_stream_turn` uses
`agent.stream` not `agent.invoke`, adapt the spy to wrap `agent.stream` instead —
same idea: capture the payload passed on the *second* turn and assert it
contains the injected block.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_diagnose.py -q`
Expected: FAIL — `luna.diagnose` doesn't exist.

- [ ] **Step 3: Implement `luna/diagnose.py`**

```python
"""Run a project's type/lint checker after edits (best-effort, never raises)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_TIMEOUT = 120
_MAX_LINES = 40


def detect(workdir: str) -> str:
    """Guess a diagnostics command from installed tools and project markers."""
    root = Path(workdir)
    if shutil.which("ruff") and (
        (root / "pyproject.toml").is_file() or (root / "ruff.toml").is_file()
    ):
        return "ruff check"
    if shutil.which("pyright") and (root / "pyproject.toml").is_file():
        return "pyright"
    if shutil.which("tsc") and (root / "tsconfig.json").is_file():
        return "tsc --noEmit --pretty false"
    if shutil.which("go") and (root / "go.mod").is_file():
        return "go vet ./..."
    if shutil.which("cargo") and (root / "Cargo.toml").is_file():
        return "cargo check --message-format short"
    return ""


def run(command: str, workdir: str, paths: list[str]) -> str:
    """Run ``command`` and return a capped, best-effort diagnostics summary."""
    if not command.strip():
        return ""
    full = f"{command} {' '.join(paths)}" if paths else command
    try:
        proc = subprocess.run(
            full, shell=True, cwd=workdir, capture_output=True, text=True, timeout=_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode == 0 and not proc.stdout.strip() and not proc.stderr.strip():
        return ""
    combined = (proc.stdout + proc.stderr).splitlines()
    lines = [line for line in combined if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[:_MAX_LINES])
```

- [ ] **Step 4: `config.py`**

Same pattern as `format_command`: `_SETTABLE["agent.diagnose_command"] = "str"`, `LunaConfig.diagnose_command: str = "auto"`, add `"diagnose_command"` to the `_apply_toml` agent-key list, `load_config`: `diagnose_command=str(merged.get("diagnose_command", "auto"))`.

- [ ] **Step 5: Extend `_format_and_diagnose` in `session.py`**

```python
def _format_and_diagnose(console: Console, cfg: LunaConfig) -> str:
    """Format then diagnose the files this turn changed. Returns diagnose text."""
    changed = gitinfo.dirty_paths(cfg.workdir) if gitinfo.is_git_repo(cfg.workdir) else []
    fmt_cmd = cfg.format_command
    if fmt_cmd == "auto":
        fmt_cmd = fmt.detect(cfg.workdir)
    if fmt_cmd:
        touched = fmt.run(fmt_cmd, cfg.workdir, changed)
        if touched:
            console.print(f"[dim]⌁ formatted {len(touched)} file(s)[/]")
    diag_cmd = cfg.diagnose_command
    if diag_cmd == "auto":
        diag_cmd = diagnose.detect(cfg.workdir)
    if not diag_cmd:
        return ""
    changed = gitinfo.dirty_paths(cfg.workdir) if gitinfo.is_git_repo(cfg.workdir) else changed
    text = diagnose.run(diag_cmd, cfg.workdir, changed)
    if text:
        console.print(f"[dim]{text}[/]")
    return text
```

Add `from luna import diagnose` to the imports.

In `run_repl`, add `pending_diagnostics = ""` before the `while True:` loop. Where the hook is called:

```python
        if tool_names & _MUTATING:
            pending_diagnostics = _format_and_diagnose(console, config)
            try:
```

At the top of the non-slash turn-building block (where `pinned_block`/`expanded` are assembled), prepend the pending block and clear it:

```python
        turn_config = {"configurable": {"thread_id": thread_id}}
        diag_block = f"<diagnostics>\n{pending_diagnostics}\n</diagnostics>\n\n" if pending_diagnostics else ""
        pending_diagnostics = ""
        pinned_block = render_pinned(pinned, workdir)
        expanded = expand_mentions(line, workdir)
        content = diag_block + (pinned_block + "\n\n" if pinned_block else "") + expanded
```

`run_once` calls `_format_and_diagnose` too (Task 2 already wired the call site) but has no "next turn" to inject into — just let it print, no stashing needed there.

- [ ] **Step 6: `/diagnose` command**

`commands.py`:

```python
def _diagnose(ctx: CommandContext, arg: str) -> None:
    """Run the project's diagnostics command now."""
    from luna import diagnose

    cmd = ctx.config.diagnose_command
    if cmd == "auto":
        cmd = diagnose.detect(ctx.config.workdir)
    if not cmd:
        ctx.console.print("[dim]no diagnose command configured or detected[/]")
        return
    text = diagnose.run(cmd, ctx.config.workdir, [])
    ctx.console.print(text or "[dim]no findings[/]")
```

Register `"/diagnose": _diagnose` in `_TABLE`; add to `HELP`.

- [ ] **Step 7: Run tests**

Run: `uv run pytest -q`
Expected: PASS (212 → 218ish).

- [ ] **Step 8: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/diagnose.py luna/config.py luna/session.py luna/commands.py tests/test_diagnose.py tests/test_repl_flow.py
git commit -m "feat: run diagnostics after edits, inject findings into the next turn"
```

---

### Task 4: LSP navigation tools (`luna/lspnav.py`)

**Files:**
- Create: `luna/lspnav.py`
- Modify: `pyproject.toml` (`lsp` extra), `luna/agent.py` (tool wiring), `luna/config.py` (`language`), `luna/prompts.py` (one line)
- Test: `tests/test_lspnav.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `lspnav.detect_language(workdir: str) -> str | None`.
  - `lspnav.available() -> bool` — whether `multilspy` is importable.
  - `lspnav.make_tools(workdir: str, language: str) -> list` — 3 `@tool`-decorated callables: `goto_definition`, `find_references`, `hover`.
  - `LunaConfig.language: str = ""`; `_SETTABLE["agent.language"]`.
  - `build_agent` appends the tools when available + a language is known.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lspnav.py
def test_detect_language_from_markers(tmp_path):
    from luna.lspnav import detect_language

    assert detect_language(str(tmp_path)) is None
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect_language(str(tmp_path)) == "python"


def test_detect_language_typescript(tmp_path):
    from luna.lspnav import detect_language

    (tmp_path / "tsconfig.json").write_text("{}")
    assert detect_language(str(tmp_path)) == "typescript"


def test_available_reflects_import(monkeypatch):
    import luna.lspnav as lspnav

    assert isinstance(lspnav.available(), bool)


def test_make_tools_without_multilspy_reports_unavailable(tmp_path, monkeypatch):
    import luna.lspnav as lspnav

    monkeypatch.setattr(lspnav, "available", lambda: False)
    tools = lspnav.make_tools(str(tmp_path), "python")
    assert tools == []
```

```python
# tests/test_lspnav.py  (append; skipped unless multilspy is installed)
import pytest


@pytest.mark.skipif(
    not __import__("luna.lspnav", fromlist=["available"]).available(),
    reason="multilspy not installed",
)
def test_goto_definition_runs_against_a_real_python_file(tmp_path):
    from luna.lspnav import make_tools

    (tmp_path / "a.py").write_text("def foo():\n    pass\n\nfoo()\n")
    tools = make_tools(str(tmp_path), "python")
    by_name = {t.name: t for t in tools}
    result = by_name["goto_definition"].invoke({"file": "a.py", "line": 4, "symbol": "foo"})
    assert "a.py" in result
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_lspnav.py -q`
Expected: FAIL — `luna.lspnav` doesn't exist.

- [ ] **Step 3: `pyproject.toml`**

Add to `[project.optional-dependencies]`:
```toml
lsp = ["multilspy>=0.0.10"]
```
(Verify the exact latest version at implementation time and pin it.) Add `"multilspy>=0.0.10"` to the `all` list too.

- [ ] **Step 4: Implement `luna/lspnav.py`**

```python
"""Optional LSP-backed navigation tools: goto_definition, find_references, hover.

Requires the ``lsp`` extra (``multilspy``). Every function degrades to a plain
string message instead of raising when the extra is missing or the server
fails to start — these are read-only tools, never gated by approval.
"""

from __future__ import annotations

import atexit
from pathlib import Path

from langchain_core.tools import tool

try:
    from multilspy import SyncLanguageServer
    from multilspy.multilspy_config import MultilspyConfig
    from multilspy.multilspy_logger import MultilspyLogger

    _HAVE_MULTILSPY = True
except ImportError:  # pragma: no cover - exercised when the extra is absent
    _HAVE_MULTILSPY = False

_LANGUAGE_MARKERS: dict[str, str] = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "tsconfig.json": "typescript",
    "package.json": "javascript",
    "go.mod": "go",
    "Cargo.toml": "rust",
}

_SERVERS: dict[tuple[str, str], object] = {}


def available() -> bool:
    """Whether the ``multilspy`` extra is installed."""
    return _HAVE_MULTILSPY


def detect_language(workdir: str) -> str | None:
    """Guess the project's language from common marker files."""
    root = Path(workdir)
    for marker, language in _LANGUAGE_MARKERS.items():
        if (root / marker).is_file():
            return language
    return None


def _server(workdir: str, language: str):
    key = (str(Path(workdir).resolve()), language)
    if key in _SERVERS:
        return _SERVERS[key]
    if not _HAVE_MULTILSPY:
        _SERVERS[key] = None
        return None
    try:
        config = MultilspyConfig.from_dict({"code_language": language})
        srv = SyncLanguageServer.create(config, MultilspyLogger(), key[0])
        ctx = srv.start_server()
        ctx.__enter__()
        atexit.register(lambda: ctx.__exit__(None, None, None))
    except Exception:  # noqa: BLE001 - a broken LSP server must not break Luna
        srv = None
    _SERVERS[key] = srv
    return srv


def _find_column(workdir: str, file: str, line: int, symbol: str) -> int:
    try:
        text = (Path(workdir) / file).read_text()
        target = text.splitlines()[line - 1]
        return max(0, target.find(symbol))
    except (OSError, IndexError):
        return 0


def make_tools(workdir: str, language: str) -> list:
    """Build the three navigation tools bound to ``workdir``/``language``."""
    if not _HAVE_MULTILSPY:
        return []

    @tool
    def goto_definition(file: str, line: int, symbol: str) -> str:
        """Find where ``symbol`` (on 1-based ``line`` of ``file``) is defined."""
        srv = _server(workdir, language)
        if srv is None:
            return "LSP unavailable"
        col = _find_column(workdir, file, line, symbol)
        try:
            results = srv.request_definition(file, line - 1, col)
        except Exception as exc:  # noqa: BLE001 - a tool must report, not crash
            return f"LSP unavailable: {exc}"
        if not results:
            return "no definition found"
        return "\n".join(
            f"{r.get('relativePath', '?')}:{r['range']['start']['line'] + 1}" for r in results
        )

    @tool
    def find_references(file: str, line: int, symbol: str) -> str:
        """Find callers/usages of ``symbol`` (on 1-based ``line`` of ``file``)."""
        srv = _server(workdir, language)
        if srv is None:
            return "LSP unavailable"
        col = _find_column(workdir, file, line, symbol)
        try:
            results = srv.request_references(file, line - 1, col)
        except Exception as exc:  # noqa: BLE001
            return f"LSP unavailable: {exc}"
        if not results:
            return "no references found"
        return "\n".join(
            f"{r.get('relativePath', '?')}:{r['range']['start']['line'] + 1}" for r in results
        )

    @tool
    def hover(file: str, line: int, symbol: str) -> str:
        """Show type/doc info for ``symbol`` (on 1-based ``line`` of ``file``)."""
        srv = _server(workdir, language)
        if srv is None:
            return "LSP unavailable"
        col = _find_column(workdir, file, line, symbol)
        try:
            result = srv.request_hover(file, line - 1, col)
        except Exception as exc:  # noqa: BLE001
            return f"LSP unavailable: {exc}"
        if not result:
            return "no hover info"
        contents = result.get("contents", "")
        return contents.get("value", str(contents)) if isinstance(contents, dict) else str(contents)

    return [goto_definition, find_references, hover]
```

- [ ] **Step 5: `config.py`**

`_SETTABLE["agent.language"] = "str"`. `LunaConfig.language: str = ""`. `_apply_toml`: add `"language"` to the agent-key list. `load_config`: `language=str(merged.get("language", ""))`.

- [ ] **Step 6: `agent.py` wiring**

```python
from luna import lspnav
...
    language = config.language or lspnav.detect_language(str(workdir))
    lsp_tools: list = []
    if language:
        if lspnav.available():
            lsp_tools = lspnav.make_tools(str(workdir), language)
        else:
            on_warn(f"a '{language}' project was detected but the 'lsp' extra "
                    f"(multilspy) is not installed — goto_definition/find_references/"
                    f"hover are unavailable")
```

Add `*lsp_tools` to the `tools=[...]` list passed to `create_deep_agent`. These are not added to `INTERRUPT_TOOLS`.

- [ ] **Step 7: `prompts.py`**

Add under "Rules:": `"- When a symbol's definition or callers matter, prefer goto_definition / find_references over grepping, if they're available."`

- [ ] **Step 8: Run tests**

Run: `uv run pytest -q` (the `multilspy`-requiring test is skipped unless the extra is installed).
Expected: PASS.

- [ ] **Step 9: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/lspnav.py luna/agent.py luna/config.py luna/prompts.py pyproject.toml tests/test_lspnav.py
git commit -m "feat: optional LSP navigation tools (goto_definition, find_references, hover)"
```

---

### Task 5: Custom slash commands (`luna/usercmd.py`)

**Files:**
- Create: `luna/usercmd.py`
- Modify: `luna/commands.py` (`DispatchResult.prompt`, `dispatch` fallthrough, `CommandContext.user_commands`, `/commands`), `luna/session.py` (load once, handle `res.prompt`, `/reload` refresh)
- Test: `tests/test_usercmd.py`, `tests/test_commands.py`

**Interfaces:**
- Consumes: `context.expand_mentions` (already used by `session.py`; `usercmd` imports it too).
- Produces:
  - `usercmd.UserCommand` — `name: str`, `description: str`, `body: str`.
  - `usercmd.load(workdir: str, env=None) -> dict[str, UserCommand]` — keys are bare names (no leading `/`); project overrides user.
  - `usercmd.expand(cmd: UserCommand, arg: str, workdir: str) -> str`.
  - `DispatchResult.prompt: str | None = None`.
  - `CommandContext.user_commands: dict | None = None`.
  - `dispatch` returns `DispatchResult(prompt=...)` for a `_TABLE` miss that matches a loaded user command.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_usercmd.py
def test_load_reads_project_commands(tmp_path):
    from luna.usercmd import load

    d = tmp_path / ".luna" / "commands"
    d.mkdir(parents=True)
    (d / "review.md").write_text("---\ndescription: review a diff\n---\nReview: $ARGUMENTS\n")
    cmds = load(str(tmp_path))
    assert "review" in cmds
    assert cmds["review"].description == "review a diff"
    assert cmds["review"].body.strip() == "Review: $ARGUMENTS"


def test_project_overrides_user(tmp_path, isolated_config_home):
    from luna.config import config_dir
    from luna.usercmd import load

    (config_dir() / "commands").mkdir(parents=True)
    (config_dir() / "commands" / "x.md").write_text("user version")
    d = tmp_path / ".luna" / "commands"
    d.mkdir(parents=True)
    (d / "x.md").write_text("project version")
    cmds = load(str(tmp_path))
    assert cmds["x"].body.strip() == "project version"


def test_malformed_frontmatter_is_skipped_gracefully(tmp_path):
    from luna.usercmd import load

    d = tmp_path / ".luna" / "commands"
    d.mkdir(parents=True)
    (d / "ok.md").write_text("plain body, no frontmatter")
    cmds = load(str(tmp_path))
    assert cmds["ok"].body.strip() == "plain body, no frontmatter"
    assert cmds["ok"].description == ""


def test_expand_substitutes_arguments(tmp_path):
    from luna.usercmd import UserCommand, expand

    cmd = UserCommand(name="x", description="", body="do: $ARGUMENTS")
    assert expand(cmd, "the thing", str(tmp_path)) == "do: the thing"


def test_expand_runs_shell_injection(tmp_path):
    import sys

    from luna.usercmd import UserCommand, expand

    cmd = UserCommand(name="x", description="", body=f"say: !`{sys.executable} -c \"print('hi')\"`")
    assert "hi" in expand(cmd, "", str(tmp_path))


def test_expand_expands_file_mentions(tmp_path):
    from luna.usercmd import UserCommand, expand

    (tmp_path / "f.py").write_text("CONTENT\n")
    cmd = UserCommand(name="x", description="", body="look at @f.py")
    assert "CONTENT" in expand(cmd, "", str(tmp_path))
```

```python
# tests/test_commands.py  (add)
def test_user_command_falls_through_to_a_prompt(tmp_path):
    from luna.usercmd import UserCommand

    ctx = _ctx(workdir=str(tmp_path), user_commands={"greet": UserCommand("greet", "", "hi $ARGUMENTS")})
    res = dispatch("/greet world", ctx)
    assert res.handled is True
    assert res.prompt == "hi world"


def test_unknown_command_still_reported_when_no_user_command_matches():
    res = dispatch("/nope", _ctx())
    assert res.prompt is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_usercmd.py -q`
Expected: FAIL — `luna.usercmd` doesn't exist.

- [ ] **Step 3: Implement `luna/usercmd.py`**

```python
"""Custom slash commands: ``.luna/commands/<name>.md`` prompt templates."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from luna.config import config_dir
from luna.context import expand_mentions

_SHELL_RE = re.compile(r"!`([^`]*)`")
_SHELL_TIMEOUT = 30
_SHELL_CAP = 4000


@dataclass
class UserCommand:
    """A parsed ``.luna/commands/<name>.md`` file."""

    name: str
    description: str
    body: str


def _dirs(workdir: str, env: Mapping[str, str] | None) -> list[Path]:
    return [config_dir(env) / "commands", Path(workdir) / ".luna" / "commands"]


def _frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1 :])
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("\"'")
    return {}, text  # unterminated frontmatter: treat the whole file as body


def load(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> dict[str, UserCommand]:
    """Load every ``*.md`` command file (project overrides user, by name)."""
    commands: dict[str, UserCommand] = {}
    for d in _dirs(workdir, env):
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            try:
                text = path.read_text()
            except OSError:
                continue
            meta, body = _frontmatter(text)
            commands[path.stem] = UserCommand(
                name=path.stem, description=meta.get("description", ""), body=body
            )
    return commands


def _run_shell(match: re.Match) -> str:
    try:
        proc = subprocess.run(
            match.group(1), shell=True, capture_output=True, text=True, timeout=_SHELL_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip()[:_SHELL_CAP]


def expand(cmd: UserCommand, arg: str, workdir: str) -> str:
    """Render a command's body: ``$ARGUMENTS``, `` !`shell` ``, then ``@file``."""
    text = cmd.body.replace("$ARGUMENTS", arg)
    text = _SHELL_RE.sub(_run_shell, text)
    return expand_mentions(text, workdir).strip()
```

- [ ] **Step 4: `commands.py`**

`DispatchResult`: add `prompt: str | None = None`.
`CommandContext`: add `user_commands: dict | None = None`.
`dispatch`: replace the "unknown command" branch:

```python
    handler = _TABLE.get(name)
    if handler is None:
        bare = name[1:]
        if ctx.user_commands and bare in ctx.user_commands:
            from luna.usercmd import expand

            prompt = expand(ctx.user_commands[bare], arg, ctx.workdir)
            return DispatchResult(prompt=prompt)
        ctx.console.print(f"[{PALETTE['mauve']}]unknown command {name!r}; try /help[/]")
        return DispatchResult()
```

Add `_commands` handler:

```python
def _commands(ctx: CommandContext, arg: str) -> None:
    """List custom slash commands loaded from .luna/commands/."""
    cmds = ctx.user_commands or {}
    if not cmds:
        ctx.console.print("[dim](no custom commands)[/]")
        return
    for name, cmd in sorted(cmds.items()):
        ctx.console.print(f"  [bold {PALETTE['peri']}]/{name}[/]  {cmd.description}")
```

Register `"/commands": _commands`; `HELP["/commands"] = "list custom slash commands"`.

- [ ] **Step 5: `session.py`**

In `run_repl`: `user_commands = usercmd.load(workdir)` before building `ctx`; pass `user_commands=user_commands` into `CommandContext(...)`. On `/reload` (the `if res.agent is not None:` branch), also `user_commands = usercmd.load(workdir); ctx.user_commands = user_commands`.

After `dispatch`, before the existing `if res.handled:` block, handle the new case:

```python
        if line.startswith("/"):
            ctx.agent = agent
            ctx.thread_id = thread_id
            res = dispatch(line, ctx)
            if res.exit:
                return 0
            if res.prompt is not None:
                line = res.prompt  # fall through to the normal turn-building code below
            elif res.handled:
                if res.agent is not None:
                    agent = res.agent
                    rules = load_rules(workdir)
                    ctx.permissions = rules
                    user_commands = usercmd.load(workdir)
                    ctx.user_commands = user_commands
                if res.thread_id is not None:
                    thread_id = res.thread_id
                continue
```

(A user command's expanded prompt already ran `expand_mentions` inside `usercmd.expand`, so falling through to the normal turn-building code, which calls `expand_mentions(line, workdir)` again, is idempotent for plain text but would re-scan for `@` tokens — harmless since the text no longer contains unexpanded `@file` tokens after the first pass, but do confirm this in the test rather than assume it.)

Add `from luna import usercmd` to the imports.

- [ ] **Step 6: Run tests**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/usercmd.py luna/commands.py luna/session.py tests/test_usercmd.py tests/test_commands.py
git commit -m "feat: custom slash commands from .luna/commands/*.md"
```

---

### Task 6: Plan mode

**Files:**
- Modify: `luna/toolguard.py` (`plan` param), `luna/agent.py` (`plan_flag` param), `luna/session.py` (`plan_state`, prompt), `luna/cli.py` (thread `plan_state` through `_rebuild`), `luna/commands.py` (`/plan`)
- Test: `tests/test_agent.py`, `tests/test_commands.py`, `tests/test_repl_flow.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `tool_guard(rules, workdir, session_id="", plan=None)` — `plan` is `Callable[[], bool] | None`.
  - `build_agent(..., plan_flag: Callable[[], bool] | None = None)`.
  - `run_repl(..., plan_flag: Callable[[], bool] | None = None)` — used only for the prompt string; the REPL owns `plan_state` and passes the SAME callable into `build_agent` via `_rebuild` (wired in `cli.py`).
  - `/plan`, `/plan on`, `/plan off`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent.py  (add)
def test_plan_mode_blocks_mutating_tools(tmp_path, fake_model):
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent
    from luna.config import LunaConfig

    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "write_file", "id": "1", "args": {"file_path": "/a.py", "content": "x"}}],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls), plan_flag=lambda: True)
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]}, config={"configurable": {"thread_id": "t"}}
    )
    blob = " ".join(getattr(m, "content", "") or "" for m in out["messages"])
    assert "plan mode" in blob
    assert not (tmp_path / "a.py").exists()


def test_plan_flag_none_means_never_blocked(tmp_path, fake_model):
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent
    from luna.config import LunaConfig

    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "write_file", "id": "1", "args": {"file_path": "/a.py", "content": "x"}}],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))  # plan_flag defaults None
    agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]}, config={"configurable": {"thread_id": "t"}}
    )
    assert (tmp_path / "a.py").exists()
```

```python
# tests/test_commands.py  (add)
def test_plan_toggle_and_explicit_state():
    ctx = _ctx()
    ctx.plan_state = [False]
    dispatch("/plan", ctx)
    assert ctx.plan_state[0] is True
    dispatch("/plan off", ctx)
    assert ctx.plan_state[0] is False
    dispatch("/plan on", ctx)
    assert ctx.plan_state[0] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_agent.py -q -k plan`
Expected: FAIL — `plan_flag` unknown kwarg.

- [ ] **Step 3: `toolguard.py`**

```python
def tool_guard(rules: RuleSet, workdir: str, session_id: str = "", plan=None):
    """..." (docstring: add a line about ``plan``.)"""

    @wrap_tool_call
    def _guard(request, handler):
        call = request.tool_call
        name = call.get("name", "")
        args = call.get("args", {}) or {}
        if rules.match(name, args) == "deny":
            return ToolMessage(
                content=f"blocked by a Luna permission rule ({name})",
                tool_call_id=call.get("id", "blocked"),
            )
        if plan is not None and plan() and name in {"write_file", "edit_file", "delete", "execute"}:
            return ToolMessage(
                content=f"plan mode is on — refusing to {name}. Run /plan off to make changes.",
                tool_call_id=call.get("id", "blocked"),
            )
        if session_id and name in _MUTATING:
            rel = (args.get("file_path") or args.get("path") or "").lstrip("/")
            if rel:
                try:
                    snapshot(workdir, session_id, name, rel)
                except (OSError, ValueError):
                    pass
        return handler(request)

    return _guard
```

- [ ] **Step 4: `agent.py`**

`build_agent(..., plan_flag: Callable[[], bool] | None = None)`. Where `guard = tool_guard(rules, str(workdir), session_id=session_id)` is built, add `plan=plan_flag`.

- [ ] **Step 5: `commands.py`**

`CommandContext`: add `plan_state: list | None = None` (a 1-element mutable list; index 0 is the bool).

```python
def _plan(ctx: CommandContext, arg: str) -> None:
    """Toggle plan mode: /plan, /plan on, /plan off."""
    if ctx.plan_state is None:
        ctx.console.print("[dim]plan mode is not available here[/]")
        return
    if arg in ("on", "off"):
        ctx.plan_state[0] = arg == "on"
    else:
        ctx.plan_state[0] = not ctx.plan_state[0]
    ctx.console.print(f"[{PALETTE['blue']}]plan mode: {'on' if ctx.plan_state[0] else 'off'}[/]")
```

Register `"/plan": _plan`; `HELP["/plan"] = "toggle plan mode (blocks writes/execute)"`.

- [ ] **Step 6: `session.py` + `cli.py`**

`run_repl`: accept `plan_state: list | None = None` param (default `None` → create `[False]` locally so standalone tests still work: `plan_state = plan_state if plan_state is not None else [False]`). Pass `plan_state=plan_state` into `CommandContext(...)`. Change the prompt line:

```python
        try:
            prompt_label = "luna (plan) › " if plan_state[0] else "luna › "
            line = input_fn(prompt_label).strip()
```

`cli.py` `main()`: create `plan_state = [False]` alongside `session_id`. `_rebuild()` passes `plan_flag=lambda: plan_state[0]` to `build_agent(...)`. `run_repl(...)` call gets `plan_state=plan_state`.

- [ ] **Step 7: Run tests**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 8: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/toolguard.py luna/agent.py luna/commands.py luna/session.py luna/cli.py tests/test_agent.py tests/test_commands.py
git commit -m "feat: /plan mode blocks mutating tool calls without a rebuild"
```

---

### Task 7: `@agent` mention sugar

**Files:**
- Modify: `luna/session.py`
- Test: `tests/test_repl_flow.py` or a new `tests/test_session.py` addition

**Interfaces:**
- Consumes: `subagents.subagent_summaries`.
- Produces: in `run_repl`, a non-slash line matching `@<known-subagent-name> <rest>` is rewritten before being sent as a turn.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session.py  (add)
def test_at_agent_mention_is_rewritten_to_a_delegation_instruction(tmp_path, fake_model):
    import io
    from langchain_core.messages import AIMessage
    from rich.console import Console
    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.session import run_repl

    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(AIMessage(content="ok"))
    )
    seen: list[str] = []
    real_stream = agent.stream

    def _spy(payload, *a, **kw):
        if isinstance(payload, dict):
            msgs = payload.get("messages", [])
            if msgs:
                seen.append(msgs[-1]["content"])
        yield from real_stream(payload, *a, **kw)

    agent.stream = _spy
    console = Console(file=io.StringIO(), force_terminal=True)
    lines = iter(["@researcher find the entry point", "/exit"])
    run_repl(
        agent, console=console, input_fn=lambda _: next(lines), rebuild=lambda: agent,
        workdir=str(tmp_path), config=LunaConfig(workdir=str(tmp_path), yolo=True),
        thread_id="t",
    )
    assert seen and "researcher" in seen[0] and "task tool" in seen[0]
    assert "find the entry point" in seen[0]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_session.py -k at_agent -q`
Expected: FAIL — the line is sent verbatim, no rewrite.

- [ ] **Step 3: Implement in `session.py`**

Add near the top: `import re` (if not already imported) and:

```python
_AT_AGENT_RE = re.compile(r"^@([\w-]+)\s+(.+)$", re.DOTALL)
```

In `run_repl`, after loading `user_commands` and before the `while True:` loop:

```python
    subagent_names = {n for n, _ in subagent_summaries(workdir)}
```

(import `subagent_summaries` from `luna.subagents`; refresh this set alongside `user_commands` on `/reload`.)

In the non-slash branch, right before building `turn_config`/`payload`:

```python
        at_match = _AT_AGENT_RE.match(line)
        if at_match and at_match.group(1) in subagent_names:
            line = (
                f"Delegate this to the '{at_match.group(1)}' subagent using the "
                f"task tool: {at_match.group(2)}"
            )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/session.py tests/test_session.py
git commit -m "feat: @agent mentions delegate directly to a named subagent"
```

---

### Task 8: `luna "prompt" --json`

**Files:**
- Modify: `luna/cli.py` (`--output-format`/`--json`), `luna/session.py` (`run_once` json mode)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `cli.build_parser()` gains `--output-format {text,json}` (default `"text"`) and `--json` (sets it to `"json"`).
  - `session.run_once(..., output_format: str = "text") -> str` — in `"json"` mode, prints one JSON line to stdout with keys `text`, `tools_used`, `usage`, `cost_usd`, `thread_id`, and returns `""`; all other console output (tool lines, indicator) is suppressed in this mode.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py  (add)
def test_json_output_is_one_parseable_object(tmp_path, fake_model, monkeypatch, capsys):
    import json
    from langchain_core.messages import AIMessage
    import luna.cli as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(
        cli, "build_agent",
        lambda *a, **k: __import__("luna.agent", fromlist=["build_agent"]).build_agent(
            *a, model=fake_model(AIMessage(content="hi")),
            **{kk: vv for kk, vv in k.items() if kk != "model"},
        ),
    )
    rc = cli.main(["--json", "hello"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    data = json.loads(out.splitlines()[-1])
    assert data["text"] == "hi"
    assert "thread_id" in data
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -k json -q`
Expected: FAIL — `--json` unrecognized.

- [ ] **Step 3: `cli.py`**

`build_parser`: add

```python
    parser.add_argument(
        "--output-format", choices=["text", "json"], default="text", dest="output_format",
        help="output format for a one-shot prompt",
    )
    parser.add_argument(
        "--json", action="store_const", const="json", dest="output_format",
        help="shorthand for --output-format json",
    )
```

In `main()`, pass `output_format=args.output_format` into the `run_once(...)` call (the `if prompt:` branch).

- [ ] **Step 4: `session.py` `run_once`**

```python
def run_once(
    agent,
    prompt: str,
    *,
    thread_id: str | None = None,
    console: Console,
    input_fn: Callable[[str], str] = input,
    index: SessionIndex | None = None,
    workdir: str = ".",
    session_id: str = "",
    cfg: LunaConfig | None = None,
    output_format: str = "text",
) -> str:
    """Run a single prompt and return the final assistant text (or "" in json mode)."""
    thread_id = thread_id or _new_thread_id()
    config = {"configurable": {"thread_id": thread_id}}
    payload = {"messages": [{"role": "user", "content": prompt}]}
    rules = load_rules(workdir)
    quiet = Console(file=open(os.devnull, "w")) if output_format == "json" else console
    text, _, turn_usage, tool_names = _stream_turn(
        agent, payload, config, quiet, input_fn, rules=rules, workdir=workdir
    )
    if cfg is not None and tool_names & _MUTATING:
        _format_and_diagnose(quiet, cfg)
        _run_verification(agent, config, quiet, cfg, input_fn, rules=rules)
    if index is not None:
        index.record(thread_id, workdir, make_title(prompt))
        index.touch(thread_id)
    if output_format == "json":
        provider = cfg.provider if cfg is not None else ""
        model = cfg.model if cfg is not None else None
        overrides = cfg.pricing if cfg is not None else None
        payload_out = {
            "text": text,
            "tools_used": sorted(tool_names),
            "usage": {
                "input": turn_usage.input_tokens,
                "output": turn_usage.output_tokens,
                "total": turn_usage.total_tokens,
            },
            "cost_usd": price(provider, model, overrides)
            and (
                turn_usage.input_tokens / 1_000_000 * price(provider, model, overrides)[0]
                + turn_usage.output_tokens / 1_000_000 * price(provider, model, overrides)[1]
            ),
            "thread_id": thread_id,
        }
        print(json.dumps(payload_out))
        return ""
    return text
```

Add `import json`, `import os`, and `from luna.usage import price` (alongside the existing `usage` import) to `session.py`.

(Tidy the repeated `price(...)` call into a single local variable before building `payload_out` rather than calling it three times — write it that way, the snippet above is illustrative of the fields, not the exact final form.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/cli.py luna/session.py tests/test_cli.py
git commit -m "feat: --json output for one-shot prompts"
```

---

### Task 9: Undo rework, part 1 — git snapshot infrastructure

**Files:**
- Modify: `luna/undo.py` (new git-path functions), `luna/toolguard.py` (skip the file journal on the git path), `luna/session.py` (`begin_turn` call)
- Test: `tests/test_undo.py`

**Interfaces:**
- Consumes: `gitinfo.is_git_repo`.
- Produces:
  - `undo.begin_turn(workdir: str, session_id: str, message_count: int) -> None` — no-op outside a git repo.
  - Internal: `undo._snapshot_tree(workdir) -> str` (a tree sha), `undo._commit_tree(workdir, tree, parent) -> str`.
  - `.luna/undo/<session_id>/turns.json` — a JSON list of `{"turn": int, "pre_sha": str, "message_count": int}`, plus a sibling `redo.json` (Task 10) that this task creates empty.
  - `tool_guard(...)`: the per-tool `snapshot(...)` call is skipped when `gitinfo.is_git_repo(workdir)` is true.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_undo.py  (add)
import subprocess


def _git(tmp_path, *args):
    subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)


def test_begin_turn_creates_a_shadow_ref(tmp_path):
    from luna.undo import begin_turn

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    begin_turn(str(tmp_path), "sess1", message_count=1)

    out = subprocess.run(
        ["git", "rev-parse", "refs/luna/undo/sess1"], cwd=tmp_path, capture_output=True, text=True
    )
    assert out.returncode == 0 and out.stdout.strip()
    ledger = tmp_path / ".luna" / "undo" / "sess1" / "turns.json"
    assert ledger.is_file()


def test_begin_turn_is_a_noop_outside_git(tmp_path):
    from luna.undo import begin_turn

    begin_turn(str(tmp_path), "sess1", message_count=1)  # must not raise
    assert not (tmp_path / ".luna" / "undo").exists()


def test_begin_turn_handles_an_unborn_head(tmp_path):
    from luna.undo import begin_turn

    _git(tmp_path, "init", "-q")  # no commits yet
    begin_turn(str(tmp_path), "sess1", message_count=0)  # must not raise
    ledger = tmp_path / ".luna" / "undo" / "sess1" / "turns.json"
    assert ledger.is_file()


def test_begin_turn_does_not_touch_the_users_index_or_worktree(tmp_path):
    from luna.undo import begin_turn

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    (tmp_path / "a.txt").write_text("staged-change\n")
    _git(tmp_path, "add", "a.txt")  # user has a staged change

    begin_turn(str(tmp_path), "sess1", message_count=1)

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "M  a.txt" in status  # still staged, untouched by the snapshot
    assert (tmp_path / "a.txt").read_text() == "staged-change\n"  # worktree untouched
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_undo.py -k begin_turn -q`
Expected: FAIL — `begin_turn` doesn't exist.

- [ ] **Step 3: Implement the git snapshot functions in `luna/undo.py`**

Add near the top: `import subprocess`, `import tempfile` (alongside the existing imports), and:

```python
def _run_git(workdir: str, args: list[str], env: dict | None = None) -> str | None:
    """Run a git plumbing command; return stdout on success, ``None`` on failure."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=workdir, capture_output=True, text=True, timeout=10, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _snapshot_tree(workdir: str) -> str | None:
    """Write the current worktree to a tree object without touching the real index."""
    with tempfile.TemporaryDirectory() as tmp:
        index_file = str(Path(tmp) / "index")
        env = {**os.environ, "GIT_INDEX_FILE": index_file}
        if _run_git(workdir, ["add", "-A"], env=env) is None:
            return None
        return _run_git(workdir, ["write-tree"], env=env)


def _git_turns_dir(workdir: str, session_id: str) -> Path:
    path = Path(workdir) / ".luna" / "undo" / (session_id or "default")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json_list(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_json_list(path: Path, data: list[dict]) -> None:
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(data))


def begin_turn(workdir: str, session_id: str, message_count: int) -> None:
    """Snapshot the worktree into a shadow ref before a turn (git repos only).

    No-op outside a git repository. Never raises.
    """
    if not gitinfo.is_git_repo(workdir):
        return
    tree = _snapshot_tree(workdir)
    if tree is None:
        return
    parent = _run_git(workdir, ["rev-parse", "HEAD"])
    commit_args = ["commit-tree", tree, "-m", "luna turn snapshot"]
    if parent:
        commit_args += ["-p", parent]
    sha = _run_git(workdir, commit_args)
    if not sha:
        return
    _run_git(workdir, ["update-ref", f"refs/luna/undo/{session_id or 'default'}", sha])
    d = _git_turns_dir(workdir, session_id)
    turns = _read_json_list(d / "turns.json")
    turns.append({"turn": len(turns), "pre_sha": sha, "message_count": message_count})
    _write_json_list(d / "turns.json", turns)
    _write_json_list(d / "redo.json", [])  # a new turn clears any pending redo
```

Add `import json`, `import os` if not already present in `undo.py` (check — `os` may be new); `import contextlib` is already there. Add `from luna import gitinfo` — but `gitinfo.py` has no framework imports, so this is safe from ANY module; confirm `undo.py` importing `gitinfo` doesn't create a cycle (`gitinfo.py` imports only stdlib — no cycle).

- [ ] **Step 4: `toolguard.py` — skip the file journal on the git path**

```python
from luna import gitinfo


def tool_guard(rules: RuleSet, workdir: str, session_id: str = "", plan=None):
    use_journal = session_id and not gitinfo.is_git_repo(workdir)
    ...
        if use_journal and name in _MUTATING:
            ...
```

(Compute `use_journal` once, outside `_guard`, so it isn't re-checked per call.)

- [ ] **Step 5: `session.py` — call `begin_turn`**

In `run_repl`, right before building the non-slash turn's `payload` (after the `@agent` rewrite, before `turn_config`):

```python
        from luna import undo as undo_mod  # local import: undo already imported at module level? check first

        current_messages = agent.get_state(turn_config).values.get("messages", [])
        undo_mod.begin_turn(workdir, session_id, len(current_messages))
```

(If `undo` is already imported at module level in `session.py` for `/undo`/`/diff` support — check first; if so just call `undo.begin_turn(...)` directly without a local import. `turn_config` must be computed before this call — reorder if needed so `turn_config = {"configurable": {"thread_id": thread_id}}` comes first.)

Do the same in `run_once` before its `_stream_turn` call.

- [ ] **Step 6: Run tests**

Run: `uv run pytest -q`
Expected: PASS. Existing undo tests (file-journal path) still pass because none of those tests run inside a git repo.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/undo.py luna/toolguard.py luna/session.py tests/test_undo.py
git commit -m "feat: git tree snapshot per turn (shadow ref), file journal for non-git"
```

---

### Task 10: Undo rework, part 2 — `/undo` (files + conversation) and `/redo`

**Files:**
- Modify: `luna/undo.py` (`undo`, `redo`), `luna/commands.py` (`_undo` git-aware, new `_redo`)
- Test: `tests/test_undo.py`, `tests/test_commands.py`

**Interfaces:**
- Consumes: `agent.get_state` / `agent.update_state` (available on the `object` passed as `ctx.agent`; no framework import needed in `undo.py` itself — it takes the already-built agent as a parameter, keeping `undo.py` framework-free).
- Produces:
  - `undo.undo(workdir: str, session_id: str, agent, thread_id: str) -> str | None`.
  - `undo.redo(workdir: str, session_id: str, agent, thread_id: str) -> str | None`.
  - `commands._undo` dispatches to the git path when `gitinfo.is_git_repo(ctx.workdir)`, else the existing file-journal path (unchanged).
  - `commands._redo` — git path only; prints "redo needs a git repository" otherwise.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_undo.py  (add)
def test_undo_restores_files_and_truncates_the_conversation(tmp_path, fake_model):
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "write_file", "id": "1", "args": {"file_path": "/a.txt", "content": "v1\n"}}],
        ),
        AIMessage(content="changed a.txt"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}

    pre = agent.get_state(cfg).values.get("messages", [])
    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "change it"}]}, config=cfg)
    assert (tmp_path / "a.txt").read_text() == "v1\n"
    after = len(agent.get_state(cfg).values["messages"])
    assert after > len(pre)

    note = undo(str(tmp_path), "sess1", agent, thread_id)
    assert note is not None
    assert (tmp_path / "a.txt").read_text() == "v0\n"
    assert len(agent.get_state(cfg).values["messages"]) == len(pre)


def test_undo_with_no_turns_returns_none(tmp_path):
    from luna.undo import undo

    _git(tmp_path, "init", "-q")
    assert undo(str(tmp_path), "sess-empty", agent=None, thread_id="t") is None


def test_redo_restores_files_and_messages(tmp_path, fake_model):
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, redo, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "write_file", "id": "1", "args": {"file_path": "/a.txt", "content": "v1\n"}}],
        ),
        AIMessage(content="changed a.txt"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}

    pre = agent.get_state(cfg).values.get("messages", [])
    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "change it"}]}, config=cfg)
    after_count = len(agent.get_state(cfg).values["messages"])

    undo(str(tmp_path), "sess1", agent, thread_id)
    redo(str(tmp_path), "sess1", agent, thread_id)

    assert (tmp_path / "a.txt").read_text() == "v1\n"
    assert len(agent.get_state(cfg).values["messages"]) == after_count


def test_redo_with_nothing_to_redo_returns_none(tmp_path):
    from luna.undo import redo

    _git(tmp_path, "init", "-q")
    assert redo(str(tmp_path), "sess-empty", agent=None, thread_id="t") is None


def test_a_new_turn_clears_the_redo_stack(tmp_path, fake_model):
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, redo, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(AIMessage(content="ok")))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}
    pre = agent.get_state(cfg).values.get("messages", [])

    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "turn 1"}]}, config=cfg)
    undo(str(tmp_path), "sess1", agent, thread_id)

    begin_turn(str(tmp_path), "sess1", len(agent.get_state(cfg).values["messages"]))  # a fresh turn begins

    assert redo(str(tmp_path), "sess1", agent, thread_id) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_undo.py -k "undo_restores or redo" -q`
Expected: FAIL — `undo`/`redo` don't exist as git-path functions (the module currently only has the file-journal `undo_last`).

- [ ] **Step 3: Implement `undo` / `redo` in `luna/undo.py`**

The existing `undo_last` (file-journal) stays under its current name for the non-git path. Add:

```python
def _message_to_dict(msg) -> dict:
    """Serialise one BaseMessage well enough to rebuild it with a fresh id."""
    return {
        "type": getattr(msg, "type", "human"),
        "content": getattr(msg, "content", ""),
        "tool_calls": getattr(msg, "tool_calls", None),
        "tool_call_id": getattr(msg, "tool_call_id", None),
        "name": getattr(msg, "name", None),
    }


def _dict_to_message(data: dict):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    kind = data.get("type")
    if kind == "tool":
        return ToolMessage(
            content=data.get("content", ""),
            tool_call_id=data.get("tool_call_id") or "",
            name=data.get("name"),
        )
    if kind == "ai":
        kwargs = {"content": data.get("content", "")}
        if data.get("tool_calls"):
            kwargs["tool_calls"] = data["tool_calls"]
        return AIMessage(**kwargs)
    return HumanMessage(content=data.get("content", ""))


def undo(workdir: str, session_id: str, agent, thread_id: str) -> str | None:
    """Revert the last turn's files and truncate the conversation (git path)."""
    d = _git_turns_dir(workdir, session_id)
    turns = _read_json_list(d / "turns.json")
    if not turns:
        return None
    record = turns.pop()
    _write_json_list(d / "turns.json", turns)

    config = {"configurable": {"thread_id": thread_id}}
    post_sha = _snapshot_tree(workdir)
    state_messages = agent.get_state(config).values.get("messages", [])
    added = state_messages[record["message_count"] :]

    _run_git(workdir, ["checkout", record["pre_sha"], "--", "."])
    from langchain_core.messages import RemoveMessage

    if added:
        agent.update_state(config, {"messages": [RemoveMessage(id=m.id) for m in added]})

    redo_stack = _read_json_list(d / "redo.json")
    redo_stack.append(
        {
            "post_sha": post_sha,
            "message_count": record["message_count"],
            "messages": [_message_to_dict(m) for m in added],
        }
    )
    _write_json_list(d / "redo.json", redo_stack)
    return f"undid turn {record['turn']} — {len(added)} message(s), files restored"


def redo(workdir: str, session_id: str, agent, thread_id: str) -> str | None:
    """Re-apply the most recently undone turn's files and messages (git path)."""
    d = _git_turns_dir(workdir, session_id)
    redo_stack = _read_json_list(d / "redo.json")
    if not redo_stack:
        return None
    record = redo_stack.pop()
    _write_json_list(d / "redo.json", redo_stack)

    if record.get("post_sha"):
        _run_git(workdir, ["checkout", record["post_sha"], "--", "."])
    config = {"configurable": {"thread_id": thread_id}}
    rebuilt = [_dict_to_message(m) for m in record.get("messages", [])]
    if rebuilt:
        agent.update_state(config, {"messages": rebuilt})

    turns = _read_json_list(d / "turns.json")
    turns.append(
        {"turn": len(turns), "pre_sha": record.get("post_sha", ""), "message_count": record["message_count"]}
    )
    _write_json_list(d / "turns.json", turns)
    return "redo applied"
```

Note: `undo`/`redo` import langchain message classes LAZILY (inside the function), not at module top — `undo.py` is NOT one of the four framework-import modules, so keep the top-level import list framework-free and only pull in `langchain_core.messages` inside these two functions where an `agent` object is already being passed in from a framework-touching caller. Record this exception explicitly in the module docstring: "`undo()`/`redo()` accept an already-built agent and lazily import message classes only inside those two functions — every other function in this file is framework-free."

- [ ] **Step 4: `commands.py`**

```python
def _undo(ctx: CommandContext, arg: str) -> None:
    """Revert the last file change made this session (confirms first)."""
    from luna import gitinfo

    if gitinfo.is_git_repo(ctx.workdir):
        from luna.undo import undo as git_undo

        if ctx.input_fn is not None:
            answer = ctx.input_fn("undo the last turn (files + conversation)? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                ctx.console.print("[dim]undo cancelled[/]")
                return
        note = git_undo(ctx.workdir, ctx.session_id, ctx.agent, ctx.thread_id)
        ctx.console.print(f"[{PALETTE['blue']}]{note}[/]" if note else "[dim]nothing to undo[/]")
        return
    desc = peek_last(ctx.workdir, ctx.session_id)
    if desc is None:
        ctx.console.print("[dim]nothing to undo[/]")
        return
    if ctx.input_fn is not None:
        answer = ctx.input_fn(f"{desc}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            ctx.console.print("[dim]undo cancelled[/]")
            return
    note = undo_last(ctx.workdir, ctx.session_id)
    ctx.console.print(f"[{PALETTE['blue']}]{note}[/]" if note else "[dim]nothing to undo[/]")


def _redo(ctx: CommandContext, arg: str) -> None:
    """Re-apply the last undone turn (git repositories only)."""
    from luna import gitinfo

    if not gitinfo.is_git_repo(ctx.workdir):
        ctx.console.print("[dim]redo needs a git repository[/]")
        return
    from luna.undo import redo as git_redo

    note = git_redo(ctx.workdir, ctx.session_id, ctx.agent, ctx.thread_id)
    ctx.console.print(f"[{PALETTE['blue']}]{note}[/]" if note else "[dim]nothing to redo[/]")
```

Register `"/redo": _redo`; `HELP["/redo"] = "re-apply the last undone turn (git only)"`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/undo.py luna/commands.py tests/test_undo.py tests/test_commands.py
git commit -m "feat: /undo reverts files + conversation on git repos; /redo"
```

---

### Task 11: Undo rework, part 3 — `/diff` via git + `gc` prunes shadow refs

**Files:**
- Modify: `luna/undo.py` (`session_diff` git-aware, `gc` prunes refs), `luna/commands.py` (`_diff` unchanged call site — dispatches inside `undo.session_diff`)
- Test: `tests/test_undo.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `undo.session_diff(workdir, session_id) -> str` — git path: `git diff <earliest pre_sha in turns.json>..HEAD -- .` (fallback to `--cached`/worktree comparison per the note below) rendered as text; non-git path: unchanged (the existing per-file journal diff).
  - `undo.gc(...)` also deletes `refs/luna/undo/<id>` for every directory it removes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_undo.py  (add)
def test_session_diff_uses_git_when_available(tmp_path, fake_model):
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, session_diff

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "write_file", "id": "1", "args": {"file_path": "/a.txt", "content": "v1\n"}}],
        ),
        AIMessage(content="changed a.txt"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    cfg = {"configurable": {"thread_id": "t"}}
    pre = agent.get_state(cfg).values.get("messages", [])
    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "change it"}]}, config=cfg)

    diff = session_diff(str(tmp_path), "sess1")
    assert "v0" in diff and "v1" in diff


def test_gc_removes_the_shadow_ref_too(tmp_path):
    import os
    import time

    from luna.undo import begin_turn, gc

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    begin_turn(str(tmp_path), "old", message_count=0)

    old_dir = tmp_path / ".luna" / "undo" / "old"
    past = time.time() - 40 * 86400
    for f in old_dir.glob("*.json"):
        os.utime(f, (past, past))
    os.utime(old_dir, (past, past))

    gc(str(tmp_path), keep_days=7)

    ref = subprocess.run(
        ["git", "rev-parse", "refs/luna/undo/old"], cwd=tmp_path, capture_output=True, text=True
    )
    assert ref.returncode != 0  # the ref is gone
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_undo.py -k "session_diff_uses_git or gc_removes_the_shadow" -q`
Expected: FAIL — `session_diff` still only reads the JSON journal (empty in a git repo where no `snapshot()` calls happened per Task 9's `use_journal` change); `gc` doesn't touch refs.

- [ ] **Step 3: Implement**

`session_diff` — check the git path first:

```python
def session_diff(workdir: str, session_id: str) -> str:
    """Unified diff for this session: git tree diff, or the file-journal fallback."""
    if gitinfo.is_git_repo(workdir):
        d = _git_turns_dir(workdir, session_id)
        turns = _read_json_list(d / "turns.json")
        if not turns:
            return ""
        earliest = turns[0]["pre_sha"]
        out = _run_git(workdir, ["diff", earliest, "--", "."])
        return out or ""
    # ... existing file-journal implementation unchanged below this point
```

`gc` — after resolving `to_remove`, also drop each dir's shadow ref:

```python
    for d in to_remove:
        with contextlib.suppress(OSError):
            shutil.rmtree(d)
        _run_git(workdir, ["update-ref", "-d", f"refs/luna/undo/{d.name}"])
```

(`_run_git` already swallows failures — deleting a ref that was never created, e.g. for a session that only ever used the file journal, is a harmless no-op.) Move `gc`'s call to `_run_git` above its own definition if needed, or keep `_run_git` defined earlier in the file (Task 9 already put it near the top).

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: PASS (this is the last undo-rework task — full suite should be comfortably over 230 tests at this point; exact count depends on cumulative additions).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/undo.py tests/test_undo.py
git commit -m "feat: /diff reads the git tree diff; gc prunes shadow refs"
```

---

### Task 12: Docs + version 0.3.0

**Files:**
- Modify: `luna/__init__.py`, `pyproject.toml`, `tests/test_metadata.py`, `tests/test_cli.py`, `CHANGELOG.md`, `README.md`, `AGENTS.md`

- [ ] **Step 1: Version**

`luna/__init__.py` → `__version__ = "0.3.0"`. `pyproject.toml` → `version = "0.3.0"`. `tests/test_metadata.py` (both assertions) and `tests/test_cli.py::test_version` → `"0.3.0"`.

- [ ] **Step 2: `CHANGELOG.md`**

Add `## [0.3.0] — <today's date>` above `## [0.2.1]`, `### Добавлено` (Russian), one bullet per feature:
- диагностика после правок (`/diagnose`, `[agent] diagnose_command`) — типовые ошибки в контекст следующего хода
- LSP-навигация (`goto_definition` / `find_references` / `hover`), опционально через `luna-simple[lsp]`
- авто-форматирование тронутых файлов после правок (`[agent] format_command`)
- кастомные slash-команды из `.luna/commands/*.md` (`$ARGUMENTS`, `` !`shell` ``, `@file`)
- `/undo` теперь откатывает файлы **и** разговор к началу хода (git-репозитории), `/redo`; вне git — прежний файловый журнал
- реестр моделей `luna/models.toml`: `$`-стоимость в `/usage` и индикаторе
- `/plan` — режим только чтения без пересборки агента
- `@agent текст` — прямая делегация субагенту
- `luna "..." --json` — структурированный вывод для CI/скриптов

- [ ] **Step 3: `README.md`**

- Add a "Диагностика и форматирование" (or fold into an existing section) covering `diagnose_command`/`format_command` config + `/diagnose`.
- Document `.luna/commands/*.md` (one example file).
- Update the `/undo` description: git repos get file+conversation rewind and `/redo`; non-git keeps the old per-file behavior. Update the Ограничения section accordingly (remove the now-stale "undo journal follows the session" wording if `/redo` supersedes it — keep both facts straight: git-path is per-turn+conversation, non-git is the old per-file journal).
- Document `[model.pricing]`, the `$` in `/usage`.
- Document `/plan`, `@agent`, `--json`.
- Note the `lsp` extra: `uv pip install "luna-simple[lsp]"`.

- [ ] **Step 4: `AGENTS.md`**

Add to Структура: `luna/fmt.py`, `luna/diagnose.py`, `luna/lspnav.py`, `luna/usercmd.py`, `luna/models.toml`. Note in Соглашения: `undo.py`'s `undo()`/`redo()` are the one exception to "framework imports live in the four core modules" — they accept an already-built agent and lazily import `langchain_core.messages` only inside those two functions.

- [ ] **Step 5: Full suite + lint + commit**

```bash
uv run pytest -q && uv run ruff check luna tests && uv run ruff format --check .
git add luna/__init__.py pyproject.toml tests/test_metadata.py tests/test_cli.py CHANGELOG.md README.md AGENTS.md
git commit -m "docs: 0.3.0 — harness parity (LSP, formatters, custom commands, git undo, plan mode)"
```

---

## Self-Review

**Spec coverage:**

| Spec §3 feature | Task |
| --- | --- |
| 3.1 LSP diagnostics | Task 3 (Task 2 lays the shared hook) |
| 3.2 LSP navigation | Task 4 |
| 3.3 Formatters | Task 2 |
| 3.4 Custom slash commands | Task 5 |
| 3.5 Undo rework | Tasks 9, 10, 11 |
| 3.6 Model registry + cost | Task 1 |
| 3.7 Plan mode | Task 6 |
| 3.8 `@agent` | Task 7 |
| 3.9 `--json` | Task 8 |
| §4 config additions | Tasks 1, 2, 3, 4 |
| §5 CLI/REPL surface | Tasks 3, 5, 6, 8, 10 |
| §12 docs + version | Task 12 |

Every spec section maps to a task. The "plan-level deviation" note at the top
of this document records the one place this plan departs from the spec's
literal mechanism (touched-file tracking) while delivering the same behavior.

**Placeholder scan:** No `TBD`/`TODO`/"handle edge cases". Task 8's `run_once`
json-payload snippet has an explicit parenthetical telling the implementer to
de-duplicate the repeated `price(...)` call into a local variable — that is a
concrete instruction, not a placeholder. Task 10's message-serialisation
helpers are complete, runnable code.

**Type consistency:**
- `usage.context_window`/`price`/`indicator_line` all gain the same trailing
  `overrides: dict | None = None` parameter (Task 1); `session.py`'s two
  `indicator_line(...)` call sites and `commands.py`'s `_usage` all pass
  `config.pricing` / `ctx.config.pricing`. ✓
- `fmt.run` / `diagnose.run` share the `(command, workdir, paths) -> ...`
  shape (Tasks 2, 3). ✓
- `_format_and_diagnose(console, cfg) -> str` is defined once (Task 2) and
  extended in place (Task 3) rather than duplicated. ✓
- `DispatchResult.prompt` (Task 5) is consumed only in `run_repl`'s dispatch
  branch; no other task adds a second consumer. ✓
- `tool_guard(rules, workdir, session_id="", plan=None)` (Task 6) — Task 9
  reads `session_id`/`workdir` again for the `use_journal` computation; both
  additions are keyword-only with defaults, order-independent. ✓
- `undo.begin_turn` (Task 9), `undo.undo`/`undo.redo` (Task 10),
  `undo.session_diff`/`undo.gc` (Task 11) — all take `workdir`/`session_id`
  first, matching the existing file-journal functions' argument order. ✓
- `run_repl(..., plan_state=None)` (Task 6) — Task 7's `@agent` rewrite and
  Task 9's `begin_turn` call both sit inside the same non-slash branch of the
  loop; no task re-orders a previous task's insertion point without saying so
  (Task 9 explicitly says "reorder if needed so `turn_config` comes first").

**Scope check:** Twelve tasks, one branch — LSP (2 sub-features), formatters,
custom commands, the undo rework (3 sub-tasks), the model registry, plan mode,
`@agent`, and `--json` are all thin, independently testable slices of one
"harness parity" release. Consistent with how 0.2.0 (13 tasks) and 0.2.1 (9
tasks + a final-review fix wave) were sized and executed.

---

## Execution notes

- Task order is mostly parallel-safe by module, but land them in the written
  order: Task 2 must precede Task 3 (shared hook); Task 9 must precede Tasks
  10 and 11 (git infra first); everything else is independent of everything
  else and could in principle be reordered, but doing so is not necessary — no
  parallel implementer dispatch regardless.
- If Task 4's `multilspy` API differs from what `docs/superpowers/specs/2026-09-09-luna-parity-design.md` §2 recorded (exact request signatures, result shapes), re-verify against the installed package (`uv pip install "luna-simple[lsp]"` then `python -c "import multilspy; help(multilspy.SyncLanguageServer)"`) before writing `lspnav.py`'s request calls — the tests that don't require the extra must still pass either way.
- If Task 9's `git commit-tree` on an unborn `HEAD` (a `git init` with zero commits) fails in practice despite the spec's mitigation, `begin_turn` must degrade to a no-op for that turn (never raise) rather than block the turn — the existing `_run_git` returning `None` on any failure already gives you this for free; just confirm the test for it (`test_begin_turn_handles_an_unborn_head`) actually exercises the code path and not a false pass.
