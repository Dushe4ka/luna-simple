# Luna Competitive Feature Set — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add nine table-stakes coding-agent capabilities Luna lacks — session persistence, context/token metering, undo/diff, `@file`, permission rules, memory tiers, post-edit verification, `luna init`, model hot-swap — as thin isolated modules.

**Architecture:** Each feature is one small module under `luna/`. deepagents/langgraph imports stay confined to `luna/agent.py`, `luna/session.py`, `luna/persistence.py`, and one new middleware module (`luna/toolguard.py`). Slash-command handling is refactored out of `session.py` into `luna/commands.py`. Every task is TDD, keeps `ruff` clean, and ends with a green `pytest`.

**Tech Stack:** Python 3.11+, deepagents ~=0.7.13, langchain ~=1.4, langgraph ~=1.2, `langgraph-checkpoint-sqlite`, rich, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-07-luna-competitive-features-design.md` — read it alongside this plan.

## Global Constraints

- Python **3.11+**, PEP 8 / PEP 257, `ruff check .` and `ruff format --check .` must pass.
- All `deepagents` / `langgraph` / `langchain_mcp_adapters` imports live only in `luna/agent.py`, `luna/session.py`, `luna/persistence.py`, `luna/toolguard.py`.
- Model IDs live in `luna/providers.py` or config — never in agent logic.
- Tests never hit the network — use the `FakeToolCallingModel` fixture (`tests/conftest.py`); the autouse `isolated_config_home` fixture already redirects `XDG_CONFIG_HOME` and `Path.home()`.
- New CLI exit codes: unchanged (0 ok / 1 runtime / 2 usage-config / 130 interrupted).
- Commit after every task with a `feat:` / `refactor:` / `chore:` prefix; end each commit message with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Work happens on branch `feat/competitive-features` (already created).

---

### Task 1: Ignore `.luna/`, add the SQLite checkpointer dependency

**Files:**
- Modify: `.gitignore`
- Modify: `pyproject.toml:24-30` (base `dependencies`)

**Interfaces:**
- Consumes: nothing.
- Produces: `langgraph.checkpoint.sqlite.SqliteSaver` is importable after `uv pip install -e ".[dev,all]"`.

- [ ] **Step 1: Add `.luna/` to `.gitignore`**

Append to `.gitignore`:

```
# Luna per-project state (undo journal, permissions, memory)
.luna/
```

- [ ] **Step 2: Add the dependency**

In `pyproject.toml`, add to the `dependencies` list (base, not an extra):

```toml
    "langgraph-checkpoint-sqlite>=2.0",
```

- [ ] **Step 3: Reinstall and verify the import**

Run: `uv pip install -e ".[dev,all]" && uv run python -c "from langgraph.checkpoint.sqlite import SqliteSaver; print('ok')"`
Expected: prints `ok`. If the version floor is wrong, run `uv run python -c "import langgraph.checkpoint.sqlite"` and adjust the floor to the installed version.

- [ ] **Step 4: Run the full suite (nothing should break)**

Run: `uv run pytest -q`
Expected: PASS (same count as before).

- [ ] **Step 5: Commit**

```bash
git add .gitignore pyproject.toml uv.lock
git commit -m "chore: ignore .luna/ and add langgraph-checkpoint-sqlite"
```

---

### Task 2: `luna/persistence.py` — SQLite checkpointer + session index

**Files:**
- Create: `luna/persistence.py`
- Test: `tests/test_persistence.py`

**Interfaces:**
- Consumes: `luna.config.config_dir(env) -> Path`.
- Produces:
  - `checkpointer(env=None) -> SqliteSaver` — a process-lifetime saver over `<config_dir>/sessions.db` (`sqlite3.connect(..., check_same_thread=False)`, `.setup()` called).
  - `SessionIndex(env=None)` with:
    - `record(thread_id: str, workdir: str, title: str) -> None` (INSERT OR IGNORE + set `created`/`updated` to now)
    - `touch(thread_id: str) -> None` (bump `updated`)
    - `latest_for(workdir: str) -> Row | None` where `Row` is a `namedtuple("Row", "thread_id workdir created updated title")`
    - `list(workdir: str | None = None, limit: int = 20) -> list[Row]` (newest `updated` first)
  - `make_title(text: str) -> str` — collapse whitespace, strip, truncate to 72 chars + `…`.
- The index table lives in the **same** `sessions.db` file: `luna_sessions(thread_id TEXT PRIMARY KEY, workdir TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, title TEXT NOT NULL)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_persistence.py
import time

from luna.persistence import SessionIndex, checkpointer, make_title


def test_make_title_collapses_and_truncates():
    assert make_title("  hello   world\n") == "hello world"
    assert make_title("x" * 100).endswith("…")
    assert len(make_title("x" * 100)) == 73


def test_index_record_and_latest_for(tmp_path):
    idx = SessionIndex()
    idx.record("t1", "/repo/a", "first")
    time.sleep(0.01)
    idx.record("t2", "/repo/a", "second")
    idx.record("t3", "/repo/b", "other")
    assert idx.latest_for("/repo/a").thread_id == "t2"
    assert idx.latest_for("/repo/b").thread_id == "t3"
    assert idx.latest_for("/repo/missing") is None


def test_index_touch_changes_order(tmp_path):
    idx = SessionIndex()
    idx.record("t1", "/r", "one")
    idx.record("t2", "/r", "two")
    time.sleep(0.01)
    idx.touch("t1")
    assert [r.thread_id for r in idx.list("/r")] == ["t1", "t2"]


def test_checkpointer_persists_across_instances():
    cp1 = checkpointer()
    cfg = {"configurable": {"thread_id": "persist-1"}}
    cp1.put(cfg, {"v": 1, "ts": "2026", "id": "c1", "channel_values": {}, "channel_versions": {}, "versions_seen": {}}, {}, {})
    cp2 = checkpointer()
    assert cp2.get(cfg) is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_persistence.py -q`
Expected: FAIL (`ModuleNotFoundError: luna.persistence`).

- [ ] **Step 3: Implement `luna/persistence.py`**

```python
"""Durable SQLite checkpointer and a small session index for Luna.

This is the only module besides ``agent``/``session`` that touches langgraph.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import namedtuple
from collections.abc import Mapping
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from luna.config import config_dir

Row = namedtuple("Row", "thread_id workdir created updated title")

_TITLE_MAX = 72
_WS = re.compile(r"\s+")


def _db_path(env: Mapping[str, str] | None) -> Path:
    path = config_dir(env) / "sessions.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def make_title(text: str) -> str:
    """Collapse whitespace and truncate for the session list."""
    clean = _WS.sub(" ", text).strip()
    return clean if len(clean) <= _TITLE_MAX else clean[:_TITLE_MAX] + "…"


def checkpointer(env: Mapping[str, str] | None = None) -> SqliteSaver:
    """A SqliteSaver bound to ``<config_dir>/sessions.db`` for the process."""
    conn = sqlite3.connect(_db_path(env), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


class SessionIndex:
    """The ``luna_sessions`` table living inside ``sessions.db``."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._conn = sqlite3.connect(_db_path(env), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS luna_sessions ("
            "thread_id TEXT PRIMARY KEY, workdir TEXT NOT NULL, "
            "created REAL NOT NULL, updated REAL NOT NULL, title TEXT NOT NULL)"
        )
        self._conn.commit()

    def record(self, thread_id: str, workdir: str, title: str) -> None:
        now = time.time()
        self._conn.execute(
            "INSERT OR IGNORE INTO luna_sessions VALUES (?, ?, ?, ?, ?)",
            (thread_id, str(Path(workdir).resolve()), now, now, title),
        )
        self._conn.commit()

    def touch(self, thread_id: str) -> None:
        self._conn.execute(
            "UPDATE luna_sessions SET updated = ? WHERE thread_id = ?",
            (time.time(), thread_id),
        )
        self._conn.commit()

    def latest_for(self, workdir: str) -> Row | None:
        cur = self._conn.execute(
            "SELECT thread_id, workdir, created, updated, title FROM luna_sessions "
            "WHERE workdir = ? ORDER BY updated DESC LIMIT 1",
            (str(Path(workdir).resolve()),),
        )
        row = cur.fetchone()
        return Row(*row) if row else None

    def list(self, workdir: str | None = None, limit: int = 20) -> list[Row]:
        sql = "SELECT thread_id, workdir, created, updated, title FROM luna_sessions"
        params: tuple = ()
        if workdir is not None:
            sql += " WHERE workdir = ?"
            params = (str(Path(workdir).resolve()),)
        sql += " ORDER BY updated DESC LIMIT ?"
        return [Row(*r) for r in self._conn.execute(sql, (*params, limit))]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_persistence.py -q`
Expected: PASS. If `test_checkpointer_persists_across_instances` fails on the `put` signature, replace its body with a real agent round-trip: build an agent with `checkpointer()` (see Task 3's helper once available) — for now, simplify the test to assert `checkpointer()` returns an object with `.get` and `.put` attributes and that two instances share the file.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check luna/persistence.py tests/test_persistence.py && uv run ruff format luna/persistence.py tests/test_persistence.py
git add luna/persistence.py tests/test_persistence.py
git commit -m "feat: SQLite checkpointer and session index (persistence.py)"
```

---

