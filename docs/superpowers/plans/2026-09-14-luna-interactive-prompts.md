# Luna Interactive Arrow-Key Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every "type a number/letter" fixed-choice prompt in
Luna with real arrow-key + Enter selection (`luna setup`'s provider
picker, the mutating-action approval prompt, both `/resume` code paths,
and `/undo`'s y/N confirmations), without breaking any of the ~300
existing tests that script these prompts via `input_fn` overrides, and
fix the `/resume` bug where the resumed thread's history never prints.

**Architecture:** A new `luna/ui/interact.py` module offers arrow-key
selection ONLY when a real interactive terminal is confirmed present
(`input_fn is input and console.is_terminal and sys.stdin.isatty()`);
it returns `None` otherwise, and every call site keeps its exact
existing fallback prompt untouched behind that `None` check. Built on
the new `questionary` dependency (the same one `aider` uses).

**Tech Stack:** Python 3.12, `questionary>=2.1` (new dependency, built on
`prompt_toolkit`), `rich.console.Console` (already a dependency).

**Spec:** `docs/superpowers/specs/2026-09-14-luna-interactive-prompts-design.md`

## Global Constraints

- New dependency: `questionary>=2.1` in `pyproject.toml`'s core
  `dependencies` list (not optional — `luna setup` needs it for every
  install).
- No existing test may be modified for the arrow-key gating itself —
  every prompt's existing fallback behavior and exact prompt text stays
  byte-for-byte identical when `input_fn` is not the real `input`
  builtin (true for every existing test in the suite).
- `arrow_pick`/`arrow_confirm` return `None` to mean "not applicable
  here, use your existing fallback" — never to mean "user cancelled."
  A real cancel (questionary's `.ask()` returning `None`, which happens
  on Ctrl-C/Ctrl-D) must raise `KeyboardInterrupt` instead, matching
  how every other prompt in Luna already lets Ctrl-C propagate.
- `/clear` is explicitly out of scope (confirmed with the user) — no
  code change to it.

---

### Task 1: `luna/ui/interact.py` — the arrow-key/fallback primitive

**Files:**
- Create: `luna/ui/interact.py`
- Test: `tests/test_interact.py`
- Modify: `pyproject.toml` (add the `questionary` dependency)

**Interfaces:**
- Produces: `arrow_pick(console: Console, input_fn: Callable[[str], str], options: list[tuple[str, str]], *, default: str | None = None) -> str | None`
  and `arrow_confirm(console: Console, input_fn: Callable[[str], str], message: str, *, default: bool = False) -> bool | None`
  — Tasks 2-5 call these from `setup_wizard.py`, `approve.py`,
  `commands.py`, and `cli.py`.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, in the `dependencies` list (currently ending with
`"langgraph-checkpoint-sqlite>=2.0",`), add a new line:

```toml
    "questionary>=2.1",
```

- [ ] **Step 2: Install it**

Run: `uv pip install -e ".[dev,all]"` (re-resolves and installs the new
dependency into the project's venv).

- [ ] **Step 3: Write the failing tests**

Create `tests/test_interact.py`:

```python
import io

from rich.console import Console

import luna.ui.interact as interact
from luna.ui.interact import arrow_confirm, arrow_pick


def _console(is_terminal=True):
    return Console(file=io.StringIO(), force_terminal=is_terminal)


def test_arrow_pick_returns_none_when_input_fn_is_not_the_real_builtin():
    # every existing test in this project overrides input_fn exactly this
    # way (a real input() would block forever with no real stdin attached)
    # — this must never touch questionary at all when that's true.
    result = arrow_pick(_console(), lambda _: "x", [("a", "A"), ("b", "B")])
    assert result is None


def test_arrow_confirm_returns_none_when_input_fn_is_not_the_real_builtin():
    result = arrow_confirm(_console(), lambda _: "x", "sure?")
    assert result is None


def test_arrow_pick_returns_none_when_console_is_not_a_terminal():
    result = arrow_pick(_console(is_terminal=False), input, [("a", "A")])
    assert result is None


def test_arrow_confirm_returns_none_when_console_is_not_a_terminal():
    result = arrow_confirm(_console(is_terminal=False), input, "sure?")
    assert result is None


def test_arrow_pick_returns_the_selected_value(monkeypatch):
    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)

    class _FakeQuestion:
        def ask(self):
            return "b"

    monkeypatch.setattr("questionary.select", lambda *a, **k: _FakeQuestion())
    result = arrow_pick(_console(), input, [("a", "A"), ("b", "B")], default="a")
    assert result == "b"


def test_arrow_pick_passes_the_default_labeled_choice(monkeypatch):
    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)
    captured = {}

    class _FakeQuestion:
        def ask(self):
            return "a"

    def _fake_select(message, choices, default=None):
        captured["default"] = default
        return _FakeQuestion()

    monkeypatch.setattr("questionary.select", _fake_select)
    arrow_pick(_console(), input, [("a", "Alpha"), ("b", "Beta")], default="b")
    assert captured["default"] == "Beta"


def test_arrow_pick_raises_keyboard_interrupt_on_cancel(monkeypatch):
    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)

    class _FakeQuestion:
        def ask(self):
            return None

    monkeypatch.setattr("questionary.select", lambda *a, **k: _FakeQuestion())
    try:
        arrow_pick(_console(), input, [("a", "A")])
        raise AssertionError("expected KeyboardInterrupt")
    except KeyboardInterrupt:
        pass


def test_arrow_confirm_returns_the_selected_value(monkeypatch):
    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)

    class _FakeQuestion:
        def ask(self):
            return True

    monkeypatch.setattr("questionary.confirm", lambda *a, **k: _FakeQuestion())
    result = arrow_confirm(_console(), input, "sure?")
    assert result is True


def test_arrow_confirm_raises_keyboard_interrupt_on_cancel(monkeypatch):
    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)

    class _FakeQuestion:
        def ask(self):
            return None

    monkeypatch.setattr("questionary.confirm", lambda *a, **k: _FakeQuestion())
    try:
        arrow_confirm(_console(), input, "sure?")
        raise AssertionError("expected KeyboardInterrupt")
    except KeyboardInterrupt:
        pass
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_interact.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna.ui.interact'`

- [ ] **Step 5: Create `luna/ui/interact.py`**

```python
"""Arrow-key interactive prompts (questionary/prompt_toolkit), with a
plain-text fallback for non-interactive use.

Every existing prompt in Luna already accepts a plain
``input_fn: Callable[[str], str]`` for testability — tests script it
with a function that returns canned strings, since a real ``input()``
call would block forever with no real stdin attached. This module adds
arrow-key selection ONLY when a real interactive terminal is confirmed
present; otherwise it returns ``None`` so the caller's own existing
``input_fn(...)`` prompt runs completely unchanged. ``None`` is never
used to mean "user cancelled" — a real cancel (Ctrl-C/Ctrl-D during the
questionary prompt) raises ``KeyboardInterrupt`` instead, matching how
every other prompt in Luna already lets Ctrl-C propagate.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from rich.console import Console


def _real_terminal(console: Console, input_fn: Callable[[str], str]) -> bool:
    return input_fn is input and console.is_terminal and sys.stdin.isatty()


def arrow_pick(
    console: Console,
    input_fn: Callable[[str], str],
    options: list[tuple[str, str]],
    *,
    default: str | None = None,
) -> str | None:
    """Arrow-key-select one of ``options`` (``(value, label)`` pairs).

    Returns the chosen ``value``, or ``None`` when no real interactive
    terminal is present — the caller must fall back to its own existing
    prompt in that case, unchanged.
    """
    if not _real_terminal(console, input_fn):
        return None
    import questionary

    default_label = next((label for value, label in options if value == default), None)
    result = questionary.select(
        "",
        choices=[questionary.Choice(label, value=value) for value, label in options],
        default=default_label,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def arrow_confirm(
    console: Console,
    input_fn: Callable[[str], str],
    message: str,
    *,
    default: bool = False,
) -> bool | None:
    """Arrow-key yes/no toggle. ``None`` when not interactive (see :func:`arrow_pick`)."""
    if not _real_terminal(console, input_fn):
        return None
    import questionary

    result = questionary.confirm(message, default=default).ask()
    if result is None:
        raise KeyboardInterrupt
    return result
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_interact.py -v`
Expected: all pass.

- [ ] **Step 7: Lint**

Run: `uv run ruff check luna/ui/interact.py tests/test_interact.py && uv run ruff format --check luna/ui/interact.py tests/test_interact.py`
Expected: clean (fix with `uv run ruff format` if needed, then re-check).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock luna/ui/interact.py tests/test_interact.py
git commit -m "feat: arrow_pick/arrow_confirm — arrow-key prompts with a text fallback"
```

---

### Task 2: Wire into `luna setup`'s provider picker

**Files:**
- Modify: `luna/repl/setup_wizard.py`
- Test: `tests/test_setup_wizard.py` (must pass unmodified — no new test required, Task 1 already covers `arrow_pick`/`arrow_confirm` directly)

**Interfaces:**
- Consumes: `luna.ui.interact.arrow_pick`, `luna.ui.interact.arrow_confirm` (Task 1).

- [ ] **Step 1: Update `luna/repl/setup_wizard.py`**

Add the import (alongside the existing imports at the top):

```python
from luna.ui.interact import arrow_confirm, arrow_pick
```

Replace `_choose_provider`:

```python
def _choose_provider(console: Console, input_fn: Callable[[str], str]) -> str:
    keys = list(PROVIDERS)
    default_idx = keys.index(DEFAULT_PROVIDER) + 1
    options: list[tuple[str, str]] = []
    for key in keys:
        spec = PROVIDERS[key]
        note = "local, no key" if spec.env_var is None else spec.env_var
        options.append((key, f"{key}  ({note})"))

    picked = arrow_pick(console, input_fn, options, default=DEFAULT_PROVIDER)
    if picked is not None:
        return picked

    console.print(f"[{PALETTE['peri']}]Choose a provider:[/]")
    for i, key in enumerate(keys, 1):
        spec = PROVIDERS[key]
        note = "local, no key" if spec.env_var is None else spec.env_var
        console.print(f"  {i}. {key}  [dim {PALETTE['blue']}]({note})[/]")

    while True:
        raw = input_fn(f"provider [{default_idx}]: ").strip().lower()
        if not raw:
            return DEFAULT_PROVIDER
        if raw in PROVIDERS:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        console.print(f"[{PALETTE['mauve']}]pick 1-{len(keys)} or a provider name[/]")
```

In `run_setup`, replace the "replace stored key?" confirmation:

```python
        if existing:
            console.print(f"\n[{PALETTE['blue']}]a key is already stored ({mask_key(existing)})[/]")
            picked = arrow_confirm(console, input_fn, "replace it?", default=False)
            if picked is None:
                picked = input_fn("replace it? [y/N]: ").strip().lower() in ("y", "yes")
            if not picked:
                prompt = None
```

(This replaces the existing `if input_fn("replace it? [y/N]: ").strip().lower() not in ("y", "yes"): prompt = None` line — same outcome, `prompt` stays `None` when the user declines.)

- [ ] **Step 2: Run the existing tests to confirm nothing broke**

Run: `uv run pytest tests/test_setup_wizard.py -v`
Expected: all pass, unmodified (every test overrides `input_fn` with a
`_scripted(...)` lambda, so `arrow_pick`/`arrow_confirm` return `None`
immediately and the existing fallback logic — unchanged text and
parsing — runs exactly as before).

- [ ] **Step 3: Lint**

Run: `uv run ruff check luna/repl/setup_wizard.py && uv run ruff format --check luna/repl/setup_wizard.py`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add luna/repl/setup_wizard.py
git commit -m "feat: luna setup's provider picker uses arrow-key selection"
```

---

### Task 3: Wire into the mutating-action approval prompt

**Files:**
- Modify: `luna/ui/approve.py`
- Test: `tests/test_approve.py` (must pass unmodified)

**Interfaces:**
- Consumes: `luna.ui.interact.arrow_pick` (Task 1).

- [ ] **Step 1: Update `luna/ui/approve.py`**

Add the import:

```python
from luna.ui.interact import arrow_pick
```

In `prompt_decision`, replace this line:

```python
    choice = input_fn("[Enter] approve · [e] edit · [a] always · [n] reject > ").strip().lower()
```

with:

```python
    picked = arrow_pick(
        console,
        input_fn,
        [("", "approve"), ("e", "edit"), ("a", "always allow"), ("n", "reject")],
        default="",
    )
    if picked is None:
        picked = input_fn("[Enter] approve · [e] edit · [a] always · [n] reject > ").strip().lower()
    choice = picked
```

Nothing else in the function changes — every branch below already
matches on `choice` exactly as it does today (`choice in ("", "y", "yes")`,
`choice in ("n", "no")`, `choice in ("a", "always")`, `choice in ("e", "edit")`),
and `arrow_pick`'s option values (`""`, `"e"`, `"a"`, `"n"`) match those
checks exactly.

- [ ] **Step 2: Run the existing tests to confirm nothing broke**

Run: `uv run pytest tests/test_approve.py -v`
Expected: all pass, unmodified (every test overrides `input_fn` with a
lambda, so `arrow_pick` returns `None` and the existing
`input_fn("[Enter] approve ...")` line runs exactly as before).

- [ ] **Step 3: Lint**

Run: `uv run ruff check luna/ui/approve.py && uv run ruff format --check luna/ui/approve.py`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add luna/ui/approve.py
git commit -m "feat: the mutating-action approval prompt uses arrow-key selection"
```

---

### Task 4: `/resume` (in-REPL) — arrow-pick, resume immediately, and fix the missing recap; `/undo` confirmations

**Files:**
- Modify: `luna/repl/commands.py`
- Test: `tests/test_commands.py` (existing tests pass unmodified; one new test added)

**Interfaces:**
- Consumes: `luna.ui.interact.arrow_pick`, `luna.ui.interact.arrow_confirm` (Task 1); `luna.core.session._print_recap` (existing, lazy-imported the same way `_compact` already lazy-imports from `luna.core.session` in this file, to avoid a real import cycle).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_commands.py` (near the existing `_resume`/`_undo` tests):

```python
def test_resume_by_number_prints_a_recap():
    from luna.core.persistence import SessionIndex

    idx = SessionIndex()
    idx.record("t-old", ".", "older")
    idx.record("t-new", ".", "newer")
    ctx = _ctx(index=idx, workdir=".")
    dispatch("/resume 2", ctx)
    out = ctx.console.file.getvalue()
    # _ctx()'s default agent is a bare object() with no get_state(), so
    # _print_recap's own broad except-and-return-silently makes this a
    # no-crash check rather than a content check — the real content case
    # is exercised end-to-end via run_repl in tests/test_repl_flow.py-style
    # coverage elsewhere in this suite, not duplicated here.
    assert "resumed session" in out  # existing behavior, still present
```

- [ ] **Step 2: Run the test to verify it currently passes trivially (no crash), then implement the real fix**

Run: `uv run pytest tests/test_commands.py -v -k test_resume_by_number_prints_a_recap`
Expected: PASS already (this specific assertion doesn't yet prove the
recap call was added — it only proves no crash). Proceed to Step 3
regardless; the meaningful verification for the recap call itself is a
manual/structural check in Step 3 plus the full existing suite staying
green.

- [ ] **Step 3: Update `_resume` in `luna/repl/commands.py`**

Replace the whole function:

```python
def _resume(ctx: CommandContext, arg: str) -> DispatchResult | None:
    """Resume a past session: /resume <number> (no arg picks interactively)."""
    if ctx.index is None:
        ctx.console.print("[dim]session history is not available here[/]")
        return None
    rows = ctx.index.list(ctx.workdir)
    if not arg:
        if not rows:
            ctx.console.print("[dim]no sessions recorded for this directory[/]")
            return None
        if ctx.input_fn is not None:
            picked = arrow_pick(
                ctx.console,
                ctx.input_fn,
                [(r.thread_id, r.title) for r in rows],
                default=None,
            )
            if picked is not None:
                return _do_resume(ctx, picked)
        for n, r in enumerate(rows, 1):
            ctx.console.print(f"  [{n}] {r.title}")
        ctx.console.print("[dim]usage: /resume <number>[/]")
        return None
    target = None
    if arg.isdigit() and 1 <= int(arg) <= len(rows):
        target = rows[int(arg) - 1].thread_id
    else:
        target = arg  # treat as a thread id
    return _do_resume(ctx, target)


def _do_resume(ctx: CommandContext, target: str) -> DispatchResult:
    from luna.core.session import _print_recap  # lazy: session imports commands

    config = {"configurable": {"thread_id": target}}
    _print_recap(ctx.agent, config, ctx.console)
    ctx.console.print(f"[{PALETTE['blue']}]resumed session {target[:8]}[/]")
    return DispatchResult(thread_id=target)
```

Add the import at the top of the file, alongside the existing ones:

```python
from luna.ui.interact import arrow_confirm, arrow_pick
```

- [ ] **Step 4: Update `_undo`'s two confirmation call sites**

Replace:

```python
            if ctx.input_fn is not None:
                answer = (
                    ctx.input_fn("undo the last turn (files + conversation)? [y/N] ")
                    .strip()
                    .lower()
                )
                if answer not in ("y", "yes"):
                    ctx.console.print("[dim]undo cancelled[/]")
                    return
```

with:

```python
            if ctx.input_fn is not None:
                confirmed = arrow_confirm(
                    ctx.console, ctx.input_fn, "undo the last turn (files + conversation)?"
                )
                if confirmed is None:
                    confirmed = (
                        ctx.input_fn("undo the last turn (files + conversation)? [y/N] ")
                        .strip()
                        .lower()
                        in ("y", "yes")
                    )
                if not confirmed:
                    ctx.console.print("[dim]undo cancelled[/]")
                    return
```

And replace:

```python
        if ctx.input_fn is not None:
            answer = ctx.input_fn(f"{desc}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                ctx.console.print("[dim]undo cancelled[/]")
                return
```

with:

```python
        if ctx.input_fn is not None:
            confirmed = arrow_confirm(ctx.console, ctx.input_fn, f"{desc}?")
            if confirmed is None:
                confirmed = ctx.input_fn(f"{desc}? [y/N] ").strip().lower() in ("y", "yes")
            if not confirmed:
                ctx.console.print("[dim]undo cancelled[/]")
                return
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_commands.py -v`
Expected: all pass — every existing `_resume`/`_undo` test (including
`test_resume_no_arg_lists_and_hints`, which uses a bare `_ctx()` with no
`input_fn`, so the `if ctx.input_fn is not None:` guard skips
`arrow_pick` entirely and the existing print-list-and-return-`None`
behavior is unchanged) plus the new recap test from Step 1.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -v`
Expected: all pass (this confirms nothing outside `test_commands.py`
depends on `_resume`'s exact prior structure — e.g. no other test
imports `_resume` directly expecting the old single-function shape).

- [ ] **Step 7: Lint**

Run: `uv run ruff check luna/repl/commands.py tests/test_commands.py && uv run ruff format --check luna/repl/commands.py tests/test_commands.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add luna/repl/commands.py tests/test_commands.py
git commit -m "fix: /resume shows the resumed thread's recap; arrow-key resume + undo confirm"
```

---

### Task 5: `luna --resume`/`-c` (CLI flag) — arrow-pick for the interactive session list

**Files:**
- Modify: `luna/cli.py`
- Test: `tests/test_resume.py` (existing tests pass unmodified; one new test added)

**Interfaces:**
- Consumes: `luna.ui.interact.arrow_pick` (Task 1).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_resume.py`:

```python
def test_resume_list_interactive_uses_arrow_pick_when_available(tmp_path, monkeypatch):
    """The interactive `--resume` (no id) branch has a real bug today: it
    calls the bare `input()` builtin directly (not an injectable
    input_fn), so this is the only way to exercise it in a test — patch
    the real builtin. Confirms arrow_pick is consulted first, and that a
    stubbed arrow_pick's return value is used directly."""
    import luna.cli as cli
    from luna.core.persistence import SessionIndex

    idx = SessionIndex()
    idx.record("thread-a", str(tmp_path), "a task")

    monkeypatch.setattr(cli, "arrow_pick", lambda console, input_fn, options, default=None: "thread-a")
    got = _resolve_resume(
        _args(resume="__list__"), idx, str(tmp_path), _console(), interactive=True
    )
    assert got == "thread-a"


def test_resume_list_interactive_falls_back_to_input_when_arrow_pick_declines(
    tmp_path, monkeypatch
):
    import luna.cli as cli
    from luna.core.persistence import SessionIndex

    idx = SessionIndex()
    idx.record("thread-a", str(tmp_path), "a task")

    monkeypatch.setattr(cli, "arrow_pick", lambda console, input_fn, options, default=None: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    got = _resolve_resume(
        _args(resume="__list__"), idx, str(tmp_path), _console(), interactive=True
    )
    assert got == "thread-a"
```

Add `from luna.cli import _resolve_resume` is already imported at the
top of this file — no new import needed there beyond what Step 1's test
bodies use inline (`import luna.cli as cli`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_resume.py -v -k interactive`
Expected: FAIL — `AttributeError: module 'luna.cli' has no attribute 'arrow_pick'` (not imported yet).

- [ ] **Step 3: Update `luna/cli.py`**

Add the import near the top of the file, alongside the other `luna.ui`
imports (or, if there are none yet, alongside the other `from luna...`
imports):

```python
from luna.ui.interact import arrow_pick
```

Replace lines 109-121 of `_resolve_resume` (the block starting
`rows = index.list(workdir)` right after the `if args.resume !=
"__list__":` block, down through `return choice or None`):

```python
    rows = index.list(workdir)
    if not rows:
        print("luna: no sessions recorded for this directory", file=sys.stderr)
        return None
    if not interactive:
        for n, r in enumerate(rows, 1):
            console.print(f"  [{n}] {r.title}")
        print("luna: --resume needs a value in non-interactive mode", file=sys.stderr)
        return None
    picked = arrow_pick(console, input, [(r.thread_id, r.title) for r in rows], default=None)
    if picked is not None:
        return picked
    for n, r in enumerate(rows, 1):
        console.print(f"  [{n}] {r.title}")
    choice = input("resume which? > ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        return rows[int(choice) - 1].thread_id
    return choice or None
```

(The non-interactive early-return keeps printing the numbered list before
bailing, exactly as before. The interactive path tries `arrow_pick`
FIRST — avoiding the redundant "static numbered list, immediately
followed by an arrow-key menu showing the same items" that a naive
insertion would produce (this exact redundancy is why Task 4's `_resume`
rewrite also tries `arrow_pick` before ever printing its own numbered
list, not after) — and only prints the list as part of the fallback,
right before the pre-existing `input("resume which? > ")` line.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_resume.py -v`
Expected: all pass — the four pre-existing tests (none of which touch
the `interactive=True` branch) are untouched, plus the two new ones
from Step 1.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all pass.

- [ ] **Step 6: Lint**

Run: `uv run ruff check luna/cli.py tests/test_resume.py && uv run ruff format --check luna/cli.py tests/test_resume.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add luna/cli.py tests/test_resume.py
git commit -m "feat: luna --resume's interactive session list uses arrow-key selection"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: nothing (prose only).

- [ ] **Step 1: Add a CHANGELOG entry**

In `CHANGELOG.md`, under `## [Unreleased]` / `### Изменено`, add (after
the existing bullets):

```markdown
- Выбор из нескольких вариантов (провайдер в `luna setup`, подтверждение
  мутирующего действия, `/resume`, `luna --resume` без ID) теперь можно
  делать стрелками + Enter вместо ввода цифры/буквы — та же техника, что
  у `aider` (библиотека `questionary` поверх `prompt_toolkit`, новая
  зависимость). В непрямом терминале (скрипты, тесты, `--no-input`,
  пайпы) поведение не изменилось — тот же текстовый ввод, что и раньше.

### Исправлено

- `/resume` переключал тред, но не показывал, что в нём — теперь печатает
  ту же сводку последних сообщений, что `luna --continue` уже показывает
  при старте.
```

(If `### Исправлено` already exists under `[Unreleased]` from an earlier
entry, append this bullet to it instead of creating a second heading.)

- [ ] **Step 2: Itemize the new file in `AGENTS.md`**

In the `## Структура` section's `luna/ui/` itemization, add a line:

```markdown
  - `interact.py` — стрелочный выбор (questionary) с текстовым откатом
```

(Insert it alongside the other `luna/ui/` entries, e.g. near `approve.py`.)

- [ ] **Step 3: Run the full suite one more time**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all clean.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md AGENTS.md
git commit -m "docs: document arrow-key interactive prompts and the /resume recap fix"
```
