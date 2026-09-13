# Luna Interactive Arrow-Key Prompts — Design

## Goal

Replace "type a number/letter" prompts with real arrow-key + Enter
selection wherever Luna asks the user to pick one of a few options — the
same interaction real competitor CLIs and other Python coding agents
(`aider`) already use — while never breaking Luna's existing
non-interactive paths (tests, `--no-input`, piped stdin), which all
already work by overriding a plain `input_fn: Callable[[str], str]`
callable.

Also fixes a real bug found during the audit for this work: `/resume
<n>` switches the active thread but never shows what's in it — unlike
`luna --continue`'s own startup recap, which does.

## Scope (agreed with the user)

Arrow-key selection everywhere a fixed set of choices is offered:
1. `luna setup`'s provider picker (`setup_wizard._choose_provider`).
2. The mutating-action approval prompt (`approve.prompt_decision`) —
   `[Enter] approve · [e] edit · [a] always · [n] reject`.
3. `/resume` with no argument (the in-REPL slash command,
   `luna/repl/commands.py::_resume`) — currently prints a numbered list
   and tells the user to re-run `/resume <n>`; becomes pick-and-resume
   in one step (and gets the missing recap fix, see below).
3b. `luna --resume`/`-c` with no ID at CLI startup
   (`luna/cli.py::_resolve_resume`, the `"__list__"` branch) — a
   *separate* code path from #3 (CLI flag vs. in-REPL command), found
   during the audit for this spec: it has the exact same numbered-list
   problem, and its `input("resume which? > ")` call (line 118) is a
   *hardcoded* call to the real `input` builtin — not even threaded
   through an injectable `input_fn` — so today it has **zero** test
   coverage for its interactive branch at all (the one existing test
   that touches this function, `test_resume_list_non_interactive_returns_none`
   in `tests/test_resume.py`, deliberately passes `interactive=False` to
   avoid ever reaching it). Since this function's caller
   (`luna/cli.py`, around line 390) only reaches this branch after
   already computing `interactive = console.is_terminal and
   sys.stdin.isatty() and not args.no_input` (line 359), calling
   `arrow_pick(console, input, options, ...)` here needs no extra
   gating — `interactive` being true already implies `arrow_pick`'s own
   internal check would pass too.
4. The `y/N` confirmations in `/undo` (two call sites: the git-repo path
   and the file-journal path) and `luna setup`'s "replace stored key?".

`/clear` is explicitly OUT of scope — confirmed with the user it keeps
its current meaning (clear the screen only; `/new` is the
context-reset command), no code change needed there.

## New dependency: `questionary`

No existing dependency provides arrow-key terminal selection (`rich`
does not; building raw-terminal key handling by hand is real,
error-prone, platform-specific work: distinct escape sequences per
terminal, a separate Windows code path, and correct cleanup of terminal
state on crash/Ctrl-C). `questionary` (built on `prompt_toolkit`) is the
established choice for this in the Python CLI ecosystem and is already
what `aider` — the closest comparable tool — uses. Add to
`pyproject.toml`'s core `dependencies` (not optional: the picker is used
by `luna setup`, which every fresh install goes through):

```toml
"questionary>=2.1",
```

Verified directly (installed in a scratch venv, not assumed from docs):
`questionary.select(message, choices=[questionary.Choice(title, value=...), ...], default=...)`
and `questionary.confirm(message, default=bool)` both return a
`Question` object; `.ask()` runs it and returns the chosen value, or
`None` — importantly, `Question.ask()` catches `KeyboardInterrupt`
itself (prints its own cancellation message) and returns `None` rather
than propagating the exception, so Luna must translate a `None` result
back into a real `KeyboardInterrupt` to match how every other prompt in
Luna already lets Ctrl-C propagate to the REPL's own top-level handler.

## Core design: additive fallback, not a replacement

The critical constraint: roughly 300 existing tests script every one of
these four prompts today by overriding `input_fn` with a function that
returns scripted strings (`"", "n", "nope"`, a digit, `"y"`, etc.) — none
of that can go through a real interactive `questionary` prompt (there is
no real terminal in a test process), and rewriting ~300 tests to some new
scripting mechanism is not this task's job and was not asked for.