### Task 3: Wire persistence into `agent.py` and `cli.py`; `--continue` / `--resume`

**Files:**
- Modify: `luna/agent.py:44-77` (`build_agent` signature + `checkpointer` default)
- Modify: `luna/cli.py` (`build_parser`, `main`, new resume flow)
- Modify: `luna/session.py:120-133` (`run_once`), `166-231` (`run_repl`) — accept a starting `thread_id` and a `SessionIndex`
- Test: `tests/test_cli.py` (extend), `tests/test_persistence.py` (extend)

**Interfaces:**
- Consumes: `persistence.checkpointer`, `persistence.SessionIndex`, `persistence.make_title`.
- Produces:
  - `cli.main` accepts `-c/--continue` (bool) and `--resume` (optional str; `nargs="?"`, `const="__list__"`).
  - `session.run_repl(agent, *, console, input_fn=input, rebuild=None, index=None, thread_id=None)` — `thread_id` seeds the first thread; `index` (a `SessionIndex`) is `record`/`touch`-ed per turn.
  - `session.run_once(agent, prompt, *, thread_id=None, console, input_fn=input, index=None, workdir=".")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py  (add)
from luna.cli import build_parser


def test_parser_has_resume_flags():
    ns = build_parser().parse_args(["-c"])
    assert ns.cont is True
    ns2 = build_parser().parse_args(["--resume"])
    assert ns2.resume == "__list__"
    ns3 = build_parser().parse_args(["--resume", "abc123"])
    assert ns3.resume == "abc123"
```

```python
# tests/test_persistence.py  (add)
from langchain_core.messages import AIMessage

from luna.agent import build_agent
from luna.config import LunaConfig
from luna.persistence import SessionIndex, checkpointer
from luna.session import run_once


def test_history_survives_rebuild(tmp_path, fake_model):
    cp = checkpointer()
    idx = SessionIndex()
    cfg = LunaConfig(workdir=str(tmp_path))
    a1 = build_agent(cfg, model=fake_model(AIMessage(content="one")), checkpointer=cp)
    run_once(a1, "remember X", thread_id="keep", console=__import__("rich").console.Console(), index=idx, workdir=str(tmp_path))
    a2 = build_agent(cfg, model=fake_model(AIMessage(content="two")), checkpointer=cp)
    state = a2.get_state({"configurable": {"thread_id": "keep"}})
    assert any("remember X" in getattr(m, "content", "") for m in state.values["messages"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py::test_parser_has_resume_flags tests/test_persistence.py::test_history_survives_rebuild -q`
Expected: FAIL (`AttributeError: cont` / unexpected kwarg `index`).

- [ ] **Step 3: Extend `build_parser` and `main` in `cli.py`**

In `build_parser()` add:

```python
    parser.add_argument(
        "-c", "--continue", dest="cont", action="store_true",
        help="resume the most recent session for this directory",
    )
    parser.add_argument(
        "--resume", nargs="?", const="__list__", default=None,
        help="resume a past session (no value: pick from a list; or a thread id)",
    )
```

In `main()`, after `config` is resolved and before building the agent, add a resume-resolution block:

```python
    from luna.persistence import SessionIndex, checkpointer, make_title

    index = SessionIndex()
    cp = checkpointer()
    start_thread = uuid.uuid4().hex

    if args.cont or args.resume:
        target = _resolve_resume(args, index, config.workdir, console, interactive)
        if target is None:
            return 2
        start_thread = target
```

Pass `checkpointer=cp` to `build_agent` in `_rebuild()`. Pass `index=index` and `thread_id=start_thread` to `run_repl`; pass `index=index, thread_id=start_thread, workdir=config.workdir` to `run_once`.

Add the helper:

```python
def _resolve_resume(args, index, workdir, console, interactive):
    """Return a thread_id to resume, or None on error."""
    if args.cont:
        row = index.latest_for(workdir)
        if row is None:
            print("luna: no previous session for this directory", file=sys.stderr)
            return None
        return row.thread_id
    if args.resume != "__list__":
        return args.resume  # treat as a thread id
    rows = index.list(workdir)
    if not rows:
        print("luna: no sessions recorded for this directory", file=sys.stderr)
        return None
    for n, r in enumerate(rows, 1):
        console.print(f"  [{n}] {r.title}")
    if not interactive:
        print("luna: --resume needs a value in non-interactive mode", file=sys.stderr)
        return None
    choice = input("resume which? > ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        return rows[int(choice) - 1].thread_id
    return choice or None
```

- [ ] **Step 4: Update `build_agent` and `session.py`**

`build_agent`: change `checkpointer=checkpointer or InMemorySaver()` to keep the `or InMemorySaver()` fallback (tests without persistence still work). No signature change needed — it already accepts `checkpointer`.

`session.run_once`: add `index=None, workdir="."` params; after the turn, if `index` is not None call `index.record(thread_id, workdir, persistence.make_title(prompt))` then `index.touch(thread_id)`.

`session.run_repl`: add `index=None, thread_id=None` params. Use the passed `thread_id` instead of always generating one. On the first user turn of each thread, `index.record(thread_id, workdir, make_title(line))`; every turn `index.touch(thread_id)`. On resume, before the loop, print a recap:

```python
    if index is not None and thread_id is not None:
        _print_recap(agent, {"configurable": {"thread_id": thread_id}}, console)
```

```python
def _print_recap(agent, config, console, keep=6):
    try:
        msgs = agent.get_state(config).values.get("messages", [])
    except Exception:
        return
    if not msgs:
        return
    console.print(f"[{PALETTE['blue']}]— resuming, last {min(keep, len(msgs))} messages —[/]")
    for m in msgs[-keep:]:
        role = getattr(m, "type", "?")
        text = (getattr(m, "content", "") or "")[:200]
        if text:
            console.print(f"[dim]{role}:[/] {text}")
```

`run_repl` needs `workdir` too — thread it through from `cli.main` (add `workdir=config.workdir`).

- [ ] **Step 5: Run tests**

Run: `uv run pytest -q`
Expected: PASS (all, including the two new). Fix `run_repl`/`run_once` call sites in existing tests if signatures shifted (only additive kwargs — should be fine).

- [ ] **Step 6: Manual smoke**

Run: `uv run luna --no-splash "say hi"` then `uv run luna -c --no-splash "what did I just ask"`
Expected: the second run's model context contains the first prompt (visible if you use a real key; otherwise trust the test).

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/agent.py luna/cli.py luna/session.py tests/
git commit -m "feat: --continue / --resume and durable sessions"
```

---

### Task 4: Refactor slash commands into `luna/commands.py`

**Files:**
- Create: `luna/commands.py`
- Modify: `luna/session.py` (remove the inline `if line == ...` ladder in `run_repl`; delegate)
- Test: `tests/test_commands.py`, `tests/test_session.py` (keep passing)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `commands.CommandContext` — a dataclass: `console`, `config` (`LunaConfig`), `agent`, `rebuild` (`Callable[[], object]`), `thread_id` (`str`), `workdir` (`str`), `index` (`SessionIndex | None`), plus mutable slots filled by later tasks (`pinned`, `usage`, `session_id`). Use `dataclass` with defaults so later tasks add fields without touching call sites.
  - `commands.dispatch(line: str, ctx: CommandContext) -> DispatchResult` where `DispatchResult` is a dataclass `{handled: bool, agent: object | None, thread_id: str | None, exit: bool}`.
  - `commands.HELP: dict[str, str]` — replaces `session.SLASH_COMMANDS` (re-export from `session` for back-compat: `from luna.commands import HELP as SLASH_COMMANDS`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_commands.py
import io

from rich.console import Console

from luna.commands import CommandContext, dispatch
from luna.config import LunaConfig


def _ctx(**kw):
    base = dict(
        console=Console(file=io.StringIO(), force_terminal=True),
        config=LunaConfig(),
        agent=object(),
        rebuild=lambda: "rebuilt",
        thread_id="t",
        workdir=".",
        index=None,
    )
    base.update(kw)
    return CommandContext(**base)


def test_unknown_command_handled_but_noop():
    res = dispatch("/nope", _ctx())
    assert res.handled is True and res.exit is False


def test_exit_command():
    assert dispatch("/exit", _ctx()).exit is True


def test_non_command_not_handled():
    assert dispatch("hello world", _ctx()).handled is False


def test_reload_swaps_agent():
    res = dispatch("/reload", _ctx())
    assert res.agent == "rebuilt"


def test_new_rotates_thread():
    res = dispatch("/new", _ctx())
    assert res.thread_id and res.thread_id != "t"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_commands.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `luna/commands.py`**

Move every branch currently in `run_repl` (`/help /tools /agents /clear /new /reload /model /provider`) into named handler functions and a dispatch table. `CommandContext` is a `@dataclass`. Each handler takes `ctx` and returns a partial `DispatchResult` (or mutates `ctx` and returns nothing → treated as handled/no-op). Keep `_print_help`, `_list_tools`, `_list_agents` — move them here.

```python
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from rich.console import Console

from luna.config import LunaConfig
from luna.subagents import subagent_summaries
from luna.ui.theme import PALETTE

