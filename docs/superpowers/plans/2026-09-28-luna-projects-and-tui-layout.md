# Luna Projects, Trust Gate and TUI Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist projects (exact launch dirs) with a Claude-Code-style trust gate, group the session list by day, and redesign the TUI as "sessions sidebar + Claude-Code-style chat" with tool calls inline.

**Architecture:** A new `luna_projects` table (via `luna/core/projects.py: ProjectIndex`) sits next to `luna_sessions` in `sessions.db`. The CLI asks for trust before launching the TUI; the long-lived local server re-checks trust per request through an injectable `trust_check`. The server computes day groups and tool-call history so every client (TUI now, desktop later) renders the same data. The TUI drops `ActivitySidebar`; tool calls become `ToolRow` widgets in the transcript.

**Tech Stack:** Python 3.12, Textual 8.2.8, Starlette + sse-starlette, sqlite3, LangGraph `SqliteSaver`, questionary (via `luna.ui.interact.arrow_pick`), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-28-luna-projects-and-tui-layout-design.md`

## Global Constraints

- Project key = `str(Path(workdir).resolve())`, the exact launch directory. No git-root detection.
- Trust prompt runs only on the TUI launch path (`interactive and not prompt` in `luna/cli.py`). `-p` and piped-stdin REPL never prompt and never write a trust record.
- Refusing trust (choice "Нет", Esc, Ctrl+C, `input()` answer other than `y`/`yes`/`д`/`да`) → prints `Luna не запущена: папка не отмечена как доверенная.`, exit code `1`, nothing written to `luna_projects`.
- `create_app(..., trust_check=None)` disables enforcement; `run_serve` always passes a real check. Errors: `400 {"error": "workdir_required"}`, `403 {"error": "workdir_not_trusted", "workdir": <value>}`.
- Session groups (API values): `"today" | "yesterday" | "week" | "older"`, by local calendar day. Client labels: `Сегодня`, `Вчера`, `На этой неделе`, `Ранее`.
- Month abbreviations: `янв фев мар апр май июн июл авг сен окт ноя дек`. Weekdays: `пн вт ср чт пт сб вс`.
- `SessionIndex.list` default limit: `200`.
- New colour tokens: `ok = #8fd6a8`, `err = #e88b8b`.
- Sidebar width: `30` columns. No `ActivitySidebar` anywhere after Task 7.
- All user-facing strings are Russian; code, identifiers and comments are English, PEP 8, matching existing module docstring style.
- Run tests with `uv run pytest`. Lint with `uv run ruff check luna tests`.

## Review Focus