The fix: a new module, `luna/ui/interact.py`, offers arrow-key selection
**only** when a real interactive terminal is confirmed present, and
returns a sentinel (`None`) otherwise — callers keep their exact existing
`input_fn(...)` fallback call, completely unchanged, when that sentinel
comes back. Since every existing test overrides `input_fn` away from the
real `input` builtin (there is no other way to script a test — `input()`
blocks forever on a real stdin read), gating on `input_fn is input` is a
reliable, already-implicit signal that costs nothing to check and needs
no test changes:

```python
def _real_terminal(console: Console, input_fn: Callable[[str], str]) -> bool:
    import sys
    return input_fn is input and console.is_terminal and sys.stdin.isatty()
```

This mirrors `luna/cli.py`'s own existing `interactive = console.is_terminal
and sys.stdin.isatty() and not args.no_input` check (line 359) — the same
three-part test already used to decide whether `run_setup`/the REPL
itself should even start interactively.

Two functions, both following this pattern:

```python
def arrow_pick(
    console: Console,
    input_fn: Callable[[str], str],
    options: list[tuple[str, str]],  # (value, label) — value is whatever
                                       # the CALLER's existing code already
                                       # branches on (e.g. "", "e", "a", "n")
    *,
    default: str | None = None,
) -> str | None:
    """Arrow-key-select one of `options`, returning its `value`.

    Returns None when no real interactive terminal is present — the
    caller's own existing `input_fn(...)` prompt runs unchanged in that
    case (this function must never be the only way to get an answer).
    """

def arrow_confirm(
    console: Console,
    input_fn: Callable[[str], str],
    message: str,
    *,
    default: bool = False,
) -> bool | None:
    """Arrow-key yes/no toggle. None when not interactive (see arrow_pick)."""
```

Both raise `KeyboardInterrupt` when `questionary` reports a `None`
result (Ctrl-C/Ctrl-D during the prompt) — they never return `None` to
mean "user cancelled"; `None` exclusively means "not applicable here, do
your normal fallback prompt instead." This distinction matters: a caller
must never mistake "cancelled" for "fall through to a second prompt."

Every call site keeps its EXACT existing fallback prompt text and
accepted-answer parsing. `arrow_pick`/`arrow_confirm` is inserted as a
single new line before the existing `input_fn(...)` call, guarded so it
only ever replaces the interactive path:

```python
picked = arrow_pick(console, input_fn, [...], default=...)
if picked is None:
    picked = input_fn("existing prompt text unchanged")...  # untouched