HELP: dict[str, str] = {
    "/help": "show this help",
    "/tools": "list the agent's tools",
    "/agents": "list available subagents",
    "/sessions": "list past sessions for this directory",
    "/resume": "resume a past session",
    "/usage": "show token usage this session",
    "/compact": "summarise and compact the conversation",
    "/diff": "show file changes made this session",
    "/undo": "revert the last file change",
    "/add": "pin files into context (/add path ...)",
    "/drop": "unpin files (/drop path ...)",
    "/context": "list pinned files",
    "/verify": "run the project's verify command now",
    "/init": "generate or update AGENTS.md",
    "/model": "show or switch the model (/model <name>)",
    "/provider": "show or switch the provider (/provider <key>)",
    "/reload": "rebuild the agent with the current config",
    "/new": "start a fresh conversation thread",
    "/clear": "clear the screen",
    "/exit": "leave Luna (also /quit, Ctrl-D)",
}


@dataclass
class CommandContext:
    console: Console
    config: LunaConfig
    agent: object
    rebuild: Callable[[], object]
    thread_id: str
    workdir: str
    index: object | None = None
    session_id: str = ""
    pinned: object | None = None      # context.PinnedFiles, Task 6
    usage: object | None = None       # usage.SessionUsage, Task 5
    permissions: object | None = None # permissions ruleset, Task 7


@dataclass
class DispatchResult:
    handled: bool = True
    agent: object | None = None
    thread_id: str | None = None
    exit: bool = False


def dispatch(line: str, ctx: CommandContext) -> DispatchResult:
    if not line.startswith("/"):
        return DispatchResult(handled=False)
    name, _, arg = line.partition(" ")
    arg = arg.strip()
    if name in ("/exit", "/quit"):
        return DispatchResult(exit=True)
    handler = _TABLE.get(name)
    if handler is None:
        ctx.console.print(f"[{PALETTE['mauve']}]unknown command {name!r}; try /help[/]")
        return DispatchResult()
    return handler(ctx, arg) or DispatchResult()


def _reload(ctx, arg):
    new = ctx.rebuild()
    ctx.console.print(f"[{PALETTE['blue']}]reloaded[/]")
    return DispatchResult(agent=new)


def _new(ctx, arg):
    tid = uuid.uuid4().hex
    ctx.console.print(f"[{PALETTE['blue']}]started a new thread[/]")
    return DispatchResult(thread_id=tid)


# ... _help, _tools, _agents, _clear, _model, _provider (Task 11), etc.

_TABLE: dict[str, Callable] = {
    "/help": _help, "/tools": _tools, "/agents": _agents, "/clear": _clear,
    "/reload": _reload, "/new": _new,
    # later tasks register: /sessions /resume /usage /compact /diff /undo
    # /add /drop /context /verify /init /model /provider
}
```

Later tasks add their handler + `_TABLE` entry. In `session.run_repl`, replace the command ladder with:

```python
        res = dispatch(line, ctx)
        if res.exit:
            return 0
        if res.handled:
            if res.agent is not None:
                agent = res.agent
            if res.thread_id is not None:
                thread_id = res.thread_id
            continue
```

Keep `session.SLASH_COMMANDS = HELP` as a re-export so `tests/test_session.py::test_slash_help_registered` and `test_agent.py` imports still pass.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_commands.py tests/test_session.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite + lint + commit**

```bash
uv run pytest -q && uv run ruff check luna tests && uv run ruff format luna tests
git add luna/commands.py luna/session.py tests/test_commands.py
git commit -m "refactor: move REPL slash commands into commands.py"
```

---

### Task 5: `luna/usage.py` — token metering, `/usage`, indicator line

**Files:**
- Create: `luna/usage.py`
- Modify: `luna/session.py` (`_stream_turn` accumulation, indicator print), `luna/commands.py` (`/usage` handler)
- Test: `tests/test_usage.py`

**Interfaces:**
- Consumes: `CommandContext.usage`.
- Produces:
  - `usage.SessionUsage` — `.add_turn(TurnUsage)`, `.turns: list[TurnUsage]`, `.totals -> tuple[int,int,int]` (in, out, total), `.last_prompt_tokens: int`.
  - `usage.TurnUsage` — `.merge(usage_metadata: dict | None)`, fields `input_tokens`, `output_tokens`, `total_tokens`.
  - `usage.context_window(provider: str, model: str | None) -> int` — static map, fallback `200_000`.
  - `usage.indicator_line(session: SessionUsage, provider: str, model: str | None) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_usage.py
from luna.usage import SessionUsage, TurnUsage, context_window, indicator_line