1. **Trust granted after the server started.** The server outlives CLI runs; a folder trusted by a later `luna` launch must be accepted immediately → `make_trust_check` opens a fresh `ProjectIndex` per call (test in Task 3).
2. **`updated` in the future (clock skew, another machine's DB).** Must render as `today` / `сейчас`, never a negative "-3м" or a crash (test in Task 4).
3. **Turn fails while a tool is running.** The pending `ToolRow` must stop pulsing and show a failed `●` instead of twinkling forever (test in Task 7).
4. **AI message content as a list of blocks** (Anthropic returns `[{"type": "text", ...}, {"type": "tool_use", ...}]` when it calls tools). History must show only the text, never raw JSON/list reprs (test in Task 5).
5. **Ctrl+C / Esc at the trust prompt.** Must behave exactly like "Нет": exit 1, nothing written, no traceback (test in Task 2).

---

## File Structure

| File | Responsibility |
|---|---|
| `luna/core/projects.py` (new) | `ProjectIndex`: `luna_projects` table, one-time migration, trust queries |
| `luna/ui/trust.py` (new) | Trust prompt (`confirm_trust`) and gate (`ensure_trusted`) for the CLI |
| `luna/server/trust.py` (new) | `trust_error(request, workdir)` shared by every server handler |
| `luna/cli.py` | Call the gate before the TUI |
| `luna/server/app.py`, `run.py`, `sessions.py`, `turns.py`, `approvals.py` | Wire `trust_check`; groups + relative time; history with tools |
| `luna/core/persistence.py` | `list()` limit 200 |
| `luna/core/turn_events.py` | Extract `tool_outcome`, add `args_preview` |
| `luna/tui/tool_row.py` (new) | `ToolRow` widget: pulsing → `●` + `└ detail · 1.2s` |
| `luna/tui/sidebar_sessions.py` | Project header, "+ новая сессия", day groups, current-row highlight |
| `luna/tui/chat.py` | Claude-Code-style transcript, inline tool rows, `TurnFinished` message |
| `luna/tui/app.py`, `theme.py`, `luna.tcss` | Two-column layout, Ctrl+N, new tokens and styles |
| `luna/tui/sidebar_activity.py` | Deleted |

---

### Task 1: `ProjectIndex` and the one-time migration

**Files:**
- Create: `luna/core/projects.py`
- Test: `tests/test_projects.py`

**Interfaces:**
- Consumes: `luna.core.persistence._db_path(env) -> Path`, `luna.core.persistence._INDEX_ERRORS`, `SessionIndex.record(thread_id, workdir, title)` (tests only).
- Produces:
  - `ProjectRow = namedtuple("ProjectRow", "path trusted_at last_opened")`
  - `class ProjectIndex(env: Mapping[str, str] | None = None)` with `.ok -> bool`, `.is_trusted(path: str) -> bool`, `.trust(path: str) -> None`, `.touch(path: str) -> None`, `.list() -> list[ProjectRow]`

- [ ] **Step 1: Write the failing tests**

`tests/test_projects.py` (the autouse `isolated_config_home` fixture in `tests/conftest.py` already points `XDG_CONFIG_HOME` at a temp dir):

```python
import time

from luna.core.persistence import SessionIndex
from luna.core.projects import ProjectIndex


def test_folder_is_untrusted_by_default(tmp_path):
    assert ProjectIndex().is_trusted(str(tmp_path)) is False


def test_trust_is_keyed_by_resolved_path(tmp_path):
    ProjectIndex().trust(str(tmp_path / "."))
    assert ProjectIndex().is_trusted(str(tmp_path)) is True


def test_list_is_newest_opened_first(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    idx = ProjectIndex()
    idx.trust(str(a))
    time.sleep(0.01)
    idx.trust(str(b))
    time.sleep(0.01)
    idx.touch(str(a))
    assert [row.path for row in idx.list()] == [str(a.resolve()), str(b.resolve())]


def test_migration_trusts_folders_that_already_have_sessions(tmp_path):
    SessionIndex().record("t1", str(tmp_path), "hi")
    assert ProjectIndex().is_trusted(str(tmp_path)) is True


def test_migration_skips_folders_that_no_longer_exist(tmp_path):
    gone = tmp_path / "gone"
    SessionIndex().record("t1", str(gone), "hi")
    assert ProjectIndex().is_trusted(str(gone)) is False


def test_migration_runs_only_when_the_table_is_first_created(tmp_path):
    ProjectIndex()  # creates luna_projects
    new = tmp_path / "new"
    new.mkdir()
    SessionIndex().record("t2", str(new), "scripted -p run")
    assert ProjectIndex().is_trusted(str(new)) is False


def test_degrades_to_a_no_op_when_the_db_cannot_open(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker / "cfg"))
    idx = ProjectIndex()
    assert idx.ok is False
    assert idx.is_trusted(str(tmp_path)) is False
    idx.trust(str(tmp_path))
    idx.touch(str(tmp_path))
    assert idx.list() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_projects.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'luna.core.projects'`

- [ ] **Step 3: Implement `luna/core/projects.py`**

```python
"""Persisted projects: exact launch directories and whether they are trusted.

Lives in the same ``sessions.db`` as :class:`luna.core.persistence.SessionIndex`
so a future desktop client can list projects and their sessions from one
store. Best-effort like the session index: an unavailable database degrades
to a no-op index instead of breaking the CLI.
"""

from __future__ import annotations

import sqlite3
import time
from collections import namedtuple
from collections.abc import Mapping
from pathlib import Path

from luna.core.persistence import _INDEX_ERRORS, _db_path

ProjectRow = namedtuple("ProjectRow", "path trusted_at last_opened")


def _key(path: str) -> str:
    return str(Path(path).resolve())


def _migrate(conn: sqlite3.Connection) -> None:
    """Trust every still-existing folder that already has sessions.

    Runs only when ``luna_projects`` is first created, so existing users are
    never re-prompted for folders they already worked in, while folders that
    gain sessions later (e.g. via a scripted ``-p`` run) are not silently
    trusted.
    """
    try:
        rows = conn.execute(
            "SELECT workdir, MIN(created) FROM luna_sessions GROUP BY workdir"
        ).fetchall()
    except sqlite3.OperationalError:
        return  # no luna_sessions table yet: nothing to migrate
    for workdir, created in rows:
        if Path(workdir).is_dir():
            conn.execute(
                "INSERT OR IGNORE INTO luna_projects VALUES (?, ?, ?)",
                (workdir, created, created),
            )


class ProjectIndex:
    """The ``luna_projects`` table inside ``sessions.db`` (best-effort)."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Open ``sessions.db``, create the table, migrate once; degrade on failure."""
        self._conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(_db_path(env), check_same_thread=False)
            existed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'luna_projects'"
            ).fetchone()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS luna_projects ("
                "path TEXT PRIMARY KEY, trusted_at REAL, last_opened REAL NOT NULL)"
            )
            if not existed:
                _migrate(conn)
            conn.commit()
            self._conn = conn
        except _INDEX_ERRORS:
            self._conn = None

    @property
    def ok(self) -> bool:
        """True when the backing table is available."""
        return self._conn is not None

    def is_trusted(self, path: str) -> bool:
        """Whether ``path`` has been trusted; ``False`` if unavailable."""
        if self._conn is None:
            return False
        try:
            row = self._conn.execute(
                "SELECT trusted_at FROM luna_projects WHERE path = ?", (_key(path),)
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None and row[0] is not None

    def trust(self, path: str) -> None:
        """Mark ``path`` trusted and opened now (no-op if unavailable)."""
        if self._conn is None:
            return
        now = time.time()
        try:
            self._conn.execute(
                "INSERT INTO luna_projects VALUES (?, ?, ?) ON CONFLICT(path) DO UPDATE "
                "SET trusted_at = excluded.trusted_at, last_opened = excluded.last_opened",
                (_key(path), now, now),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass

    def touch(self, path: str) -> None:
        """Bump ``last_opened`` (no-op if unavailable)."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "UPDATE luna_projects SET last_opened = ? WHERE path = ?",
                (time.time(), _key(path)),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass

    def list(self) -> list[ProjectRow]:
        """Projects, most recently opened first; ``[]`` if unavailable."""
        if self._conn is None:
            return []
        try:
            return [
                ProjectRow(*r)
                for r in self._conn.execute(
                    "SELECT path, trusted_at, last_opened FROM luna_projects "
                    "ORDER BY last_opened DESC"
                )
            ]
        except sqlite3.Error:
            return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_projects.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add luna/core/projects.py tests/test_projects.py
git commit -m "feat: ProjectIndex — persisted projects with trust, one-time migration from existing sessions"
```

---

### Task 2: Trust prompt before the TUI

**Files:**
- Create: `luna/ui/trust.py`
- Modify: `luna/cli.py` (imports at top; `main()` right after `interactive = ...`, currently line ~383)
- Modify: `tests/test_cli_tui_entry.py` (first test and `_force_interactive` helper)
- Test: `tests/test_trust.py`

**Interfaces:**
- Consumes: `ProjectIndex` (Task 1); `luna.ui.interact.arrow_pick(console, input_fn, options) -> str | None` (returns `None` when not a real terminal; raises `KeyboardInterrupt` on Esc/Ctrl+C).
- Produces:
  - `confirm_trust(console, workdir: str, input_fn=input) -> bool`
  - `ensure_trusted(console, workdir: str, *, index: ProjectIndex | None = None, input_fn=input) -> bool`
  - `REFUSED_MESSAGE = "Luna не запущена: папка не отмечена как доверенная."`

- [ ] **Step 1: Write the failing tests**

`tests/test_trust.py`:

```python
import io

import pytest
from rich.console import Console

from luna.core.projects import ProjectIndex
from luna.ui.trust import REFUSED_MESSAGE, confirm_trust, ensure_trusted


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=100, force_terminal=False), buf


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "д", "да"])
def test_confirm_accepts_yes_answers(tmp_path, answer):
    console, buf = _console()
    assert confirm_trust(console, str(tmp_path), input_fn=lambda _p: answer) is True
    assert str(tmp_path) in buf.getvalue()


@pytest.mark.parametrize("answer", ["", "n", "нет", "maybe"])
def test_confirm_rejects_anything_else(tmp_path, answer):
    console, _ = _console()
    assert confirm_trust(console, str(tmp_path), input_fn=lambda _p: answer) is False


def test_ensure_trusted_records_trust_on_yes(tmp_path):
    console, _ = _console()
    assert ensure_trusted(console, str(tmp_path), input_fn=lambda _p: "y") is True
    assert ProjectIndex().is_trusted(str(tmp_path)) is True


def test_ensure_trusted_refusal_writes_nothing(tmp_path):
    console, buf = _console()
    assert ensure_trusted(console, str(tmp_path), input_fn=lambda _p: "n") is False
    assert ProjectIndex().list() == []
    assert REFUSED_MESSAGE in buf.getvalue()


@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_ctrl_c_or_eof_counts_as_refusal(tmp_path, exc):
    def boom(_prompt):
        raise exc

    console, buf = _console()
    assert ensure_trusted(console, str(tmp_path), input_fn=boom) is False
    assert ProjectIndex().list() == []
    assert REFUSED_MESSAGE in buf.getvalue()


def test_trusted_folder_skips_the_prompt(tmp_path):
    ProjectIndex().trust(str(tmp_path))

    def never(_prompt):
        pytest.fail("a trusted folder must not be asked about again")

    console, _ = _console()
    assert ensure_trusted(console, str(tmp_path), input_fn=never) is True
```

Append to `tests/test_cli_tui_entry.py`:

```python
def test_refusing_trust_exits_1_without_launching_the_tui(monkeypatch, tmp_path):
    import luna.cli as cli_mod

    _force_interactive(monkeypatch, cli_mod)
    monkeypatch.setattr(cli_mod, "ensure_trusted", lambda *a, **k: False)
    monkeypatch.setattr(
        cli_mod, "run_tui", lambda *a, **k: pytest.fail("TUI must not start in an untrusted folder")
    )

    assert cli_mod.main(["--no-splash", "--workdir", str(tmp_path)]) == 1


def test_one_shot_prompt_never_asks_for_trust(monkeypatch, tmp_path):
    import luna.cli as cli_mod

    _force_interactive(monkeypatch, cli_mod)
    monkeypatch.setattr(
        cli_mod, "ensure_trusted", lambda *a, **k: pytest.fail("-p must not prompt for trust")
    )
    monkeypatch.setattr(cli_mod, "run_once", lambda *a, **k: None)

    assert cli_mod.main(["--no-splash", "--workdir", str(tmp_path), "-p", "hi"]) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_trust.py tests/test_cli_tui_entry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna.ui.trust'`, and `AttributeError: <module 'luna.cli'> has no attribute 'ensure_trusted'`

- [ ] **Step 3: Implement `luna/ui/trust.py`**

```python
"""Claude-Code-style "do you trust this folder?" gate, shown before the TUI starts."""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console

from luna.core.projects import ProjectIndex
from luna.ui.interact import arrow_pick
from luna.ui.theme import PALETTE

REFUSED_MESSAGE = "Luna не запущена: папка не отмечена как доверенная."
_YES = {"y", "yes", "д", "да"}


def confirm_trust(console: Console, workdir: str, input_fn: Callable[[str], str] = input) -> bool:
    """Ask whether ``workdir`` is trusted; ``True`` only on an explicit yes.

    Uses the arrow-key picker in a real terminal and falls back to a plain
    ``[y/N]`` question otherwise (``arrow_pick`` returns ``None`` there).
    Esc / Ctrl+C propagate as ``KeyboardInterrupt`` for the caller to treat
    as a refusal.
    """
    console.print()
    console.print("[bold]Доверяете этой папке?[/]")
    console.print()
    console.print(workdir, style=PALETTE["peri"], highlight=False)
    console.print()
    console.print(
        "Luna сможет читать и изменять файлы в этой папке и запускать в ней\n"
        "команды. Открывайте только проекты, которым доверяете.",
        style=PALETTE["moon_dim"],
    )
    console.print()
    choice = arrow_pick(console, input_fn, [("yes", "Да, доверяю"), ("no", "Нет, выйти")])
    if choice is None:
        return input_fn("Доверяете? [y/N] ").strip().lower() in _YES
    return choice == "yes"


def ensure_trusted(
    console: Console,
    workdir: str,
    *,
    index: ProjectIndex | None = None,
    input_fn: Callable[[str], str] = input,
) -> bool:
    """Return whether Luna may run in ``workdir``, prompting once if unknown.

    A trusted folder is only touched (``last_opened``); an unknown one is
    asked about and recorded on yes. A refusal — including Esc, Ctrl+C or a
    closed stdin — writes nothing and prints :data:`REFUSED_MESSAGE`.
    """
    index = index or ProjectIndex()
    if index.is_trusted(workdir):
        index.touch(workdir)
        return True
    try:
        trusted = confirm_trust(console, workdir, input_fn)
    except (KeyboardInterrupt, EOFError):
        console.print()
        trusted = False
    if trusted:
        index.trust(workdir)
    else:
        console.print(REFUSED_MESSAGE)
    return trusted
```

- [ ] **Step 4: Wire the gate into `luna/cli.py`**

Add to the imports block (alphabetical, next to `from luna.ui.splash import render_splash`):

```python
from luna.ui.trust import ensure_trusted
```

In `main()`, directly after the line `interactive = console.is_terminal and sys.stdin.isatty() and not args.no_input`, insert:

```python
    # Trust gate: only the TUI path asks. `-p` and a piped-stdin REPL are
    # scripted invocations, where the invocation itself is the consent.
    if interactive and not prompt and not ensure_trusted(console, config.workdir):
        return 1
```

- [ ] **Step 5: Keep the existing TUI-entry tests off the real prompt**

In `tests/test_cli_tui_entry.py`, in `test_main_launches_tui_when_interactive_and_no_prompt` add after `monkeypatch.setattr(cli_mod, "run_tui", fake_run_tui)`:

```python
    monkeypatch.setattr(cli_mod, "ensure_trusted", lambda *a, **k: True)
```

and in `_force_interactive` add as its first line:

```python
    monkeypatch.setattr(cli_mod, "ensure_trusted", lambda *a, **k: True)
```

(The two new tests above override it again after calling `_force_interactive`.)

- [ ] **Step 6: Run the tests and the whole CLI suite**

Run: `uv run pytest tests/test_trust.py tests/test_cli_tui_entry.py tests/test_cli.py -v`
Expected: all pass. If any other `test_cli.py` test now fails reading stdin ("reading from stdin while output is captured"), it reaches the TUI path: add the same `monkeypatch.setattr(cli_mod, "ensure_trusted", lambda *a, **k: True)` line to it.

- [ ] **Step 7: Commit**

```bash
git add luna/ui/trust.py luna/cli.py tests/test_trust.py tests/test_cli_tui_entry.py tests/test_cli.py
git commit -m "feat: ask to trust an unknown folder before launching the TUI; refusal exits 1"
```

---

### Task 3: Server-side trust enforcement

**Files:**
- Create: `luna/server/trust.py`
- Modify: `luna/server/app.py` (`create_app`), `luna/server/run.py` (new `make_trust_check`, `run_serve`), `luna/server/sessions.py` (`list_sessions`, `create_session`), `luna/server/turns.py` (`get_history`, `post_message`), `luna/server/approvals.py` (`post_approve`)
- Test: `tests/test_server_trust.py`

**Interfaces:**
- Consumes: `ProjectIndex` (Task 1).
- Produces:
  - `luna.server.trust.trust_error(request: Request, workdir: str | None) -> JSONResponse | None`
  - `create_app(agent_factory, *, token: str, trust_check: Callable[[str], bool] | None = None) -> Starlette`
  - `luna.server.run.make_trust_check() -> Callable[[str], bool]`

- [ ] **Step 1: Write the failing tests**

`tests/test_server_trust.py`:

```python
import httpx
import pytest

from luna.core.projects import ProjectIndex
from luna.server.app import create_app
from luna.server.run import make_trust_check


def _client(trust_check):
    app = create_app(agent_factory=lambda _w: None, token="t", trust_check=trust_check)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer t"},
    )


ROUTES = [
    ("GET", "/sessions", "params"),
    ("POST", "/sessions", "json"),
    ("GET", "/sessions/t1/messages", "params"),
    ("POST", "/sessions/t1/messages", "json"),
    ("POST", "/sessions/t1/approve", "json"),
]


@pytest.mark.parametrize(("method", "path", "where"), ROUTES)
async def test_untrusted_workdir_is_rejected_on_every_route(tmp_path, method, path, where):
    body = {"workdir": str(tmp_path), "content": "hi", "decision": {"type": "approve"}}
    kwargs = {"params": {"workdir": str(tmp_path)}} if where == "params" else {"json": body}
    async with _client(make_trust_check()) as c:
        resp = await c.request(method, path, **kwargs)
    assert resp.status_code == 403
    assert resp.json() == {"error": "workdir_not_trusted", "workdir": str(tmp_path)}


async def test_missing_workdir_is_a_400(tmp_path):
    async with _client(make_trust_check()) as c:
        resp = await c.get("/sessions")
    assert resp.status_code == 400
    assert resp.json() == {"error": "workdir_required"}


async def test_trust_granted_after_the_server_started_is_honoured(tmp_path):
    async with _client(make_trust_check()) as c:
        assert (await c.get("/sessions", params={"workdir": str(tmp_path)})).status_code == 403
        ProjectIndex().trust(str(tmp_path))  # e.g. a later `luna` launch in this folder
        assert (await c.get("/sessions", params={"workdir": str(tmp_path)})).status_code == 200


async def test_no_trust_check_means_no_enforcement(tmp_path):
    async with _client(None) as c:
        resp = await c.get("/sessions", params={"workdir": str(tmp_path)})
    assert resp.status_code == 200


def test_run_serve_wires_the_real_trust_check(tmp_path, monkeypatch):
    import uvicorn

    import luna.server.app as app_mod
    import luna.server.run as run_mod

    captured = {}
    real_create_app = app_mod.create_app

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_create_app(*args, **kwargs)

    monkeypatch.setattr(app_mod, "create_app", spy)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "write_token_file", lambda **k: None)

    assert run_mod.run_serve(["--port", "1", "--token", "x"]) == 0
    check = captured["trust_check"]
    assert check(str(tmp_path)) is False
    ProjectIndex().trust(str(tmp_path))
    assert check(str(tmp_path)) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_trust.py -v`
Expected: FAIL with `ImportError: cannot import name 'make_trust_check'`

- [ ] **Step 3: Implement `luna/server/trust.py`**

```python
"""Per-request workdir trust enforcement for the local server.

The server is long-lived and shared across projects, so it re-checks trust
on every request instead of trusting whatever the client claims — a future
web/desktop client must not be able to bypass the CLI's trust prompt.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse


def trust_error(request: Request, workdir: str | None) -> JSONResponse | None:
    """Return an error response if ``workdir`` may not be used, else ``None``.

    No-op when the app was built without a ``trust_check`` (tests, embedding).
    """
    check = getattr(request.app.state, "trust_check", None)
    if check is None:
        return None
    if not workdir:
        return JSONResponse({"error": "workdir_required"}, status_code=400)
    if not check(workdir):
        return JSONResponse(
            {"error": "workdir_not_trusted", "workdir": workdir}, status_code=403
        )
    return None
```

- [ ] **Step 4: Accept `trust_check` in `create_app`**

In `luna/server/app.py` change the signature and store it:

```python
def create_app(
    agent_factory: Callable[[str], object],
    *,
    token: str,
    trust_check: Callable[[str], bool] | None = None,
) -> Starlette:
```

Add to its docstring: ``"``trust_check(workdir)`` gates every workdir-scoped route (see :mod:`luna.server.trust`); ``None`` disables the check."`` After `app.state.agent_factory = agent_factory` add:

```python
    app.state.trust_check = trust_check
```

- [ ] **Step 5: Add `make_trust_check` and use it in `run_serve`**

In `luna/server/run.py`, after `make_agent_factory`:

```python
def make_trust_check() -> Callable[[str], bool]:
    """Build the server's trust check against ``luna_projects``.

    Opens a fresh :class:`luna.core.projects.ProjectIndex` per call: trust is
    written by a separate CLI process, possibly long after this server
    started, and must take effect immediately.
    """

    def trust_check(workdir: str) -> bool:
        from luna.core.projects import ProjectIndex

        return ProjectIndex().is_trusted(workdir)

    return trust_check
```

In `run_serve` replace the `create_app(...)` line with:

```python
    app = create_app(
        agent_factory=make_agent_factory(), token=token, trust_check=make_trust_check()
    )
```

- [ ] **Step 6: Guard every handler**

`luna/server/sessions.py` — add `from luna.server.trust import trust_error`, then:

```python
async def list_sessions(request: Request) -> JSONResponse:
    """List sessions for a workdir, newest first."""
    workdir = request.query_params.get("workdir")
    if (error := trust_error(request, workdir)) is not None:
        return error
    index = SessionIndex()
    rows = index.list(workdir=workdir)
    # ... response body unchanged in this task
```

```python
async def create_session(request: Request) -> JSONResponse:
    """Create a new session with a random thread_id."""
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    thread_id = uuid.uuid4().hex
    return JSONResponse({"thread_id": thread_id, "workdir": body.get("workdir", ".")})
```

`luna/server/turns.py` — add `from starlette.responses import JSONResponse` (already imported) and `from luna.server.trust import trust_error`. In `get_history`:

```python
    thread_id = request.path_params["thread_id"]
    raw_workdir = request.query_params.get("workdir")
    if (error := trust_error(request, raw_workdir)) is not None:
        return error
    workdir = raw_workdir or "."
```

In `post_message` (change the return annotation to `EventSourceResponse | JSONResponse`):

```python
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    content = body["content"]
    workdir = body.get("workdir", ".")
```

`luna/server/approvals.py` — add `from starlette.responses import JSONResponse` and `from luna.server.trust import trust_error`; annotation `-> EventSourceResponse | JSONResponse`:

```python
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    workdir = body.get("workdir", ".")
```

- [ ] **Step 7: Run the new and all server tests**

Run: `uv run pytest tests/test_server_trust.py tests/test_server_app.py tests/test_server_sessions.py tests/test_server_turns.py tests/test_server_approvals.py tests/test_server_run.py -v`
Expected: all pass (existing tests build `create_app` without `trust_check`, so they are unaffected).

- [ ] **Step 8: Commit**

```bash
git add luna/server tests/test_server_trust.py
git commit -m "feat: server rejects untrusted workdirs (injectable trust_check, wired in luna serve)"
```

---

### Task 4: Day groups and relative time in `/sessions`

**Files:**
- Modify: `luna/server/sessions.py` (replace `_relative_time`; add `group` to the response)
- Modify: `luna/core/persistence.py:121` (`limit: int = 200`)
- Test: `tests/test_server_sessions.py`

**Interfaces:**
- Produces:
  - `session_group(updated: float, *, now: float | None = None) -> str` — one of `"today" | "yesterday" | "week" | "older"`
  - `relative_time(updated: float, *, now: float | None = None) -> str`
  - `/sessions` rows: `{"thread_id", "workdir", "title", "updated", "group", "relative_time"}`

Reference dates: 2026-09-28 is a Monday; 2026-09-22 a Tuesday; 2026-09-21 a Monday; 2026-09-20 a Sunday.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_sessions.py` (merge the new imports into the file's existing import block at the top — it already imports `pytest` and `SessionIndex`):

```python
from datetime import datetime

import pytest

from luna.server.sessions import relative_time, session_group

NOW = datetime(2026, 9, 28, 12, 0).timestamp()


def _ts(*args) -> float:
    return datetime(*args).timestamp()


@pytest.mark.parametrize(
    ("updated", "group", "label"),
    [
        (NOW - 30, "today", "сейчас"),
        (NOW - 11 * 60, "today", "11м"),
        (NOW - 3 * 3600, "today", "3ч"),
        (_ts(2026, 9, 27, 23, 59), "yesterday", "23:59"),
        (_ts(2026, 9, 22, 10, 0), "week", "вт"),
        (_ts(2026, 9, 21, 10, 0), "older", "21 сен"),
        (_ts(2026, 9, 20, 10, 0), "older", "20 сен"),
        (_ts(2025, 12, 31, 10, 0), "older", "31 дек"),
    ],
)
def test_group_and_label(updated, group, label):
    assert session_group(updated, now=NOW) == group
    assert relative_time(updated, now=NOW) == label


def test_calendar_day_boundary_not_24_hours():
    just_after_midnight = _ts(2026, 9, 28, 0, 1)
    two_minutes_earlier = _ts(2026, 9, 27, 23, 59)
    assert session_group(two_minutes_earlier, now=just_after_midnight) == "yesterday"


def test_future_timestamp_is_treated_as_now():
    assert session_group(NOW + 600, now=NOW) == "today"
    assert relative_time(NOW + 600, now=NOW) == "сейчас"


async def test_list_sessions_includes_group(client, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    SessionIndex().record("t1", "/repo", "first session")
    rows = (await client.get("/sessions", params={"workdir": "/repo"})).json()["sessions"]
    assert rows[0]["group"] == "today"


async def test_list_sessions_returns_more_than_twenty(client, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    index = SessionIndex()
    for i in range(25):
        index.record(f"t{i}", "/repo", f"session {i}")
    rows = (await client.get("/sessions", params={"workdir": "/repo"})).json()["sessions"]
    assert len(rows) == 25
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_sessions.py -v`
Expected: FAIL with `ImportError: cannot import name 'relative_time'`

- [ ] **Step 3: Implement grouping in `luna/server/sessions.py`**

Replace `_relative_time` and its `import time` usage with:

```python
import time
import uuid
from datetime import date, datetime

_MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def session_group(updated: float, *, now: float | None = None) -> str:
    """Bucket a timestamp by local calendar day: today/yesterday/week/older.

    Calendar days, not 24-hour windows: 23:59 yesterday is "yesterday" even
    when it is only two minutes ago. A future timestamp (clock skew) counts
    as today.
    """
    now = time.time() if now is None else now
    days = (date.fromtimestamp(now) - date.fromtimestamp(updated)).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "week"
    return "older"


def relative_time(updated: float, *, now: float | None = None) -> str:
    """Short label matching the group: "11м"/"3ч" today, "14:02" yesterday,
    "вт" this week, "25 сен" earlier.

    Russian month names come from a fixed table, not ``strftime("%b")``,
    which follows the process locale (it rendered "Sep").
    """
    now = time.time() if now is None else now
    group = session_group(updated, now=now)
    when = datetime.fromtimestamp(updated)
    if group == "today":
        delta = max(0.0, now - updated)
        if delta < 60:
            return "сейчас"
        if delta < 3600:
            return f"{int(delta // 60)}м"
        return f"{int(delta // 3600)}ч"
    if group == "yesterday":
        return when.strftime("%H:%M")
    if group == "week":
        return _WEEKDAYS[when.weekday()]
    return f"{when.day} {_MONTHS[when.month - 1]}"
```

In `list_sessions`, build each row as:

```python
                {
                    "thread_id": r.thread_id,
                    "workdir": r.workdir,
                    "title": r.title,
                    "updated": r.updated,
                    "group": session_group(r.updated),
                    "relative_time": relative_time(r.updated),
                }
```

- [ ] **Step 4: Raise the default list limit**

In `luna/core/persistence.py`, `SessionIndex.list`: change `limit: int = 20` to `limit: int = 200`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_server_sessions.py tests/test_persistence.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add luna/server/sessions.py luna/core/persistence.py tests/test_server_sessions.py
git commit -m "feat: /sessions groups sessions by calendar day with Russian relative labels; limit 200"
```

---

### Task 5: Tool calls in history and in the live `tool_started` event

**Files:**
- Modify: `luna/core/turn_events.py` (extract `tool_outcome`, add `args_preview`, use in `iter_turn`)
- Modify: `luna/server/turns.py` (`_event_dict` adds `args_preview`; `get_history` via new `history_entries`)
- Test: `tests/test_turn_events.py`, `tests/test_server_turns.py`

**Interfaces:**
- Produces:
  - `luna.core.turn_events.tool_outcome(message) -> tuple[bool, str]`
  - `luna.core.turn_events.args_preview(args: dict, limit: int = 40) -> str`
  - `luna.server.turns.history_entries(raw_messages: list) -> list[dict]` — entries are `{"role": "human"|"ai", "content": str}` or `{"role": "tool", "name": str, "args_preview": str, "ok": bool, "detail": str}`
  - SSE `tool_started` event gains `"args_preview": str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_turn_events.py`:

```python
from langchain_core.messages import ToolMessage

from luna.core.turn_events import args_preview, tool_outcome


def test_tool_outcome_first_line_of_body():
    msg = ToolMessage(content="8 results\nmore", tool_call_id="c1", name="web_search")
    assert tool_outcome(msg) == (True, "8 results")


def test_tool_outcome_quiet_tools_have_no_detail_on_success():
    msg = ToolMessage(content="line 1\nline 2", tool_call_id="c1", name="read_file")
    assert tool_outcome(msg) == (True, "")


def test_tool_outcome_error_keeps_detail_even_for_quiet_tools():
    msg = ToolMessage(content="No such file", tool_call_id="c1", name="read_file", status="error")
    assert tool_outcome(msg) == (False, "No such file")


def test_args_preview_first_non_empty_string_collapsed_and_truncated():
    assert args_preview({"n": 3, "query": "  погода\n Орёл  "}) == "погода Орёл"
    long = args_preview({"q": "x" * 100})
    assert len(long) == 40 and long.endswith("…")
    assert args_preview({"n": 3}) == ""
```

In `tests/test_server_turns.py`, replace `test_get_history_returns_prior_human_and_ai_turns_only` with:

```python
async def test_get_history_includes_tool_calls_in_order(tmp_path):
    from types import SimpleNamespace

    class _StubAgent:
        def get_state(self, config):
            return SimpleNamespace(
                values={
                    "messages": [
                        SimpleNamespace(type="human", content="what does this repo do"),
                        SimpleNamespace(
                            type="ai",
                            content="",
                            tool_calls=[
                                {"id": "c1", "name": "read_file", "args": {"file_path": "/README.md"}}
                            ],
                        ),
                        SimpleNamespace(
                            type="tool",
                            content="# Luna",
                            tool_call_id="c1",
                            name="read_file",
                            status="success",
                        ),
                        SimpleNamespace(type="ai", content="it parses the config file"),
                    ]
                }
            )

    app = create_app(agent_factory=lambda _workdir: _StubAgent(), token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer secret-token"},
    ) as c:
        resp = await c.get("/sessions/t1/messages", params={"workdir": str(tmp_path)})

    assert resp.json()["messages"] == [
        {"role": "human", "content": "what does this repo do"},
        {
            "role": "tool",
            "name": "read_file",
            "args_preview": "/README.md",
            "ok": True,
            "detail": "",
        },
        {"role": "ai", "content": "it parses the config file"},
    ]


def test_history_shows_only_text_from_block_content():
    """Anthropic AI messages that call tools carry a list of content blocks."""
    from types import SimpleNamespace

    from luna.server.turns import history_entries

    blocks = [
        {"type": "text", "text": "Сейчас посмотрю."},
        {"type": "tool_use", "id": "c1", "name": "ls", "input": {}},
    ]
    entries = history_entries([SimpleNamespace(type="ai", content=blocks, tool_calls=[])])
    assert entries == [{"role": "ai", "content": "Сейчас посмотрю."}]


def test_history_marks_errored_tool_calls():
    from types import SimpleNamespace

    from luna.server.turns import history_entries

    entries = history_entries(
        [
            SimpleNamespace(
                type="ai", content="", tool_calls=[{"id": "c1", "name": "execute", "args": {"command": "false"}}]
            ),
            SimpleNamespace(
                type="tool", content="exit 1", tool_call_id="c1", name="execute", status="error"
            ),
        ]
    )
    assert entries == [
        {"role": "tool", "name": "execute", "args_preview": "false", "ok": False, "detail": "exit 1"}
    ]
```

Add a live-event test next to the existing SSE tests in `tests/test_server_turns.py` (it reuses the file's `_parse_sse` helper):

```python
def test_tool_started_event_carries_args_preview():
    from luna.core.turn_events import ToolStarted
    from luna.server.turns import _event_dict

    evt = _event_dict(ToolStarted("c1", "web_search", {"query": "погода Орёл"}))
    assert evt["args_preview"] == "погода Орёл"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_turn_events.py tests/test_server_turns.py -v`
Expected: FAIL with `ImportError: cannot import name 'args_preview'`

- [ ] **Step 3: Extract helpers in `luna/core/turn_events.py`**

Add after `_RELOAD_MARKER`:

```python
def tool_outcome(message) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for a finished tool call's ``ToolMessage``.

    ``detail`` is the body's first line (max 120 chars), blank on success
    for the read-only navigation tools in ``_QUIET_ON_SUCCESS``. Shared by
    the live ``ToolFinished`` event and the server's history replay so both
    show identical rows.
    """
    body = str(message.content) if message.content else ""
    detail = body.splitlines()[0][:120] if body else ""
    ok = getattr(message, "status", "success") != "error"
    if ok and getattr(message, "name", None) in _QUIET_ON_SUCCESS:
        detail = ""
    return ok, detail


def args_preview(args: dict, limit: int = 40) -> str:
    """The first non-empty string argument, whitespace-collapsed and truncated."""
    for value in args.values():
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= limit else text[: limit - 1] + "…"
    return ""
```

In `iter_turn`, replace the inline block

```python
                        body = str(m.content) if m.content else ""
                        detail = body.splitlines()[0][:120] if body else ""
                        ok = getattr(m, "status", "success") != "error"
                        if ok and m.name in _QUIET_ON_SUCCESS:
                            detail = ""
                        yield ToolFinished(m.tool_call_id, m.name or "", ok, detail)
```

with

```python
                        body = str(m.content) if m.content else ""
                        ok, detail = tool_outcome(m)
                        yield ToolFinished(m.tool_call_id, m.name or "", ok, detail)
```

(`body` is still needed by the `_RELOAD_MARKER` check right below.)

- [ ] **Step 4: History and the live event in `luna/server/turns.py`**

Extend the `luna.core.turn_events` import with `args_preview, tool_outcome`. In `_event_dict`'s `ToolStarted` branch add `"args_preview": args_preview(event.args),`.

Add above `get_history`:

```python
def _text_of(content) -> str:
    """Plain text of a message's content: a string, or the text blocks of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block if isinstance(block, str) else block.get("text", "")
            for block in content
            if isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text")
        ]
        return "".join(parts)
    return ""


def history_entries(raw_messages: list) -> list[dict]:
    """Turn a thread's raw messages into the transcript the TUI replays.

    Human and AI text plus one entry per finished tool call, in order, with
    the same ``ok``/``detail`` the live ``tool_finished`` event carries
    (:func:`luna.core.turn_events.tool_outcome`). Durations are not stored,
    so replayed tool rows have none.
    """
    calls: dict[str, dict] = {}
    entries: list[dict] = []
    for m in raw_messages:
        kind = getattr(m, "type", None)
        if kind in ("human", "ai"):
            text = _text_of(m.content)
            if text:
                entries.append({"role": kind, "content": text})
            for call in getattr(m, "tool_calls", None) or []:
                if call.get("id"):
                    calls[call["id"]] = call
        elif kind == "tool":
            call = calls.get(getattr(m, "tool_call_id", None), {})
            ok, detail = tool_outcome(m)
            entries.append(
                {
                    "role": "tool",
                    "name": getattr(m, "name", None) or call.get("name", ""),
                    "args_preview": args_preview(call.get("args") or {}),
                    "ok": ok,
                    "detail": detail,
                }
            )
    return entries
```

In `get_history` replace the `messages = [...]` comprehension with `messages = history_entries(raw_messages)` and update its docstring's second paragraph to: "Includes finished tool calls (see :func:`history_entries`) so a replayed session shows the same inline tool rows the live transcript did."

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_turn_events.py tests/test_server_turns.py tests/test_session.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add luna/core/turn_events.py luna/server/turns.py tests/test_turn_events.py tests/test_server_turns.py
git commit -m "feat: history replays tool calls; tool_started carries args_preview; shared tool_outcome"
```

---

### Task 6: Sessions sidebar with project header, day groups, Ctrl+N

**Files:**
- Modify: `luna/tui/sidebar_sessions.py` (rewrite)
- Modify: `luna/tui/app.py` (bindings, new-session action, current-row sync, refresh after a turn)
- Modify: `luna/tui/chat.py` (post `ChatPane.TurnFinished` at the end of `on_input_submitted`)
- Modify: `luna/tui/luna.tcss` (sidebar section)
- Test: `tests/test_tui_sidebars.py`

**Interfaces:**
- Consumes: `/sessions` rows with `group` and `relative_time` (Task 4); `ServerClient.create_session(workdir) -> str`.
- Produces:
  - `SessionsSidebar(*, workdir: str, current_thread_id: str | None = None)` with `refresh_sessions()`, `set_current(thread_id: str)`, messages `SessionSelected(thread_id)` and `NewSessionRequested()`
  - `short_path(path: str, width: int) -> str`
  - `GROUP_LABELS: dict[str, str]`
  - `ChatPane.TurnFinished` message (no fields)
  - `LunaApp.action_new_session()` (async), binding `ctrl+n`

- [ ] **Step 1: Write the failing tests**

Replace `test_sessions_sidebar_lists_sessions_from_the_server` in `tests/test_tui_sidebars.py` with the following (keep the two activity-sidebar tests for now; Task 7 deletes them):

```python
import time

import httpx
import pytest
from langchain_core.messages import AIMessage
from textual.widgets import Input, Label

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.persistence import SessionIndex
from luna.server.app import create_app
from luna.tui.app import LunaApp
from luna.tui.chat import ChatPane
from luna.tui.sidebar_sessions import SessionsSidebar, short_path


def _app(tmp_path, fake_model, thread_id="t1") -> LunaApp:
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    transport = httpx.ASGITransport(app=create_app(agent_factory=lambda _w: agent, token="t"))
    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id=thread_id)
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")
    return app


def _age(thread_id: str, seconds: float) -> None:
    idx = SessionIndex()
    idx._conn.execute(
        "UPDATE luna_sessions SET updated = ? WHERE thread_id = ?",
        (time.time() - seconds, thread_id),
    )
    idx._conn.commit()


def test_short_path_uses_tilde_and_truncates_from_the_left(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert short_path(str(tmp_path / "a" / "b"), 30) == "~/a/b"
    long = short_path(str(tmp_path / ("x" * 50)), 20)
    assert len(long) == 20 and long.startswith("…")


async def test_sidebar_groups_sessions_and_skips_empty_groups(tmp_path, fake_model):
    idx = SessionIndex()
    idx.record("t1", str(tmp_path), "fix the bug in toolguard")
    idx.record("t2", str(tmp_path), "old question")
    _age("t2", 30 * 86400)
    app = _app(tmp_path, fake_model)
    async with app.run_test():
        sidebar = app.query_one(SessionsSidebar)
        await sidebar.refresh_sessions()
        groups = [str(item.query_one(Label).render()) for item in sidebar.query(".session-group")]
        assert groups == ["СЕГОДНЯ", "РАНЕЕ"]
        assert all(item.disabled for item in sidebar.query(".session-group"))
        titles = [item.data_thread_id for item in sidebar.query(".session-item")]
        assert titles == ["t1", "t2"]


async def test_current_session_is_highlighted_and_follows_set_current(tmp_path, fake_model):
    idx = SessionIndex()
    idx.record("t1", str(tmp_path), "one")
    idx.record("t2", str(tmp_path), "two")
    app = _app(tmp_path, fake_model, thread_id="t1")
    async with app.run_test():
        sidebar = app.query_one(SessionsSidebar)
        await sidebar.refresh_sessions()
        current = [i.data_thread_id for i in sidebar.query(".session-item.-current")]
        assert current == ["t1"]
        sidebar.set_current("t2")
        current = [i.data_thread_id for i in sidebar.query(".session-item.-current")]
        assert current == ["t2"]


async def test_ctrl_n_starts_a_fresh_session(tmp_path, fake_model):
    app = _app(tmp_path, fake_model, thread_id="t1")
    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        chat = app.query_one(ChatPane)
        assert chat.thread_id not in (None, "t1")
        assert app.query_one(SessionsSidebar).current_thread_id == chat.thread_id


async def test_sidebar_shows_the_session_after_its_first_turn(tmp_path, fake_model):
    app = _app(tmp_path, fake_model, thread_id="t-new")
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "hello"))
        await pilot.pause()
        ids = [i.data_thread_id for i in app.query_one(SessionsSidebar).query(".session-item")]
        assert "t-new" in ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui_sidebars.py -v`
Expected: FAIL with `ImportError: cannot import name 'short_path'`

- [ ] **Step 3: Rewrite `luna/tui/sidebar_sessions.py`**

```python
"""Left sidebar: the current project's sessions, grouped by day."""

from __future__ import annotations

import os
from pathlib import Path

from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView

#: Client-side labels for the server's language-neutral ``group`` values.
GROUP_LABELS = {
    "today": "Сегодня",
    "yesterday": "Вчера",
    "week": "На этой неделе",
    "older": "Ранее",
}


def short_path(path: str, width: int) -> str:
    """``$HOME`` shortened to ``~``; truncated from the left with ``…`` to ``width``."""
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        path = "~" + path[len(home) :]
    return path if len(path) <= width else "…" + path[-(width - 1) :]


class SessionsSidebar(Widget):
    """The open project's sessions; posts SessionSelected / NewSessionRequested.

    Styling lives in ``luna/tui/luna.tcss`` (external stylesheet), not a
    ``DEFAULT_CSS`` string here.
    """

    class SessionSelected(Message):
        """Posted when the user picks a session in the list."""

        def __init__(self, thread_id: str) -> None:
            self.thread_id = thread_id
            super().__init__()

    class NewSessionRequested(Message):
        """Posted when the user picks the "+ новая сессия" row."""

    def __init__(self, *, workdir: str, current_thread_id: str | None = None) -> None:
        super().__init__()
        self._workdir = workdir
        self.current_thread_id = current_thread_id

    def compose(self):
        """Yield the project header and the session list."""
        with Vertical():
            name = Path(self._workdir).name or self._workdir
            yield Label(f"◐ {name}", classes="project-name")
            yield Label(short_path(self._workdir, 28), classes="project-path")
            yield ListView(id="session-list")

    async def refresh_sessions(self) -> None:
        """Fetch this project's sessions and rebuild the grouped list.

        Group headers are disabled ``ListItem``s: ``ListView`` skips disabled
        items when moving the cursor, and they cannot be selected.
        """
        sessions = await self.app.client.list_sessions(self._workdir)
        list_view = self.query_one("#session-list", ListView)
        await list_view.clear()
        items: list[ListItem] = [ListItem(_NewSessionRow(), classes="new-session-item")]
        group = None
        for s in sessions:
            if s["group"] != group:
                group = s["group"]
                items.append(
                    ListItem(
                        Label(GROUP_LABELS[group].upper()),
                        classes="session-group",
                        disabled=True,
                    )
                )
            item = ListItem(_SessionRow(s["title"], s["relative_time"]), classes="session-item")
            item.data_thread_id = s["thread_id"]  # plain attribute, no reactive needed here
            item.set_class(s["thread_id"] == self.current_thread_id, "-current")
            items.append(item)
        await list_view.extend(items)

    def set_current(self, thread_id: str) -> None:
        """Move the ``-current`` highlight to ``thread_id``'s row."""
        self.current_thread_id = thread_id
        for item in self.query(".session-item"):
            item.set_class(item.data_thread_id == thread_id, "-current")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Post NewSessionRequested or SessionSelected for the picked row."""
        if event.item.has_class("new-session-item"):
            self.post_message(self.NewSessionRequested())
            return
        thread_id = getattr(event.item, "data_thread_id", None)
        if thread_id:
            self.post_message(self.SessionSelected(thread_id))


class _SessionRow(Horizontal):
    """One line: ellipsis-truncated title, relative time right-aligned."""

    def __init__(self, title: str, relative_time: str) -> None:
        super().__init__()
        self._title = title
        self._relative_time = relative_time

    def compose(self):
        yield Label(self._title, classes="session-title")
        yield Label(self._relative_time, classes="session-time")


class _NewSessionRow(Horizontal):
    """The "+ новая сессия ^n" row at the top of the list."""

    def compose(self):
        yield Label("+ новая сессия", classes="session-title")
        yield Label("^n", classes="session-time")
```

- [ ] **Step 4: Post `TurnFinished` from `ChatPane`**

In `luna/tui/chat.py`, add `from textual.message import Message` to the imports and inside `class ChatPane` (above `thread_id: reactive...`):

```python
    class TurnFinished(Message):
        """Posted after a turn's stream ends, so the sidebar can re-sort."""
```

At the end of `on_input_submitted`, change the trailing

```python
        finally:
            await reply.stop()
```

to

```python
        finally:
            await reply.stop()
            self.post_message(self.TurnFinished())
```

- [ ] **Step 5: Wire the app in `luna/tui/app.py`**

Bindings:

```python
    BINDINGS = [
        ("ctrl+b", "toggle_panels", "Панель"),
        ("ctrl+n", "new_session", "Новая сессия"),
    ]
```

In `compose`, construct the sidebar with the start thread:

```python
        sessions_sidebar = SessionsSidebar(
            workdir=self._workdir, current_thread_id=self._start_thread_id
        )
```

Replace `on_sessions_sidebar_session_selected` and add three methods:

```python
    async def on_sessions_sidebar_session_selected(
        self, event: SessionsSidebar.SessionSelected
    ) -> None:
        """Switch the chat pane to the picked session's thread and reload it."""
        await self._open_thread(event.thread_id)

    async def on_sessions_sidebar_new_session_requested(
        self, event: SessionsSidebar.NewSessionRequested
    ) -> None:
        """The "+ новая сессия" row does the same as Ctrl+N."""
        await self.action_new_session()

    async def action_new_session(self) -> None:
        """Start a fresh thread in this project (Ctrl+N)."""
        await self._open_thread(await self.client.create_session(self._workdir))

    async def on_chat_pane_turn_finished(self, event: ChatPane.TurnFinished) -> None:
        """Re-fetch the list so a new session appears and the order updates."""
        await self.query_one(SessionsSidebar).refresh_sessions()

    async def _open_thread(self, thread_id: str) -> None:
        """Point the chat at ``thread_id``, replay its history, sync the highlight.

        Without the reload, the transcript kept showing whatever the
        previously-open session had streamed into it.
        """
        chat = self.query_one(ChatPane)
        chat.thread_id = thread_id
        await chat.load_history()
        self.query_one(SessionsSidebar).set_current(thread_id)
        self.query_one("#chat-input").focus()
```

- [ ] **Step 6: Sidebar styles in `luna/tui/luna.tcss`**

Change `#sessions-sidebar { width: 24; ... }` to `width: 30;`. Replace the whole `SessionsSidebar _SessionRow` / `.session-title` / `.session-time` block with:

```css
SessionsSidebar .project-name {
    padding: 0 1;
    color: $moon;
    text-style: bold;
}

SessionsSidebar .project-path {
    padding: 0 1 1 1;
    color: $border;
}

SessionsSidebar #session-list {
    background: $panel;
}

SessionsSidebar ListItem {
    background: $panel;
    padding: 0 1;
}

SessionsSidebar ListItem.session-group {
    margin-top: 1;
    color: $border;
    text-style: none;
}

SessionsSidebar ListItem.session-item.-current {
    background: $peri;
}

SessionsSidebar ListItem.session-item.-current Label {
    color: $bg;
}

SessionsSidebar ListItem.new-session-item .session-title {
    color: $peri;
}

SessionsSidebar _SessionRow,
SessionsSidebar _NewSessionRow {
    height: 1;
    width: 1fr;
}

SessionsSidebar .session-title {
    width: 1fr;
    color: $moon;
    text-overflow: ellipsis;
    text-wrap: nowrap;
}

SessionsSidebar .session-time {
    width: auto;
    padding-left: 1;
    color: $moon-dim;
}
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_tui_sidebars.py tests/test_tui_app.py tests/test_tui_chat.py -v`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add luna/tui tests/test_tui_sidebars.py
git commit -m "feat: sessions sidebar with project header, day groups, current highlight and Ctrl+N"
```

---

### Task 7: Claude-Code-style transcript with inline tool rows; remove ActivitySidebar

**Files:**
- Create: `luna/tui/tool_row.py`
- Modify: `luna/tui/chat.py` (message classes, `render_history`, `_LiveReply`, `on_input_submitted`, `_apply_event`)
- Modify: `luna/tui/app.py` (drop the activity sidebar; toggle only the sessions sidebar)
- Modify: `luna/tui/theme.py` (add `ok`, `err`)
- Modify: `luna/tui/luna.tcss` (message and tool-row styles; drop activity rules)
- Delete: `luna/tui/sidebar_activity.py`
- Test: `tests/test_tui_chat.py`, `tests/test_tui_app.py`, `tests/test_tui_sidebars.py`, new `tests/test_tui_tool_row.py`

**Interfaces:**
- Consumes: SSE `tool_started` with `args_preview` (Task 5); history entries with `role == "tool"` (Task 5); `ChatPane.TurnFinished` (Task 6).
- Produces:
  - `ToolRow(name: str, preview: str, *, result: tuple[bool, str] | None = None)` with `async finish(ok: bool, detail: str) -> None`, attribute `is_finished: bool`
  - `render_history(messages: list[dict]) -> list[Widget]`
  - `_LiveReply.tool_started(call_id, name, preview)`, `_LiveReply.tool_finished(call_id, ok, detail)`

- [ ] **Step 1: Write the failing tests**

`tests/test_tui_tool_row.py`:

```python
from textual.app import App, ComposeResult
from textual.widgets import Static

from luna.tui.tool_row import ToolRow
from luna.tui.widgets import PulseGlyph


class _Host(App):
    def __init__(self, row: ToolRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        yield self.row


def _text(row: ToolRow) -> str:
    return "\n".join(str(s.render()) for s in row.query(Static) if not isinstance(s, PulseGlyph))


async def test_running_row_pulses_then_shows_result_and_duration():
    row = ToolRow("web_search", "погода Орёл")
    async with _Host(row).run_test():
        assert len(row.query(PulseGlyph)) == 1
        await row.finish(True, "8 результатов")
        assert row.is_finished
        assert len(row.query(PulseGlyph)) == 0
        text = _text(row)
        assert "● web_search" in text and "«погода Орёл»" in text
        assert "└ 8 результатов · " in text and text.rstrip().endswith("s")


async def test_quiet_success_shows_only_duration_live():
    row = ToolRow("read_file", "/a.py")
    async with _Host(row).run_test():
        await row.finish(True, "")
        assert "└ " in _text(row)


async def test_replayed_row_has_no_duration_and_no_empty_detail_line():
    row = ToolRow("read_file", "/a.py", result=(True, ""))
    async with _Host(row).run_test():
        assert row.is_finished
        assert "└" not in _text(row)


async def test_replayed_failure_shows_detail():
    row = ToolRow("execute", "false", result=(False, "exit 1"))
    async with _Host(row).run_test():
        assert "└ exit 1" in _text(row)
```

In `tests/test_tui_chat.py`:
- Remove `from luna.tui.sidebar_activity import ActivitySidebar` and, in `_HarnessApp.compose`, the comment block plus `yield ActivitySidebar()`.
- In the `render_history` test, replace the two label assertions with:

```python
        assert widgets[0].source.startswith("› ")
        assert "**Luna**" not in widgets[1].source
```

- Change `UserMessage("**You**\n\nhello")` to `UserMessage("› hello")`.
- Append:

```python
class _ToolThenTextClient:
    async def get_history(self, thread_id, workdir):
        return []

    async def send_message(self, thread_id, content, workdir):
        yield {"event": "text_delta", "text": "Сейчас поищу."}
        yield {"event": "tool_started", "call_id": "c1", "name": "web_search", "args": {}, "args_preview": "погода"}
        yield {"event": "tool_finished", "call_id": "c1", "name": "web_search", "ok": True, "detail": "8 результатов"}
        yield {"event": "text_delta", "text": "Завтра +12."}
        yield {"event": "turn_done"}


async def test_tool_row_sits_between_the_text_before_and_after_it():
    from luna.tui.chat import LunaMessage
    from luna.tui.tool_row import ToolRow

    app = _HarnessApp(_ToolThenTextClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "погода?"))
        transcript = chat.query_one("#transcript", VerticalScroll)
        kinds = [
            type(w).__name__
            for w in transcript.children
            if isinstance(w, (UserMessage, LunaMessage, ToolRow))
        ]
        assert kinds == ["UserMessage", "LunaMessage", "ToolRow", "LunaMessage"]
        assert transcript.query_one(ToolRow).is_finished


class _DiesMidToolClient:
    async def get_history(self, thread_id, workdir):
        return []

    async def send_message(self, thread_id, content, workdir):
        yield {"event": "tool_started", "call_id": "c1", "name": "execute", "args": {}, "args_preview": "sleep 99"}
        raise RuntimeError("connection dropped mid-tool")


async def test_a_turn_that_dies_mid_tool_stops_the_pulse():
    from luna.tui.tool_row import ToolRow
    from luna.tui.widgets import PulseGlyph

    app = _HarnessApp(_DiesMidToolClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        try:
            await chat.on_input_submitted(Input.Submitted(inp, "run it"))
        except RuntimeError:
            pass
        row = chat.query_one(ToolRow)
        assert row.is_finished
        assert len(row.query(PulseGlyph)) == 0


def test_render_history_builds_tool_rows():
    from luna.tui.chat import render_history
    from luna.tui.tool_row import ToolRow

    widgets = render_history(
        [
            {"role": "human", "content": "hi"},
            {"role": "tool", "name": "ls", "args_preview": "/", "ok": True, "detail": ""},
            {"role": "ai", "content": "done"},
        ]
    )
    assert isinstance(widgets[1], ToolRow)
```

In `tests/test_tui_sidebars.py` delete `test_activity_sidebar_adds_and_resolves_a_tool_entry` and `test_activity_sidebar_does_not_shadow_widgets_private_render_method`.

In `tests/test_tui_app.py`:
- In `test_app_mounts_three_zones` rename to `test_app_mounts_sidebar_chat_and_status_bar` and replace `assert app.query_one("#activity-sidebar") is not None` with:

```python
        assert not app.query("#activity-sidebar")
```

- Replace `test_activity_sidebar_stays_on_screen_at_a_realistic_terminal_width` with:

```python
async def test_chat_fills_the_width_right_of_the_sessions_sidebar(tmp_path, fake_model):
    """Regression guard for the old `width: 1fr` bug: ChatPane must share the
    row with the sidebar and end exactly at the terminal's right edge."""
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    app_asgi = create_app(agent_factory=lambda _workdir: agent, token="t")
    transport = httpx.ASGITransport(app=app_asgi)

    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id="t1")
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test(size=(120, 40)):
        sidebar = app.query_one("#sessions-sidebar")
        chat = app.query_one(ChatPane)
        assert chat.region.x == sidebar.region.x + sidebar.region.width
        assert chat.region.x + chat.region.width == 120
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui_tool_row.py tests/test_tui_chat.py tests/test_tui_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'luna.tui.tool_row'`

- [ ] **Step 3: Colour tokens in `luna/tui/theme.py`**

Add to `TUI_VARIABLES`:

```python
    #: Tool-call outcome dots. Both clear 4.5:1 against ``bg`` (#0b1026):
    #: pastel tones keep them in the moon/night palette instead of a
    #: saturated traffic-light green/red.
    "ok": "#8fd6a8",
    "err": "#e88b8b",
```

- [ ] **Step 4: Create `luna/tui/tool_row.py`**

```python
"""One tool call inline in the chat transcript, Claude-Code style.

While running it is a twinkling ``PulseGlyph``; once finished it becomes a
coloured ``●`` headline plus a ``└ detail · 1.2s`` line. Replayed history
rows are built finished (``result=``) and have no duration, which is not
stored.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

from luna.tui.theme import TUI_VARIABLES
from luna.tui.widgets import PulseGlyph


class ToolRow(Vertical):
    """A tool call's row: pulsing while running, ``●`` + detail when done."""

    def __init__(self, name: str, preview: str, *, result: tuple[bool, str] | None = None) -> None:
        super().__init__()
        self.tool_name = name
        self.preview = preview
        self._started_at = time.monotonic()
        self._result = result
        self._duration: str | None = None

    @property
    def is_finished(self) -> bool:
        """True once the call has an outcome (live or replayed)."""
        return self._result is not None

    def _label(self) -> str:
        return f"{self.tool_name}  «{self.preview}»" if self.preview else self.tool_name

    def compose(self):
        if self._result is None:
            yield PulseGlyph(self._label())
            return
        ok, detail = self._result
        head = Text()
        head.append("● ", style=TUI_VARIABLES["ok"] if ok else TUI_VARIABLES["err"])
        head.append(self.tool_name, style="bold")
        if self.preview:
            head.append(f"  «{self.preview}»", style=TUI_VARIABLES["moon-dim"])
        yield Static(head, classes="tool-head")
        tail = " · ".join(part for part in (detail, self._duration) if part)
        if tail:
            yield Static(f"  └ {tail}", classes="tool-detail")

    async def finish(self, ok: bool, detail: str) -> None:
        """Record the outcome and duration, then swap the pulse for the result."""
        self._duration = f"{time.monotonic() - self._started_at:.1f}s"
        self._result = (ok, detail)
        await self.recompose()
```

- [ ] **Step 5: Rework the transcript in `luna/tui/chat.py`**

Imports: remove `from luna.tui.sidebar_activity import ActivitySidebar`; add `from textual.widget import Widget` (already imported) and `from luna.tui.tool_row import ToolRow`.

Message class docstrings:

```python
class UserMessage(Markdown):
    """The user's message, prefixed with "› " — no panel, no label."""


class LunaMessage(Markdown):
    """A block of Luna's reply text, indented under the user's message."""


class SystemMessage(Markdown):
    """Local-command output (help text, "not yet in the TUI" notices) — dim, unlabeled."""
```

`render_history`:

```python
def render_history(messages: list[dict]) -> list[Widget]:
    """Build one widget per history entry, ready to mount into the transcript.

    Mirrors the live turn exactly — user line, inline tool rows (finished,
    without durations), Luna's text — so replaying history looks identical
    to having watched it happen live.
    """
    widgets: list[Widget] = []
    for m in messages:
        if m["role"] == "human":
            widgets.append(UserMessage(f"› {m['content']}"))
        elif m["role"] == "tool":
            widgets.append(
                ToolRow(m["name"], m.get("args_preview", ""), result=(m["ok"], m["detail"]))
            )
        else:
            widgets.append(LunaMessage(m["content"]))
    return widgets
```

`_LiveReply` (replace the class):

```python
class _LiveReply:
    """Streams one turn into the transcript: text blocks and inline tool rows.

    Keeps the "thinking" placeholder visible until there is something to
    show. Each tool call closes the current text block, so text the model
    writes after a tool lands *below* that tool's row instead of being
    appended to a block above it. Any tool still running when the turn
    ends (an error, a dropped stream) is marked failed so it stops pulsing.
    """

    def __init__(self, transcript: VerticalScroll, thinking: PulseGlyph) -> None:
        self._transcript = transcript
        self._thinking = thinking
        self._stream: MarkdownStream | None = None
        self._tools: dict[str, ToolRow] = {}

    async def _mount(self, widget: Widget) -> None:
        if self._thinking.is_mounted:
            await self._transcript.mount(widget, before=self._thinking)
        else:
            await self._transcript.mount(widget)
        self._transcript.anchor()

    async def _end_text_block(self) -> None:
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None

    async def write(self, text: str) -> None:
        if self._stream is None:
            if self._thinking.is_mounted:
                await self._thinking.remove()
            response = LunaMessage("")
            await self._mount(response)
            self._stream = Markdown.get_stream(response)
        await self._stream.write(text)

    async def tool_started(self, call_id: str, name: str, preview: str) -> None:
        await self._end_text_block()
        row = ToolRow(name, preview)
        self._tools[call_id] = row
        await self._mount(row)

    async def tool_finished(self, call_id: str, ok: bool, detail: str) -> None:
        row = self._tools.pop(call_id, None)
        if row is not None:
            await row.finish(ok, detail)

    async def stop(self) -> None:
        await self._end_text_block()
        for row in self._tools.values():
            await row.finish(False, "прервано")
        self._tools.clear()
        if self._thinking.is_mounted:
            await self._thinking.remove()
```

In `on_input_submitted`: change `await transcript.mount(UserMessage(f"**You**\n\n{content}"))` to `await transcript.mount(UserMessage(f"› {content}"))`; delete the line `activity = app.query_one(ActivitySidebar)` (and its leading comment about resolving inside the try); change `pending = await self._apply_event(evt, reply, activity, turn_usage)` to `pending = await self._apply_event(evt, reply, turn_usage)`.

In `_apply_event`: signature `(self, evt: dict, reply: _LiveReply, turn_usage: TurnUsage)`, docstring "Apply one SSE event to the transcript/status bar.", and the tool branches:

```python
        elif evt["event"] == "tool_started":
            await reply.tool_started(evt["call_id"], evt["name"], evt.get("args_preview", ""))
        elif evt["event"] == "tool_finished":
            await reply.tool_finished(evt["call_id"], evt["ok"], evt["detail"])
```

- [ ] **Step 6: Drop the activity sidebar from `luna/tui/app.py`**

Remove the `ActivitySidebar` import, the `activity_sidebar = ...`/`.id = ...` lines and `yield activity_sidebar`; update the comment above the sidebar construction to mention only `#sessions-sidebar`; update the `compose` docstring to "Build the layout: sessions sidebar, chat pane, footer.". Replace `action_toggle_panels`:

```python
    def action_toggle_panels(self) -> None:
        """Show/hide the sessions sidebar (Ctrl+B)."""
        sidebar = self.query_one("#sessions-sidebar")
        sidebar.display = not sidebar.display
```

Delete `luna/tui/sidebar_activity.py`: `git rm luna/tui/sidebar_activity.py`. Then `grep -rn "sidebar_activity\|ActivitySidebar" luna tests` must print nothing except the `widgets.py` docstring mention, which you update to "(chat "thinking" placeholder and running tool rows)".

- [ ] **Step 7: Styles in `luna/tui/luna.tcss`**

Delete the `#activity-sidebar { ... }` and `ActivitySidebar PulseGlyph { ... }` rules; in the `ChatPane` rule's comment, replace "two fixed-width sidebar siblings" with "fixed-width sidebar sibling". Replace the `UserMessage`, `LunaMessage`, `SystemMessage` rules with:

```css
UserMessage {
    color: $moon;
    margin: 1 1 1 1;
    padding: 0;
}

LunaMessage {
    color: $moon;
    margin: 0 1 1 3;
    padding: 0;
}

SystemMessage {
    color: $moon-dim;
    margin: 0 1 1 3;
    padding: 0;
}

ToolRow {
    height: auto;
    margin: 0 1 1 3;
}

ToolRow PulseGlyph {
    margin: 0;
}

ToolRow .tool-detail {
    color: $moon-dim;
}
```

(`ToolRow PulseGlyph` is more specific than `ChatPane PulseGlyph`, so the thinking placeholder keeps its own margin while tool rows stay compact.)

- [ ] **Step 8: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check luna tests`
Expected: all tests pass, ruff reports no errors

- [ ] **Step 9: Commit**

```bash
git add -A luna/tui tests
git commit -m "feat: Claude-Code-style transcript with inline tool rows; remove the activity sidebar"
```

---

### Task 8: Cleanup, live check, critique/polish, changelog

**Files:**
- Modify: `CHANGELOG.md` (`## [Unreleased]`)
- One-off: `~/.config/luna/sessions.db` (user data — back it up first)

- [ ] **Step 1: Back up and remove the 3 stray sessions**

```bash
cp ~/.config/luna/sessions.db ~/.config/luna/sessions.db.bak-2026-09-28
sqlite3 ~/.config/luna/sessions.db "
  DELETE FROM checkpoints WHERE thread_id IN (SELECT thread_id FROM luna_sessions WHERE workdir LIKE '/private/var/folders/%');
  DELETE FROM writes      WHERE thread_id IN (SELECT thread_id FROM luna_sessions WHERE workdir LIKE '/private/var/folders/%');
  DELETE FROM luna_sessions WHERE workdir LIKE '/private/var/folders/%';
"
sqlite3 ~/.config/luna/sessions.db "SELECT workdir, COUNT(*) FROM luna_sessions GROUP BY workdir"
```

Expected: only the `Luna_pi` row remains (23 sessions).

- [ ] **Step 2: Restart the server so it runs the new code**

`ensure_running` compares a code fingerprint and restarts a stale server; confirm by launching `uv run luna` in the repo and checking the sidebar shows day groups (a stale server would omit `group` and the TUI would raise `KeyError: 'group'`).

- [ ] **Step 3: Live check in a real pty**

- In `Luna_pi`: no trust prompt (migrated), sidebar shows `◐ Luna_pi`, `~/Desktop/MyProjects/Обучение/Luna_pi` (truncated with `…`), groups `СЕГОДНЯ / ВЧЕРА / …`, current session highlighted.
- Ask "поищи в интернете погоду в Орле": tool rows pulse, then turn into `● web_search «…»` + `└ … · 1.2s`; text after a tool appears below it.
- Reopen that session from the sidebar: tool rows are replayed without durations.
- Ctrl+N: empty transcript, new session appears in `СЕГОДНЯ` after the first message.
- Ctrl+B hides/shows the sidebar.
- `mkdir /tmp/luna-trust-check && cd /tmp/luna-trust-check && uv run --project <repo> luna`: trust prompt appears; "Нет" exits with `Luna не запущена: папка не отмечена как доверенная.` and exit code 1; relaunch → prompt again; "Да" → TUI with an empty sidebar for that folder; Luna_pi sessions are not listed there.

- [ ] **Step 4: Run the `critique` skill, then the `polish` skill on the live screen**

Fix what they find in `luna/tui/luna.tcss` / widgets only (no scope creep; anything larger → ask the user). Re-run `uv run pytest -q`.

- [ ] **Step 5: Changelog**

Under `## [Unreleased]` in `CHANGELOG.md` add:

```markdown
### Added
- Projects: each exact launch directory is stored in `luna_projects` (`sessions.db`); folders that already had sessions are trusted automatically.
- Trust prompt before the TUI starts in an unknown folder ("Доверяете этой папке?"); refusing exits with code 1. `-p` and piped input never prompt.
- The local server rejects requests for untrusted folders (`403 workdir_not_trusted`).
- TUI: sessions grouped by day (Сегодня / Вчера / На этой неделе / Ранее), project header, "+ новая сессия" and Ctrl+N.
- TUI: tool calls inline in the chat (`● name «args»` + `└ detail · 1.2s`), also when reopening a session.

### Changed
- TUI layout is now sessions sidebar + chat; the activity column is gone.
- Session times use Russian month/weekday names instead of the locale's (`Sep`).
- The session list shows up to 200 sessions instead of 20.
```

- [ ] **Step 6: Commit**

```bash
git add CHANGELOG.md luna/tui
git commit -m "docs: changelog for projects, trust gate and the new TUI layout"
```