```

This means **zero existing tests change** for this feature — every one
of them overrides `input_fn`, so `arrow_pick`/`arrow_confirm` returns
`None` immediately (before ever importing `questionary`) and the
existing, already-tested fallback code runs exactly as it does today.
New tests only need to cover `arrow_pick`/`arrow_confirm` themselves
(the gating logic and the `None`-means-cancelled translation) plus one
end-to-end check per call site that the fallback path is reached when
`input_fn` is overridden (already implicitly proven by every existing
test continuing to pass unmodified, but an explicit assertion is cheap
insurance).

## Per-call-site changes

### 1. `luna/repl/setup_wizard.py::_choose_provider`

Options: `(key, f"{key}  ({note})")` for each provider, `default=DEFAULT_PROVIDER`.
On a `None` result, the existing numbered-list-print-then-loop code runs
unchanged.

Also: the "replace stored key? [y/N]" prompt (`run_setup`, line 68)
becomes `arrow_confirm(console, input_fn, "replace stored key?", default=False)`
with the same None-falls-through-to-existing-prompt pattern.

### 2. `luna/ui/approve.py::prompt_decision`

Options: `[("", "approve"), ("e", "edit"), ("a", "always"), ("n", "reject")]`,
`default=""`. The picked/typed `choice` variable feeds into the EXACT
SAME existing `if choice in ("", "y", "yes"): ...` / `elif choice in
("n", "no"): ...` / etc. branching already in this function — the
follow-up free-text prompts inside those branches (a rejection reason,
an edited rule, a replacement command) are unchanged; only the single
front-door choice becomes arrow-selectable.

### 3. `luna/repl/commands.py::_resume`

Current behavior with no `arg`: print a numbered list, tell the user to
re-run `/resume <n>`. New behavior: when a real terminal is present,
`arrow_pick` the session directly from `[(r.thread_id, r.title) for r in
rows]` and resume it immediately — no second round-trip. When not
interactive, the existing print-list-and-ask-to-retype behavior is
unchanged.

**Bug fix, both paths** (arg-based and picker-based): after resuming,
call the existing `_print_recap` helper (already used at REPL startup
for `luna --continue`, currently never called from `/resume`) so the
user actually sees what's in the thread they just switched to.
`_print_recap` lives in `luna/core/session.py`; `_resume` lives in
`luna/repl/commands.py` — `_resume` cannot call it directly (`commands.py`
already lazy-imports from `luna.core.session` elsewhere, e.g. `_compact`,
to avoid a real import cycle — follow that exact pattern). `_resume`
already has everything `_print_recap(agent, config, console, keep=6)`
needs: `ctx.agent`, `ctx.console`, and the resolved `target` thread id to
build `{"configurable": {"thread_id": target}}` — no change to
`run_repl` itself is needed, `_resume` calls it directly before
returning its `DispatchResult`.

### 3b. `luna/cli.py::_resolve_resume`

In the `"__list__"` + `interactive` branch (lines 109-121): replace the
`input("resume which? > ")` call with `arrow_pick(console, input,
[(r.thread_id, r.title) for r in rows], default=None)`, falling back to
the exact existing `input("resume which? > ")` prompt (unchanged parsing:
digit-as-index, else raw text-as-thread-id, else `None`) when
`arrow_pick` returns `None`.

### 4. `luna/repl/commands.py::_undo`

Both confirmation call sites (the git-repo path's "undo the last turn
(files + conversation)?" and the file-journal path's `f"{desc}?"`) get
an `arrow_confirm(...)` check before their existing `ctx.input_fn(...)`
call, same None-falls-through pattern. Both already guard with `if
ctx.input_fn is not None:` — `arrow_confirm` only runs inside that same
guard (never called with `input_fn=None`).

## Testing

New `tests/test_interact.py`:
- `arrow_pick`/`arrow_confirm` return `None` immediately (no
  `questionary` import, verified via `sys.modules` not gaining a
  `questionary` entry) whenever `input_fn` is not literally the `input`
  builtin — covers the test/non-interactive case that matters for every
  other test in the suite.
- `arrow_pick`/`arrow_confirm` return `None` when `console.is_terminal`
  is `False`, even if `input_fn is input`.
- A `questionary`-facing unit test that monkeypatches
  `questionary.select`/`questionary.confirm` (not real terminal I/O) to
  return a scripted `Question`-like stub, verifying: a normal pick
  returns the right `value`; a stub `.ask()` returning `None` raises
  `KeyboardInterrupt`, not a silent `None`.

Existing test files — confirmed by grep, not assumed:
`tests/test_setup_wizard.py` (`_choose_provider`), `tests/test_approve.py`
(`prompt_decision`), `tests/test_commands.py` (both the in-REPL
`/resume` slash command and `_undo`'s confirmations), `tests/test_resume.py`
(`cli.py::_resolve_resume` — note this file is about the CLI flag path,
*not* the slash command, despite the similar name) — are **not modified**
for the arrow-key gating itself, per "Core design" above. Two genuinely
**new** tests are added on top, not replacing anything:
- `tests/test_commands.py`: `/resume <n>` (and the new picker path) now
  prints the resumed thread's recent-message recap — assert the recap
  text appears after resuming, where today's tests only check the
  `thread_id` changed.
- `tests/test_resume.py`: the interactive `"__list__"` branch of
  `_resolve_resume` had zero coverage before this change (see #3b) —
  add a test with `interactive=True` and a scripted `input_fn`-equivalent
  (monkeypatch the real `input` builtin, since this call site has no
  injectable parameter) confirming the existing numbered-choice parsing
  still works when `arrow_pick` returns `None` (i.e. in a test process).

The plan's task-level verification is: run the full existing suite after
each call-site change and confirm the pass count only grows by the new
tests above — no existing test broken, none skipped/removed.

## Non-goals

- No change to `/clear` (confirmed with the user).
- No arrow-key treatment for free-text answers (a rejection reason, an
  edited shell command, an edited permission rule, a model name) — only
  fixed-choice prompts.
- No change to the REPL's own main input line (`luna › `) — that stays
  a normal text prompt; only follow-up choice/confirm prompts change.