def test_turn_merges_metadata():
    t = TurnUsage()
    t.merge({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    t.merge({"input_tokens": 5, "output_tokens": 3, "total_tokens": 8})
    assert (t.input_tokens, t.output_tokens, t.total_tokens) == (105, 23, 128)


def test_turn_merge_none_is_safe():
    t = TurnUsage()
    t.merge(None)
    assert t.total_tokens == 0


def test_session_totals_and_last_prompt():
    s = SessionUsage()
    a = TurnUsage(); a.merge({"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
    b = TurnUsage(); b.merge({"input_tokens": 300, "output_tokens": 40, "total_tokens": 340})
    s.add_turn(a); s.add_turn(b)
    assert s.totals == (400, 50, 450)
    assert s.last_prompt_tokens == 300


def test_context_window_lookup_and_fallback():
    assert context_window("anthropic", "claude-sonnet-4-5") >= 200_000
    assert context_window("ollama", "who-knows") == 200_000


def test_indicator_line_mentions_ctx_and_session():
    s = SessionUsage()
    t = TurnUsage(); t.merge({"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200})
    s.add_turn(t)
    line = indicator_line(s, "anthropic", "claude-sonnet-4-5")
    assert "ctx" in line and "session" in line
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/test_usage.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `luna/usage.py`**

```python
"""Token accounting for a Luna session (best-effort, never raises)."""

from __future__ import annotations

from dataclasses import dataclass, field

_WINDOWS: dict[str, int] = {
    "claude-sonnet-4": 200_000, "claude-opus-4": 200_000, "claude-haiku": 200_000,
    "claude-3": 200_000, "gpt-4.1": 1_000_000, "gpt-4o": 128_000, "gpt-5": 400_000,
    "o1": 200_000, "o3": 200_000, "gemini-2.5": 1_000_000, "gemini-1.5": 1_000_000,
    "deepseek": 64_000, "qwen2.5-coder": 32_000,
}
_FALLBACK = 200_000


@dataclass
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def merge(self, meta: dict | None) -> None:
        if not meta:
            return
        self.input_tokens += int(meta.get("input_tokens", 0) or 0)
        self.output_tokens += int(meta.get("output_tokens", 0) or 0)
        self.total_tokens += int(
            meta.get("total_tokens", 0)
            or (meta.get("input_tokens", 0) or 0) + (meta.get("output_tokens", 0) or 0)
        )


@dataclass
class SessionUsage:
    turns: list[TurnUsage] = field(default_factory=list)

    def add_turn(self, turn: TurnUsage) -> None:
        if turn.total_tokens or turn.input_tokens or turn.output_tokens:
            self.turns.append(turn)

    @property
    def totals(self) -> tuple[int, int, int]:
        return (
            sum(t.input_tokens for t in self.turns),
            sum(t.output_tokens for t in self.turns),
            sum(t.total_tokens for t in self.turns),
        )

    @property
    def last_prompt_tokens(self) -> int:
        return self.turns[-1].input_tokens if self.turns else 0


def context_window(provider: str, model: str | None) -> int:
    needle = (model or provider or "").lower()
    for key, size in _WINDOWS.items():
        if key in needle:
            return size
    return _FALLBACK


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def indicator_line(session: SessionUsage, provider: str, model: str | None) -> str:
    win = context_window(provider, model)
    used = session.last_prompt_tokens
    _, _, total = session.totals
    last = session.turns[-1] if session.turns else TurnUsage()
    return (
        f"ctx ~{_k(used)}/{_k(win)} · turn {_k(last.input_tokens)} in / "
        f"{_k(last.output_tokens)} out · session {_k(total)}"
    )
```

- [ ] **Step 4: Wire into `session.py`**

In `_stream_turn`, create a `TurnUsage()` at the top. In the `mode == "messages"` branch, after handling text, call `turn_usage.merge(getattr(msg, "usage_metadata", None))`. Return it alongside the existing tuple (extend to `(text, reload_requested, turn_usage)`), update the two call sites. After `close_turn`, in `run_repl`, do `session_usage.add_turn(turn_usage)` and `console.print(f"[dim]{indicator_line(session_usage, config.provider, config.model)}[/]")`.

Add `/usage` handler to `commands.py`:

```python
def _usage(ctx, arg):
    s = ctx.usage
    if s is None or not s.turns:
        ctx.console.print("[dim]no usage recorded yet[/]")
        return
    i, o, t = s.totals
    ctx.console.print(f"turns: {len(s.turns)}  in: {i}  out: {o}  total: {t}")
```

Register `"/usage": _usage`. Create `session_usage = SessionUsage()` in `run_repl` and put it on `ctx.usage`.

- [ ] **Step 5: Run tests + full suite**

Run: `uv run pytest -q`
Expected: PASS. Adjust the `_stream_turn` call sites in `tests` if any unpack its return tuple (grep for `_stream_turn`).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/usage.py luna/session.py luna/commands.py tests/test_usage.py
git commit -m "feat: token metering, /usage, and a per-turn context indicator"
```

---

### Task 6: `luna/context.py` — `@file` mentions + `/add` / `/drop` / `/context`

**Files:**
- Create: `luna/context.py`
- Modify: `luna/session.py` (expand mentions + prepend pinned files before each turn), `luna/commands.py` (three handlers)
- Test: `tests/test_context.py`

**Interfaces:**
- Consumes: `CommandContext.pinned`, `CommandContext.workdir`.
- Produces:
  - `context.expand_mentions(text: str, workdir: str) -> str` — returns `text` plus appended `<attached: relpath>\n...\n</attached>` blocks / not-found notes / shallow dir listings. Token detection via `shlex.split` (fall back to `text.split()` on `ValueError`).
  - `context.render_pinned(pinned: PinnedFiles, workdir: str) -> str` — `""` if empty, else the same `<attached:>` framing for each pinned file, fresh from disk.
  - `context.PinnedFiles` — `.add(*paths)`, `.drop(*paths)`, `.paths -> list[str]` (sorted relpaths).
  - Module const `MAX_BYTES = 100_000`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_context.py
from luna.context import PinnedFiles, expand_mentions, render_pinned


def test_expand_existing_file(tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n")
    out = expand_mentions("look at @a.py please", str(tmp_path))
    assert "<attached: a.py>" in out and "print(1)" in out


def test_expand_quoted_path(tmp_path):
    (tmp_path / "a b.py").write_text("x = 1\n")
    out = expand_mentions('open @"a b.py"', str(tmp_path))
    assert "<attached: a b.py>" in out


def test_expand_missing_file(tmp_path):
    out = expand_mentions("@nope.py", str(tmp_path))
    assert "(@nope.py: not found)" in out


def test_expand_directory_lists(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("")
    out = expand_mentions("@pkg", str(tmp_path))
    assert "m.py" in out


def test_size_cap(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 200_000)
    out = expand_mentions("@big.txt", str(tmp_path))
    assert "truncated" in out and len(out) < 150_000


def test_pinned_roundtrip(tmp_path):
    (tmp_path / "p.py").write_text("P = 1\n")
    pins = PinnedFiles()
    pins.add("p.py")
    assert pins.paths == ["p.py"]
    assert "P = 1" in render_pinned(pins, str(tmp_path))
    pins.drop("p.py")
    assert render_pinned(pins, str(tmp_path)) == ""
```

- [ ] **Step 2: Verify failure** — `uv run pytest tests/test_context.py -q` → FAIL.

- [ ] **Step 3: Implement `luna/context.py`**

```python
"""`@file` mention expansion and session-pinned files."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

MAX_BYTES = 100_000


def _attach(rel: str, path: Path) -> str:
    data = path.read_bytes()[: MAX_BYTES + 1]
    text = data.decode("utf-8", "replace")
    if len(text) > MAX_BYTES:
        text = text[:MAX_BYTES] + "\n… (truncated)"
    return f"\n\n<attached: {rel}>\n{text}\n</attached>"


def _resolve(token: str, root: Path) -> tuple[str, Path] | None:
    rel = token.lstrip("@")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return rel, candidate


def expand_mentions(text: str, workdir: str) -> str:
    root = Path(workdir)
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    extra = ""
    for tok in tokens:
        if not tok.startswith("@") or len(tok) < 2:
            continue
        resolved = _resolve(tok, root)
        if resolved is None:
            extra += f"\n(@{tok.lstrip('@')}: not found)"
            continue
        rel, path = resolved
        if path.is_dir():
            names = sorted(p.name for p in path.iterdir())
            extra += f"\n\n<dir {rel}>\n" + "\n".join(names) + "\n</dir>"
        elif path.is_file():
            extra += _attach(rel, path)
        else:
            extra += f"\n(@{rel}: not found)"
    return text + extra


@dataclass
class PinnedFiles:
    _paths: set[str] = field(default_factory=set)

    def add(self, *paths: str) -> None:
        self._paths.update(paths)

    def drop(self, *paths: str) -> None:
        self._paths.difference_update(paths)

    @property
    def paths(self) -> list[str]:
        return sorted(self._paths)


def render_pinned(pinned: PinnedFiles, workdir: str) -> str:
    root = Path(workdir)
    out = ""
    for rel in pinned.paths:
        path = root / rel
        if path.is_file():
            out += _attach(rel, path)
    return out
```

- [ ] **Step 4: Wire into `session.py` + `commands.py`**

In `run_repl`, before building `payload`: `line = expand_mentions(line, workdir)` then `pinned_block = render_pinned(pinned, workdir)` and set `content = pinned_block + "\n\n" + line if pinned_block else line`. Create `pinned = PinnedFiles()` and put on `ctx.pinned`.

`commands.py` handlers:

```python
def _add(ctx, arg):
    if not arg:
        ctx.console.print("[dim]usage: /add path ...[/]")
        return
    ctx.pinned.add(*arg.split())
    ctx.console.print(f"[dim]pinned: {', '.join(ctx.pinned.paths)}[/]")

def _drop(ctx, arg):
    ctx.pinned.drop(*arg.split())
    ctx.console.print(f"[dim]pinned: {', '.join(ctx.pinned.paths) or '(none)'}[/]")

def _context(ctx, arg):
    ctx.console.print("\n".join(f"  {p}" for p in ctx.pinned.paths) or "[dim](no pinned files)[/]")
```

Register `/add /drop /context`.

- [ ] **Step 5: Tests + lint + commit**

```bash
uv run pytest -q && uv run ruff check luna tests && uv run ruff format luna tests
git add luna/context.py luna/session.py luna/commands.py tests/test_context.py
git commit -m "feat: @file mentions and /add pinned-file context"
```

---

### Task 7: `luna/permissions.py` + `luna/toolguard.py` — allow/deny rules

**Files:**
- Create: `luna/permissions.py`, `luna/toolguard.py`
- Modify: `luna/agent.py` (attach the middleware), `luna/session.py` (`collect_decisions` consults allow rules), `luna/ui/approve.py` (`[a] always`), `luna/commands.py` (reload rules after `always`)
- Test: `tests/test_permissions.py`

**Interfaces:**
- Consumes: `luna.config.config_dir`.
- Produces:
  - `permissions.RuleSet` — `.match(tool: str, args: dict) -> "allow" | "deny" | None`, `.allow: list[str]`, `.deny: list[str]`.
  - `permissions.load_rules(workdir: str, env=None) -> RuleSet` — merges `config.toml [permissions]` + `<workdir>/.luna/permissions.toml`.
  - `permissions.append_project_rule(workdir: str, rule: str) -> Path` — adds to `allow` in `<workdir>/.luna/permissions.toml`.
  - `permissions.suggest_rule(tool: str, args: dict, workdir: str) -> str` — for `execute`: `execute:<first-word> *`; for file tools: `<tool>:<relpath>`.
  - `toolguard.tool_guard(rules: RuleSet, workdir: str) -> middleware` — a `@wrap_tool_call` callable. deny match → returns a blocking `ToolMessage`. (Undo snapshotting is added here in Task 8.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_permissions.py
from luna.permissions import RuleSet, append_project_rule, load_rules, suggest_rule


def test_match_execute_prefix():
    rs = RuleSet(allow=["execute:git status*"], deny=["execute:git push*"])
    assert rs.match("execute", {"command": "git status -s"}) == "allow"
    assert rs.match("execute", {"command": "git push origin"}) == "deny"
    assert rs.match("execute", {"command": "ls"}) is None


def test_match_path_glob():
    rs = RuleSet(allow=[], deny=["write_file:.env", "write_file:secrets/*"])
    assert rs.match("write_file", {"file_path": ".env"}) == "deny"
    assert rs.match("write_file", {"file_path": "secrets/k.txt"}) == "deny"
    assert rs.match("write_file", {"file_path": "app.py"}) is None


def test_deny_beats_allow():
    rs = RuleSet(allow=["execute:*"], deny=["execute:rm -rf*"])
    assert rs.match("execute", {"command": "rm -rf /"}) == "deny"


def test_load_merges_sources(tmp_path, isolated_config_home):
    (isolated_config_home / "luna").mkdir(parents=True)
    (isolated_config_home / "luna" / "config.toml").write_text(
        '[permissions]\nallow = ["execute:a*"]\n'
    )
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text('deny = ["execute:b*"]\n')
    rs = load_rules(str(tmp_path))
    assert "execute:a*" in rs.allow and "execute:b*" in rs.deny


def test_append_project_rule(tmp_path):
    append_project_rule(str(tmp_path), "execute:npm test *")
    rs = load_rules(str(tmp_path))
    assert "execute:npm test *" in rs.allow


def test_suggest_rule():
    assert suggest_rule("execute", {"command": "pytest -q"}, ".") == "execute:pytest *"
    assert suggest_rule("write_file", {"file_path": "a/b.py"}, ".") == "write_file:a/b.py"
```

- [ ] **Step 2: Verify failure** — `uv run pytest tests/test_permissions.py -q` → FAIL.

- [ ] **Step 3: Implement `luna/permissions.py`**

```python
"""Allow / deny rules for tool calls. Format: '<tool>:<fnmatch pattern>'."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from luna.config import config_dir

_FILE_TOOLS = {"write_file", "edit_file", "delete", "read_file"}


def _subject(tool: str, args: dict) -> str:
    if tool == "execute":
        return str(args.get("command", ""))
    return str(args.get("file_path", args.get("path", "")))


@dataclass
class RuleSet:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    def _hit(self, rules: list[str], tool: str, subject: str) -> bool:
        for rule in rules:
            rtool, _, pattern = rule.partition(":")
            if rtool == tool and fnmatch(subject, pattern):
                return True
        return False

    def match(self, tool: str, args: dict) -> str | None:
        subject = _subject(tool, args)
        if self._hit(self.deny, tool, subject):
            return "deny"
        if self._hit(self.allow, tool, subject):
            return "allow"
        return None


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError):
        return {}


def _project_file(workdir: str) -> Path:
    return Path(workdir) / ".luna" / "permissions.toml"


def load_rules(workdir: str, env: Mapping[str, str] | None = None) -> RuleSet:
    rs = RuleSet()
    user = _read(config_dir(env) / "config.toml").get("permissions", {})
    proj = _read(_project_file(workdir))
    proj = proj.get("permissions", proj)  # allow bare or [permissions] table
    for src in (user, proj):
        rs.allow += [r for r in src.get("allow", []) if r not in rs.allow]
        rs.deny += [r for r in src.get("deny", []) if r not in rs.deny]
    return rs


def append_project_rule(workdir: str, rule: str) -> Path:
    path = _project_file(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read(path)
    table = data.get("permissions", data) if data else {}
    allow = list(table.get("allow", []))
    if rule not in allow:
        allow.append(rule)
    lines = ["[permissions]", "allow = [", *(f'    "{r}",' for r in allow), "]"]
    deny = table.get("deny", [])
    if deny:
        lines += ["deny = [", *(f'    "{r}",' for r in deny), "]"]
    path.write_text("\n".join(lines) + "\n")
    return path


def suggest_rule(tool: str, args: dict, workdir: str) -> str:
    if tool == "execute":
        first = str(args.get("command", "")).split()
        return f"execute:{first[0]} *" if first else "execute:*"
    return f"{tool}:{_subject(tool, args)}"
```

- [ ] **Step 4: Implement `luna/toolguard.py`**

```python
"""wrap_tool_call middleware: enforce deny rules (and, from Task 8, snapshot files)."""

from __future__ import annotations

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from luna.permissions import RuleSet


def tool_guard(rules: RuleSet, workdir: str):
    @wrap_tool_call
    def _guard(request, handler):
        name = getattr(request, "name", "")
        args = getattr(request, "args", {}) or {}
        if rules.match(name, args) == "deny":
            call_id = getattr(request, "tool_call_id", None) or getattr(
                getattr(request, "tool_call", None), "get", lambda *_: None
            )("id")
            return ToolMessage(
                content=f"blocked by a Luna permission rule ({name})",
                tool_call_id=call_id or "blocked",
            )
        return handler(request)

    return _guard
```

Verify the `request` attribute names against the installed source:
`uv run python -c "import inspect, langchain.agents.middleware as m; print(inspect.getsource(m.wrap_tool_call))"` — adjust `getattr` keys if needed.

- [ ] **Step 5: Attach in `agent.py`**

```python
from luna.permissions import load_rules
from luna.toolguard import tool_guard
...
    rules = load_rules(str(workdir))
    return create_deep_agent(
        ...
        middleware=[tool_guard(rules, str(workdir))],
        ...
    )
```

- [ ] **Step 6: allow auto-approve + `[a] always` in `session.py` / `approve.py`**

`session.collect_decisions` gains a `rules` + `workdir` param (default `None`). For each request, before calling `prompt_decision`: if `rules and rules.match(name, args) == "allow"`, append `{"type": "approve"}` and print a dim `⚙ {name} · auto (rule)` line; else call `prompt_decision`.

`ui/approve.prompt_decision`: add `[a] always` to the prompt string. On `a`: build `rule = suggest_rule(action, args, ".")`, show it, let the user edit (`input_fn(f"rule [{rule}] > ")` — empty keeps it), return `{"type": "approve", "always": rule}`.

`session.py` after collecting decisions: for any decision with `"always"`, call `permissions.append_project_rule(workdir, d["always"])` and refresh the in-session `rules` object (`rules = load_rules(workdir)`); also stash on `ctx.permissions`.

- [ ] **Step 7: Tests**

Add to `tests/test_permissions.py`:

```python
def test_guard_blocks_deny(tmp_path, fake_model):
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent
    from luna.config import LunaConfig

    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text('deny = ["execute:rm *"]\n')
    calls = [
        AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": "rm x"}, "id": "1"}]),
        AIMessage(content="stopped"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    out = agent.invoke({"messages": [{"role": "user", "content": "go"}]},
                       config={"configurable": {"thread_id": "g"}})
    assert any("blocked by a Luna permission rule" in getattr(m, "content", "") for m in out["messages"])
```

Run: `uv run pytest tests/test_permissions.py -q` → PASS.

- [ ] **Step 8: Full suite + lint + commit**

```bash
uv run pytest -q && uv run ruff check luna tests && uv run ruff format luna tests
git add luna/permissions.py luna/toolguard.py luna/agent.py luna/session.py luna/ui/approve.py luna/commands.py tests/test_permissions.py
git commit -m "feat: allow/deny permission rules with [a] always"
```

---

### Task 8: `luna/undo.py` + `luna/gitinfo.py` — snapshots, `/diff`, `/undo`, dirty-check

**Files:**
- Create: `luna/undo.py`, `luna/gitinfo.py`
- Modify: `luna/toolguard.py` (snapshot before mutating calls), `luna/agent.py` (pass `session_id`), `luna/cli.py` (dirty-check warning), `luna/commands.py` (`/diff`, `/undo`)
- Test: `tests/test_undo.py`, `tests/test_gitinfo.py`

**Interfaces:**
- Consumes: `CommandContext.session_id`, `CommandContext.workdir`.
- Produces:
  - `undo.journal_dir(workdir: str, session_id: str) -> Path` (`<workdir>/.luna/undo/<session_id>/`).
  - `undo.snapshot(workdir: str, session_id: str, tool: str, rel_path: str) -> None` — writes the next `NNNN.json` with `{tool, path, before, ts}` (`before=None` if the file is absent).
  - `undo.session_diff(workdir: str, session_id: str) -> str` — unified diff earliest-`before` → current, per path; `""` if nothing.
  - `undo.undo_last(workdir: str, session_id: str) -> str | None` — restore/delete; returns a description or `None` when the journal is empty.
  - `gitinfo.is_git_repo(workdir: str) -> bool`, `gitinfo.dirty_paths(workdir: str) -> list[str]`.
  - `toolguard.tool_guard(rules, workdir, session_id="")` — new optional param.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gitinfo.py
import subprocess

from luna.gitinfo import dirty_paths, is_git_repo


def test_not_a_repo(tmp_path):
    assert is_git_repo(str(tmp_path)) is False
    assert dirty_paths(str(tmp_path)) == []


def test_dirty_detection(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x")
    assert is_git_repo(str(tmp_path)) is True
    assert "a.txt" in " ".join(dirty_paths(str(tmp_path)))
```

```python
# tests/test_undo.py
from luna.undo import session_diff, snapshot, undo_last


def test_snapshot_and_undo_modify(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("old\n")
    snapshot(str(tmp_path), "s1", "edit_file", "a.py")
    f.write_text("new\n")
    assert "old" in session_diff(str(tmp_path), "s1") and "new" in session_diff(str(tmp_path), "s1")
    assert undo_last(str(tmp_path), "s1") is not None
    assert f.read_text() == "old\n"


def test_snapshot_and_undo_create(tmp_path):
    snapshot(str(tmp_path), "s1", "write_file", "new.py")
    (tmp_path / "new.py").write_text("created\n")
    undo_last(str(tmp_path), "s1")
    assert not (tmp_path / "new.py").exists()


def test_undo_empty_returns_none(tmp_path):
    assert undo_last(str(tmp_path), "empty") is None
```

- [ ] **Step 2: Verify failure** — run both new test files → FAIL.

- [ ] **Step 3: Implement `luna/gitinfo.py`**

```python
"""Read-only git status helpers (never raise)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def is_git_repo(workdir: str) -> bool:
    return (Path(workdir) / ".git").exists() or _run(workdir, "rev-parse", "--is-inside-work-tree") == "true"


def _run(workdir: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=workdir, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def dirty_paths(workdir: str) -> list[str]:
    if not is_git_repo(workdir):
        return []
    out = _run(workdir, "status", "--porcelain")
    return [line[3:] for line in out.splitlines() if line.strip()]
```

- [ ] **Step 4: Implement `luna/undo.py`**

```python
"""Per-session file snapshots so /undo and /diff can work without git."""

from __future__ import annotations

import difflib
import json
import time
from pathlib import Path


def journal_dir(workdir: str, session_id: str) -> Path:
    path = Path(workdir) / ".luna" / "undo" / (session_id or "default")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _entries(workdir: str, session_id: str) -> list[Path]:
    return sorted(journal_dir(workdir, session_id).glob("[0-9]*.json"))


def snapshot(workdir: str, session_id: str, tool: str, rel_path: str) -> None:
    d = journal_dir(workdir, session_id)
    target = Path(workdir) / rel_path
    before = target.read_text() if target.is_file() else None
    n = len(_entries(workdir, session_id))
    (d / f"{n:04d}.json").write_text(
        json.dumps({"tool": tool, "path": rel_path, "before": before, "ts": time.time()})
    )


def session_diff(workdir: str, session_id: str) -> str:
    earliest: dict[str, str | None] = {}
    for entry in _entries(workdir, session_id):
        rec = json.loads(entry.read_text())
        earliest.setdefault(rec["path"], rec["before"])
    chunks: list[str] = []
    for rel, before in earliest.items():
        current = (Path(workdir) / rel).read_text() if (Path(workdir) / rel).is_file() else ""
        diff = difflib.unified_diff(
            (before or "").splitlines(), current.splitlines(),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="",
        )
        text = "\n".join(diff)
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def undo_last(workdir: str, session_id: str) -> str | None:
    entries = _entries(workdir, session_id)
    if not entries:
        return None
    rec = json.loads(entries[-1].read_text())
    target = Path(workdir) / rec["path"]
    if rec["before"] is None:
        if target.is_file():
            target.unlink()
        note = f"removed {rec['path']}"
    else:
        target.write_text(rec["before"])
        note = f"reverted {rec['path']}"
    entries[-1].unlink()
    return note
```

- [ ] **Step 5: Extend `toolguard.py`**

Add `session_id: str = ""` param. Before the deny check returns / before `handler(request)` for `name in {"write_file", "edit_file", "delete"}`:

```python
        if session_id and name in {"write_file", "edit_file", "delete"}:
            rel = (args.get("file_path") or args.get("path") or "").lstrip("/")
            if rel:
                try:
                    snapshot(workdir, session_id, name, rel)
                except OSError:
                    pass
```

`agent.build_agent`: accept `session_id: str = ""`, pass to `tool_guard(rules, str(workdir), session_id=session_id)`. `cli.main` passes `session_id` = the REPL's `session_id` (a per-process `uuid.uuid4().hex`, distinct from `thread_id`; reused across `/reload`). Thread `session_id` into `_rebuild()` via closure.

- [ ] **Step 6: dirty-check in `cli.py` + commands**

In `cli.main`, after resolving config, before the splash:

```python
    from luna.gitinfo import dirty_paths
    dirty = dirty_paths(config.workdir)
    if dirty:
        console.print(f"[yellow]note:[/] working tree has {len(dirty)} changed file(s); Luna edits files in place")
```

`commands.py`:

```python
def _diff(ctx, arg):
    text = session_diff(ctx.workdir, ctx.session_id)
    ctx.console.print(text or "[dim]no changes this session[/]")

def _undo(ctx, arg):
    note = undo_last(ctx.workdir, ctx.session_id)
    ctx.console.print(f"[{PALETTE['blue']}]{note}[/]" if note else "[dim]nothing to undo[/]")
```

Register `/diff`, `/undo`.

- [ ] **Step 7: Tests + full suite**

Run: `uv run pytest -q` → PASS. Add one integration test in `tests/test_undo.py` that builds an agent (yolo) whose fake model emits a `write_file` call and asserts a journal entry appears under `.luna/undo/<sid>/`.

- [ ] **Step 8: Lint + commit**

```bash
uv run ruff check luna tests && uv run ruff format luna tests
git add luna/undo.py luna/gitinfo.py luna/toolguard.py luna/agent.py luna/cli.py luna/commands.py tests/test_undo.py tests/test_gitinfo.py
git commit -m "feat: session snapshots, /diff, /undo, and a git dirty-tree warning"
```

---

### Task 9: `luna/memory.py` + `remember` tool + memory tiers

**Files:**
- Create: `luna/memory.py`
- Modify: `luna/agent.py:63` (extend `memory=[...]`), `luna/extension_tools.py` (`remember` tool), `luna/prompts.py` (one line)
- Test: `tests/test_memory.py`, `tests/test_extension_tools.py` (extend)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `memory.memory_files(workdir: str) -> list[str]` — sorted existing `.luna/memory/*.md` as workdir-relative POSIX paths.
  - `memory.append_note(workdir: str, kind: str, topic: str, note: str) -> Path` — appends `\n## <YYYY-MM-DD> — <topic>\n\n<note>\n` to `.luna/memory/<kind>.md` (kind in `{"project","conventions","decisions","failures"}`; else `ValueError`).
  - `extension_tools.remember` tool + `EXTENSION_INTERRUPTS["remember"] = True`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_memory.py
import pytest

from luna.memory import append_note, memory_files


def test_append_creates_dated_section(tmp_path):
    p = append_note(str(tmp_path), "failures", "lib X", "conflicts with 3.13")
    assert p.name == "failures.md"
    body = p.read_text()
    assert "lib X" in body and "conflicts with 3.13" in body and body.lstrip().startswith("## ")


def test_append_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        append_note(str(tmp_path), "random", "t", "n")


def test_memory_files_sorted(tmp_path):
    d = tmp_path / ".luna" / "memory"
    d.mkdir(parents=True)
    (d / "project.md").write_text("x")
    (d / "failures.md").write_text("y")
    assert memory_files(str(tmp_path)) == [".luna/memory/failures.md", ".luna/memory/project.md"]
```

```python
# tests/test_extension_tools.py  (add)
from luna.extension_tools import EXTENSION_INTERRUPTS, remember


def test_remember_is_interrupted():
    assert EXTENSION_INTERRUPTS.get("remember") is True


def test_remember_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = remember.invoke({"kind": "decisions", "topic": "db", "note": "chose sqlite"})
    assert "decisions.md" in out
    assert "chose sqlite" in (tmp_path / ".luna" / "memory" / "decisions.md").read_text()
```

- [ ] **Step 2: Verify failure** — run new tests → FAIL.

- [ ] **Step 3: Implement `luna/memory.py`**

```python
"""`.luna/memory/*.md` tiers loaded into the system prompt."""

from __future__ import annotations

from datetime import date
from pathlib import Path

KINDS = ("project", "conventions", "decisions", "failures")


def _memory_dir(workdir: str) -> Path:
    return Path(workdir) / ".luna" / "memory"


def memory_files(workdir: str) -> list[str]:
    d = _memory_dir(workdir)
    if not d.is_dir():
        return []
    root = Path(workdir)
    return sorted(str(p.relative_to(root).as_posix()) for p in d.glob("*.md"))


def append_note(workdir: str, kind: str, topic: str, note: str) -> Path:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    d = _memory_dir(workdir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{kind}.md"
    section = f"\n## {date.today().isoformat()} — {topic}\n\n{note}\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(section)
    return path
```

- [ ] **Step 4: `remember` tool in `extension_tools.py`**

```python
from typing import Literal
from luna.memory import append_note

@tool
def remember(
    kind: Literal["project", "conventions", "decisions", "failures"],
    topic: str,
    note: str,
) -> str:
    """Record a durable note in .luna/memory/<kind>.md.

    Use 'failures' for dead ends ("tried X, it conflicts with Y, don't retry"),
    'decisions' for load-bearing choices, 'conventions' for how this repo works,
    'project' for what it is. Run /reload afterwards to load it into context.
    """
    path = append_note(".", kind, topic, note)
    return f"noted in {path.as_posix()}. Run /reload to load it into context."

EXTENSION_TOOLS = [manage_mcp, manage_skills, remember]
EXTENSION_INTERRUPTS = {"manage_mcp": True, "manage_skills": True, "remember": True}
```

- [ ] **Step 5: Extend `memory=[...]` in `agent.py`**

```python
    from luna.memory import memory_files
    mem = (["AGENTS.md"] if (workdir / "AGENTS.md").is_file() else []) + memory_files(str(workdir))
    memory = mem or None
```

- [ ] **Step 6: `prompts.py` line**

Add under "Rules:":

```
- When you hit a dead end or make a load-bearing decision, record it with the
  `remember` tool (use `failures` especially — future runs read it back).
```

- [ ] **Step 7: Tests + lint + commit**

```bash
uv run pytest -q && uv run ruff check luna tests && uv run ruff format luna tests
git add luna/memory.py luna/agent.py luna/extension_tools.py luna/prompts.py tests/test_memory.py tests/test_extension_tools.py
git commit -m "feat: .luna/memory tiers and the remember tool"
```

---

### Task 10: Post-edit verification loop + `/verify`

**Files:**
- Modify: `luna/config.py` (`agent.verify_command` settable + `LunaConfig.verify_command`), `luna/session.py` (loop after mutating turns), `luna/commands.py` (`/verify`)
- Create: `luna/verify.py`
- Test: `tests/test_verify.py`, `tests/test_config.py` (extend)

**Interfaces:**
- Consumes: `LunaConfig.verify_command`, `CommandContext`.
- Produces:
  - `verify.run_verify(command: str, workdir: str) -> tuple[bool, str]` — `(ok, tail)`; tail is the last ~40 lines of combined stdout+stderr; `("", ...)` → `(True, "")` (disabled).
  - `verify.VERIFY_TAIL_LINES = 40`, timeout 300s.
  - `session` exposes `_run_verification(agent, thread_id, config, console, cfg, input_fn)` — runs the command, and on failure feeds exactly one fix-up turn then re-checks.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_verify.py
from luna.verify import run_verify


def test_disabled_command_is_ok(tmp_path):
    assert run_verify("", str(tmp_path)) == (True, "")


def test_passing_command(tmp_path):
    ok, tail = run_verify("python -c \"print('hi')\"", str(tmp_path))
    assert ok is True


def test_failing_command_tail(tmp_path):
    ok, tail = run_verify("python -c \"import sys; print('boom'); sys.exit(1)\"", str(tmp_path))
    assert ok is False and "boom" in tail
```

```python
# tests/test_config.py  (add)
def test_verify_command_settable(tmp_path, isolated_config_home):
    from luna.config import load_config, set_config_values
    set_config_values({"agent.verify_command": "pytest -q"})
    assert load_config({}).verify_command == "pytest -q"
```

- [ ] **Step 2: Verify failure** — run → FAIL.

- [ ] **Step 3: Implement `luna/verify.py`**

```python
"""Run the project's verify command and capture a short tail."""

from __future__ import annotations

import subprocess

VERIFY_TAIL_LINES = 40
_TIMEOUT = 300


def run_verify(command: str, workdir: str) -> tuple[bool, str]:
    if not command.strip():
        return True, ""
    try:
        proc = subprocess.run(
            command, shell=True, cwd=workdir, capture_output=True, text=True, timeout=_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return False, f"verify command timed out after {_TIMEOUT}s"
    except OSError as exc:
        return False, f"could not run verify command: {exc}"
    combined = (proc.stdout + proc.stderr).splitlines()
    tail = "\n".join(combined[-VERIFY_TAIL_LINES:])
    return proc.returncode == 0, tail
```

- [ ] **Step 4: `config.py`**

Add `"agent.verify_command": "str"` to `_SETTABLE`; add `verify_command: str = ""` to `LunaConfig`; in `_apply_toml`, read `agent["verify_command"]`; in `load_config`, `verify_command=str(merged.get("verify_command", ""))`.

- [ ] **Step 5: Loop in `session.py`**

After `_stream_turn` returns in `run_repl` (and `run_once`), if the turn used a mutating tool (track via the `seen_tools` names — expose them from `_stream_turn`), call:

```python
def _run_verification(agent, thread_id, config, console, cfg, input_fn):
    if not cfg.verify_command:
        return
    ok, tail = run_verify(cfg.verify_command, cfg.workdir)
    if ok:
        console.print("[dim]✓ verify ok[/]")
        return
    console.print(f"[yellow]verify failed[/]\n{tail}")
    payload = {"messages": [{"role": "user", "content":
        f"The verify command `{cfg.verify_command}` failed. Output:\n{tail}\nFix it."}]}
    _stream_turn(agent, payload, config, console, input_fn)
    ok, tail = run_verify(cfg.verify_command, cfg.workdir)
    console.print("[dim]✓ verify ok[/]" if ok else f"[yellow]⚠ verify still failing after 1 retry[/]\n{tail}")
```

`_MUTATING = {"write_file", "edit_file", "delete", "execute"}`.

- [ ] **Step 6: `/verify` command**

```python
def _verify(ctx, arg):
    ok, tail = run_verify(ctx.config.verify_command, ctx.workdir)
    if not ctx.config.verify_command:
        ctx.console.print("[dim]set agent.verify_command in config first[/]")
        return
    ctx.console.print(("[dim]✓ verify ok[/]" if ok else f"[yellow]verify failed[/]\n{tail}"))
```

Register `/verify`.

- [ ] **Step 7: Tests + full suite + lint + commit**

```bash
uv run pytest -q && uv run ruff check luna tests && uv run ruff format luna tests
git add luna/verify.py luna/config.py luna/session.py luna/commands.py tests/test_verify.py tests/test_config.py
git commit -m "feat: post-edit verification loop with one auto-retry and /verify"
```

---

### Task 11: `/model` + `/provider` hot-swap + fast model for subagents

**Files:**
- Modify: `luna/config.py` (`model.fast` settable + `LunaConfig.fast_model`), `luna/subagents.py` (`load_subagents(..., fast_model=None)`, apply to built-ins), `luna/agent.py` (pass `fast_model`), `luna/commands.py` (`/model`, `/provider` handlers)
- Test: `tests/test_subagents.py` (extend), `tests/test_commands.py` (extend), `tests/test_config.py` (extend)

**Interfaces:**
- Consumes: `CommandContext.config`, `CommandContext.rebuild`.
- Produces:
  - `LunaConfig.fast_model: str | None`.
  - `subagents.load_subagents(workdir=".", *, env=None, fast_model=None)` — built-in `researcher`/`reviewer` get `model=fast_model` when set and they have no explicit model.
  - `commands._model(ctx, arg)` / `commands._provider(ctx, arg)` — no arg: print current; with arg: mutate `ctx.config`, rebuild, return `DispatchResult(agent=...)`. `/provider` with a missing key prints a `luna config set-key` hint and does not swap.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_subagents.py  (add)
from luna.subagents import load_subagents


def test_fast_model_applied_to_builtins(tmp_path):
    subs = load_subagents(str(tmp_path), fast_model="anthropic:claude-haiku-4-5")
    by_name = {s["name"]: s for s in subs}
    assert by_name["researcher"].get("model") == "anthropic:claude-haiku-4-5"
```

```python
# tests/test_commands.py  (add)
def test_model_swap_rebuilds(monkeypatch):
    ctx = _ctx(config=LunaConfig(model="claude-sonnet-4-5"))
    res = dispatch("/model claude-opus-4", ctx)
    assert ctx.config.model == "claude-opus-4"
    assert res.agent == "rebuilt"


def test_provider_without_key_does_not_swap(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ctx = _ctx(config=LunaConfig(provider="deepseek"))
    res = dispatch("/provider anthropic", ctx)
    assert ctx.config.provider == "deepseek"  # unchanged
```

```python
# tests/test_config.py  (add)
def test_fast_model_settable(isolated_config_home):
    from luna.config import load_config, set_config_values
    set_config_values({"model.fast": "anthropic:claude-haiku-4-5"})
    assert load_config({}).fast_model == "anthropic:claude-haiku-4-5"
```

- [ ] **Step 2: Verify failure** — run → FAIL.

- [ ] **Step 3: `config.py`**

`_SETTABLE["model.fast"] = "str"`; `LunaConfig.fast_model: str | None = None`; `_apply_toml`: `if "fast" in model: into["fast_model"] = model["fast"]`; `load_config`: `fast_model=merged.get("fast_model")`.

- [ ] **Step 4: `subagents.py`**

```python
def load_subagents(workdir=".", *, env=None, fast_model=None):
    agents = []
    for a in BUILTIN_SUBAGENTS:
        if fast_model and "model" not in a:
            a = _subagent(a["name"], a["description"], a["system_prompt"],
                          _READ_ONLY, model=fast_model)
        agents.append(a)
    ...
```

Adjust `_subagent` reads — `SubAgent` is a dict-like; use `a["system_prompt"]`. Verify with `uv run python -c "from deepagents import SubAgent; print(SubAgent(name='x', description='d', system_prompt='p').keys())"`.

- [ ] **Step 5: `agent.py`** — `subs = subagents_mod.load_subagents(config.workdir, fast_model=config.fast_model)` inside `_extension_bits` (thread `config.fast_model` in).

- [ ] **Step 6: `commands.py` handlers**

```python
def _model(ctx, arg):
    if not arg:
        ctx.console.print(f"model: {ctx.config.model or '(provider default)'}")
        return
    ctx.config.model = arg
    ctx.console.print(f"[{PALETTE['blue']}]model → {arg}[/]")
    return DispatchResult(agent=ctx.rebuild())

def _provider(ctx, arg):
    if not arg:
        ctx.console.print(f"provider: {ctx.config.provider}")
        return
    from luna.providers import PROVIDERS
    from luna.credentials import get_api_key
    import os
    if arg not in PROVIDERS:
        ctx.console.print(f"[{PALETTE['mauve']}]unknown provider {arg!r}[/]")
        return
    spec = PROVIDERS[arg]
    if spec.env_var and not (os.environ.get(spec.env_var) or get_api_key(arg)):
        ctx.console.print(f"[{PALETTE['mauve']}]no key for {arg}; run: luna config set-key {arg}[/]")
        return
    ctx.config.provider = arg
    ctx.config.model = None
    ctx.console.print(f"[{PALETTE['blue']}]provider → {arg}[/]")
    return DispatchResult(agent=ctx.rebuild())
```

Replace the old `/model` `/provider` branches; register in `_TABLE`.

- [ ] **Step 7: Tests + full suite + lint + commit**

```bash
uv run pytest -q && uv run ruff check luna tests && uv run ruff format luna tests
git add luna/config.py luna/subagents.py luna/agent.py luna/commands.py tests/
git commit -m "feat: /model and /provider hot-swap plus a fast model for subagents"
```

---

### Task 12: `luna/initgen.py` + `luna init` + `/init`

**Files:**
- Create: `luna/initgen.py`
- Modify: `luna/cli.py` (`_SUBCOMMANDS` + handler), `luna/commands.py` (`/init`)
- Test: `tests/test_initgen.py`

**Interfaces:**
- Consumes: `luna.session.run_once`, `luna.agent.build_agent`.
- Produces:
  - `initgen.init_prompt(workdir: str) -> str` — the exploration+write instruction; wording differs for create vs update based on `AGENTS.md` presence.
  - `initgen.existing_action(workdir: str) -> str` — `"create"` | `"update"`.
  - `cli`: `luna init` runs a one-shot agent turn with `init_prompt(cwd)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_initgen.py
from luna.initgen import existing_action, init_prompt


def test_existing_action(tmp_path):
    assert existing_action(str(tmp_path)) == "create"
    (tmp_path / "AGENTS.md").write_text("# x\n")
    assert existing_action(str(tmp_path)) == "update"


def test_prompt_mentions_agents_md(tmp_path):
    p = init_prompt(str(tmp_path))
    assert "AGENTS.md" in p and ("build" in p.lower() or "test" in p.lower())


def test_init_subcommand_runs_agent(tmp_path, fake_model, monkeypatch, capsys):
    from langchain_core.messages import AIMessage
    from luna import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(
        cli, "build_agent",
        lambda *a, **k: __import__("luna.agent", fromlist=["build_agent"]).build_agent(
            *a, model=fake_model(AIMessage(content="wrote AGENTS.md")), **{kk: vv for kk, vv in k.items() if kk != "model"}
        ),
    )
    rc = cli.main(["init"])
    assert rc == 0
```

- [ ] **Step 2: Verify failure** — FAIL.

- [ ] **Step 3: Implement `luna/initgen.py`**

```python
"""Prompt + guard for `luna init` (generate AGENTS.md)."""

from __future__ import annotations

from pathlib import Path

_BASE = (
    "Explore this repository and {verb} an AGENTS.md at its root. Cover: what the "
    "project does in two sentences; the exact build, test, and lint commands; the "
    "directory/module layout; and the coding conventions a contributor must follow. "
    "Keep it under ~60 lines. Use your read tools first, then write the file."
)


def existing_action(workdir: str) -> str:
    return "update" if (Path(workdir) / "AGENTS.md").is_file() else "create"


def init_prompt(workdir: str) -> str:
    if existing_action(workdir) == "update":
        return _BASE.format(verb="revise the existing") + (
            " Preserve anything still accurate; do not blindly overwrite."
        )
    return _BASE.format(verb="create")
```

- [ ] **Step 4: `cli.py`**

Add `"init"` to `_SUBCOMMANDS`; handler:

```python
def _run_init(argv: list[str]) -> int:
    console = get_console()
    config = load_config({})
    from luna.initgen import init_prompt
    try:
        agent = build_agent(config, on_warn=lambda m: console.print(f"[yellow]{m}[/]"))
    except LunaConfigError as exc:
        print(f"luna: {exc}", file=sys.stderr)
        return 2
    run_once(agent, init_prompt(config.workdir), thread_id=uuid.uuid4().hex, console=console)
    return 0
```

Wire into the `handlers` dict in `main`.

- [ ] **Step 5: `/init` command**

```python
def _init(ctx, arg):
    from luna.initgen import init_prompt
    from luna.session import run_once
    run_once(ctx.agent, init_prompt(ctx.workdir), thread_id=ctx.thread_id, console=ctx.console)
```

Register `/init`.

- [ ] **Step 6: Tests + full suite + lint + commit**

```bash
uv run pytest -q && uv run ruff check luna tests && uv run ruff format luna tests
git add luna/initgen.py luna/cli.py luna/commands.py tests/test_initgen.py
git commit -m "feat: luna init / /init to generate AGENTS.md"
```

---

### Task 13: Docs, CHANGELOG, README, AGENTS.md

**Files:**
- Modify: `README.md` (new commands + config keys), `CHANGELOG.md` (0.2.0 entry), `AGENTS.md` (new modules), `.env.example` (no change unless needed)
- Test: `tests/test_metadata.py` (keep passing; extend if it checks command lists)

**Interfaces:** none.

- [ ] **Step 1: Update `CHANGELOG.md`**

Add a `## [0.2.0]` section (Keep a Changelog format) listing: durable sessions + `--continue`/`--resume`, `/usage` + context indicator, `/compact`, `@file` + `/add`, permission rules, `.luna/memory` + `remember`, verify loop, `luna init`, `/model` + `/provider` swap, fast model.

- [ ] **Step 2: Update `README.md`**

New subsection "Сессии и контекст" (persistence, `--continue`, `/resume`, `/usage`, `/compact`), extend the REPL command list, add the new `[permissions]` / `[model] fast` / `[agent] verify_command` config keys to the Конфигурация block, document `@file` and `/add`, document `luna init`.

- [ ] **Step 3: Update `AGENTS.md`**

Add the new modules to the Структура list: `persistence.py`, `usage.py`, `context.py`, `permissions.py`, `toolguard.py`, `undo.py`, `gitinfo.py`, `memory.py`, `verify.py`, `initgen.py`, `commands.py`. Note the new "framework imports also allowed in `persistence.py` / `toolguard.py`" rule.

- [ ] **Step 4: Bump version**

`luna/__init__.py`: `__version__ = "0.2.0"`. `pyproject.toml`: `version = "0.2.0"`.

- [ ] **Step 5: Full suite + lint + commit**

```bash
uv run pytest -q && uv run ruff check luna tests && uv run ruff format --check .
git add README.md CHANGELOG.md AGENTS.md luna/__init__.py pyproject.toml
git commit -m "docs: 0.2.0 — competitive feature set"
```

---

## Self-Review

**Spec coverage:**

| Spec §4 feature | Task |
| --- | --- |
| 4.1 Session persistence | Task 2, 3 |
| 4.2 Context indicator + `/compact` | Task 5 (indicator/`/usage`); `/compact` → see gap below |
| 4.3 `/diff` + dirty-check + `/undo` | Task 8 |
| 4.4 `@file` + `/add` | Task 6 |
| 4.5 Permission rules | Task 7 |
| 4.6 `.luna/memory` + `remember` | Task 9 |
| 4.7 Verification loop | Task 10 |
| 4.8 `luna init` | Task 12 |
| 4.9 `/model` + `/provider` + fast model | Task 11 |
| §5 command refactor | Task 4 |
| §6 config keys | Tasks 10, 11 |
| §7 CLI flags | Tasks 3, 12 |

**Gap found — `/compact` (spec §4.2):** not covered by a task above. Add:

### Task 5b: `/compact` — summarise-and-reseed

**Files:** Modify `luna/commands.py`, `luna/session.py`; Test `tests/test_commands.py`.

**Interfaces:** Produces `commands._compact(ctx, arg)` — runs one hidden `run_once` on the current thread asking for a dense handoff summary, allocates a new `thread_id` via `uuid.uuid4().hex`, calls `ctx.index.record(new_id, ctx.workdir, "compacted: " + old_title)` (or `touch`), and returns `DispatchResult(thread_id=new_id)` after seeding the new thread by invoking the agent once with `{"messages": [{"role": "user", "content": "Continuing a compacted session. Handoff note:\n" + summary}]}`.

- [ ] **Step 1: Failing test**

```python
def test_compact_rotates_thread_with_summary(tmp_path, fake_model):
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent
    ctx = _ctx(
        agent=build_agent(LunaConfig(workdir=str(tmp_path)),
                          model=fake_model(AIMessage(content="SUMMARY: did X"))),
        workdir=str(tmp_path), thread_id="old",
    )
    res = dispatch("/compact", ctx)
    assert res.thread_id and res.thread_id != "old"
    state = ctx.agent.get_state({"configurable": {"thread_id": res.thread_id}})
    assert any("did X" in getattr(m, "content", "") for m in state.values["messages"])
```

- [ ] **Step 2–5:** verify fail → implement `_compact` (register `/compact`) → verify pass → `uv run pytest -q` → commit:

```bash
git add luna/commands.py luna/session.py tests/test_commands.py
git commit -m "feat: /compact — summarise and reseed the conversation"
```

Renumber: this slots in right after Task 5.

**Placeholder scan:** no `TBD`/`TODO`/"handle edge cases"/"similar to Task N" — code blocks are concrete. `toolguard.py` Step 4 has a documented verification step for the `request` shape (acceptable — it is a real command to run, not a placeholder).

**Type consistency:**
- `checkpointer()` / `SessionIndex` / `make_title` — same names in Tasks 2, 3, 5b. ✓
- `CommandContext` fields added additively (`usage`, `pinned`, `session_id`, `permissions`) — Tasks 4, 5, 6, 7, 8. ✓
- `tool_guard(rules, workdir, session_id="")` — Task 7 defines two params, Task 8 adds the third with a default. ✓
- `run_verify -> (bool, str)` — consistent Task 10. ✓
- `load_subagents(..., fast_model=None)` — Task 11 only. ✓
- `DispatchResult` fields (`handled`, `agent`, `thread_id`, `exit`) — consistent Tasks 4, 11, 5b. ✓

---

## Execution notes

- Tasks are ordered so each builds only on earlier ones. Task 4 (command refactor) must land before Tasks 5–12 register handlers.
- If `SqliteSaver` cannot serialise deepagents state (Task 3 Step 6 smoke), stop and raise it — the fallback is a custom serde and it changes Task 2.
- Keep `InMemorySaver` as the `build_agent` default so unit tests stay isolated from disk.
