# Luna: projects, workdir trust, and the TUI layout redesign

Date: 2026-09-28
Status: approved in conversation, awaiting written-spec review

## Goal

1. Make "project" a first-class, persisted entity keyed by the exact launch
   directory, gated by a Claude-Code-style trust prompt, so the future
   web/desktop client (Hermes-like) can list projects and their sessions
   from the same store.
2. Remove the 3 stray sessions left in the user's real `sessions.db` by
   manual runs in `tempfile` directories.
3. Redesign the TUI: layout A (sessions sidebar + chat) without the right
   activity column, a Claude-Code-style chat transcript (variant B), and a
   session list grouped by day (variant 1). Mockups:
   https://claude.ai/artifact/XX9Ahuh9EXbVRdiWseiW7t

## Current state (verified)

- `luna_sessions(thread_id, workdir, created, updated, title)` in
  `~/.config/luna/sessions.db`; `workdir` is `Path.resolve()`d and
  `/sessions?workdir=` already filters by it. Per-project separation works;
  the screenshot's sessions were all from `Luna_pi`.
- The DB contains 3 sessions whose `workdir` is
  `/private/var/folders/.../T/tmp*` (created 2026-09-25/26, titles "say hi",
  "привет"). They are **not** a test leak — `tests/conftest.py` has
  isolated `XDG_CONFIG_HOME` via the autouse `isolated_config_home` fixture
  since 2026-09-06, and pytest dirs are named `pytest-of-*`. They come from
  manual runs of the real `luna` in `tempfile.mkdtemp()` dirs.
- A session row is only written on the first message (`turns.py`
  `post_message`, `session.py`), so there are no empty sessions.
- `SessionIndex.list` defaults to `limit=20`.
- `get_history` drops tool-call messages; tool activity is only visible
  live, in `ActivitySidebar`.
- `ActivitySidebar` shows only in-flight tool calls (a `PulseGlyph` per
  pending call).

## Part 1 — projects and storage

### Data model

New table in the same `sessions.db`, created by `SessionIndex.__init__`
alongside `luna_sessions`:

```sql
CREATE TABLE IF NOT EXISTS luna_projects (
    path        TEXT PRIMARY KEY,   -- Path(workdir).resolve(), exact launch dir
    trusted_at  REAL,               -- NULL = not trusted
    last_opened REAL NOT NULL
)
```

`luna_sessions.workdir` keeps its meaning and values; it refers to
`luna_projects.path` by convention (no FK enforcement — SQLite FKs are off
by default and the index is best-effort by design).

New module `luna/core/projects.py` with a `ProjectIndex` sharing the
`SessionIndex` degrade-to-no-op behaviour:

- `is_trusted(path) -> bool`
- `trust(path) -> None` — upsert with `trusted_at = now`, `last_opened = now`
- `touch(path) -> None` — bump `last_opened`
- `list() -> list[ProjectRow]` — newest `last_opened` first (for the future
  desktop client; the TUI does not use it yet)

Migration (idempotent, runs on open): every distinct `workdir` in
`luna_sessions` that is missing from `luna_projects` and still exists on
disk is inserted as trusted (`trusted_at = created of its oldest session`).
Existing users are never re-prompted for folders they already worked in.

If the index is unavailable (DB cannot open), `is_trusted` returns `False`
and `trust` is a no-op; the prompt still runs, and accepting lets the
session proceed for this launch only.

### Trust prompt

In `luna/cli.py`, on the TUI launch path only (`interactive and not
prompt` — `interactive` always leads to the TUI, so it is the only
interactive mode), before `run_tui` / `ensure_running`:

```
 Доверяете этой папке?

 /Users/pavelgolubinec/Desktop/MyProjects/Обучение/Luna_pi

 Luna сможет читать и изменять файлы в этой папке и запускать в ней
 команды. Открывайте только проекты, которым доверяете.

 › 1. Да, доверяю
   2. Нет, выйти

 Enter — подтвердить · Esc — выйти
```

- Rendered with the existing Rich/`luna.ui` console helpers used by the
  interactive prompts (`2026-09-14-luna-interactive-prompts-design.md`), not
  inside Textual, so it runs before the server or TUI start.
- "Да" → `ProjectIndex.trust(path)`, continue.
- "Нет", Esc, Ctrl+C → print one line ("Luna не запущена: папка не отмечена
  как доверенная."), exit code 1, nothing written to the DB.
- Trusted folder → no prompt, `ProjectIndex.touch(path)`.
- Non-interactive `luna -p "..."` and the piped-stdin REPL → no prompt, no
  trust record (a scripted invocation is the consent, as with Claude
  Code's `-p`).
- If `arrow_pick` reports no real terminal (returns `None`), fall back to a
  plain `input()` "[y/N]" question; anything but `y`/`д` means "Нет".

### Server enforcement

`create_app` gains `trust_check: Callable[[str], bool] | None = None`,
stored on `app.state`. `luna serve` (`run_serve`) passes
`ProjectIndex().is_trusted`; `None` (tests, embedding) disables the check,
so the 23 existing `create_app(...)` call sites in tests keep working. When
set, every route that takes a `workdir` — `/sessions` (GET/POST) and all
`/sessions/{thread_id}/*` routes — answers `400 {"error":
"workdir_required"}` if it is missing and `403 {"error":
"workdir_not_trusted", "workdir": ...}` if untrusted. One helper,
`luna/server/trust.py: trust_error(request, workdir) -> JSONResponse |
None`, is used by every handler. This keeps
a future web/desktop client from bypassing the CLI prompt; such a client
will implement its own trust UI and call a `POST /projects/trust` endpoint
added then, not now.

### Cleanup

- One-off cleanup (a documented manual step in the plan, not code): delete
  the 3 `luna_sessions` rows whose `workdir` starts with `/private/var/folders/`
  and their checkpoints (`checkpoints`/`writes` rows with those `thread_id`s).

### Session listing API

`GET /sessions?workdir=` response gains `group` per session and a higher
default limit:

- `SessionIndex.list(..., limit=200)`.
- `group`: one of `"today" | "yesterday" | "week" | "older"`, computed
  server-side from `updated` in local time (calendar days, not 24h windows):
  today; yesterday; within the last 7 calendar days; everything earlier.
- `relative_time` rules change to match the grouping:
  today → `сейчас` / `11м` / `3ч`; yesterday and week → `HH:MM` for
  yesterday, `пн`/`вт`/... for the rest of the week; older → `25 сен`
  (Russian month abbreviations, not `%b`, which is locale-dependent and
  currently renders `Sep`).

Group labels are rendered by the client (`Сегодня`, `Вчера`,
`На этой неделе`, `Ранее`), so the API stays language-neutral.

## Part 2 — TUI layout

### Layout

```
┌ sessions sidebar (30) ┬ chat (1fr) ─────────────────────────────┐
│ ◐ Luna_pi             │                                         │
│ ~/Desktop/MyProjects… │ › поищи в интернете погоду в Орле       │
│ + новая сессия     ^n │                                         │
│                       │   ● web_search «погода Орёл 29 сент…»   │
│ СЕГОДНЯ               │     └ 8 результатов · 1.2s              │
│▌поищи в интернете  11м│                                         │
│ какая погода в о…  21ч│   Завтра в Орле облачно, днём +11…      │
│ ВЧЕРА                 │                                         │
│ напиши какое ядро 14:02│ ╭──────────────────────────────────╮   │
│ …                     │ │ ›                                 │   │
│                       │ ╰──────────────────────────────────╯   │
│                       │ claude-sonnet-5 · ctx … · $0.03 · …     │
└───────────────────────┴─────────────────────────────────────────┘
 ^b панель  ^n новая сессия  ^c выход
```

- `ActivitySidebar` (`luna/tui/sidebar_activity.py`) and its CSS, tests and
  `app.query_one(ActivitySidebar)` usages are removed.
- `action_toggle_panels` (Ctrl+B) toggles only `#sessions-sidebar`.
- New binding Ctrl+N → new session (creates a thread via
  `client.create_session`, clears the transcript, focuses the input).
- Sidebar width 24 → 30.

### Sessions sidebar (variant 1)

- Header: `◐ <folder name>` (bold, `$moon`), then the path with `$HOME`
  shortened to `~`, ellipsis-truncated from the left, `$border` colour.
- `+ новая сессия` row (clickable, `$peri`) with `^n` right-aligned.
- Groups in API order; empty groups are not rendered. Group header:
  uppercase, `$border` colour, one blank row above (none above the first).
- Session row: one line, title ellipsis-truncated, `relative_time`
  right-aligned in `$moon-dim`.
- Selected row (the thread open in the chat): `$peri` background, `$bg`
  text. Hover: `$panel` lightened.
- Group headers are not focusable/selectable; keyboard ↑/↓ skips them.
- The list refreshes after each finished turn (so a new session appears
  and `updated` re-sorts it).

### Chat transcript (variant B)

- User message: `› ` in `$peri` bold, then the text. No background, no
  border, no "You" label.
- Tool call row, mounted on `tool_started`:
  `<PulseGlyph> name  <short args>` while running; on `tool_finished` the
  glyph becomes a static `●` (`$ok` green if `ok`, red otherwise) and a
  second line `  └ <detail> · <duration>` appears in `$moon-dim`.
  Short args: the first string argument, quoted, truncated to 40 chars.
- Luna reply: Markdown, indented 2 columns, no background, no border, no
  "Luna" label. Tool rows and reply text belong to the same indented block.
- System messages (help, errors): `$moon-dim` text, indented, no plate.
- `LunaBanner` stays as the first transcript item.
- New colour tokens in `luna/tui/theme.py`: `ok` (`#8fd6a8`) and `err`
  (`#e88b8b`), both checked for ≥4.5:1 against `bg`.

### History with tool calls

`get_history` returns tool calls in order, in addition to human/ai text:

```json
{"role": "tool", "name": "web_search", "args_preview": "погода Орёл…",
 "ok": true, "detail": "8 результатов"}
```

Built from the AI messages' `tool_calls` plus the matching `ToolMessage`s.
The `ok`/`detail` derivation currently lives inline in
`luna/core/turn_events.py` (`iter_turn`'s ToolMessage branch: first line of
the body, max 120 chars, blank on success for `_QUIET_ON_SUCCESS` tools).
It is extracted into `tool_outcome(message: ToolMessage) -> tuple[bool, str]`
in the same module and used by both `iter_turn` and `get_history`, so live
and replayed rows cannot drift. `args_preview` is produced by one shared
helper used by both the live row and history. Durations are not stored, so
replayed tool rows omit `· <duration>`. `render_history` renders these
exactly like the live rows.

When `detail` is empty (quiet successful tools), the `└` line shows only
the duration live, and is omitted in replayed history.

## Testing

- `ProjectIndex`: trust/is_trusted/touch/list; migration marks existing
  workdirs trusted and is idempotent; degrades to no-op on DB failure.
- CLI trust flow: prompt shown for an untrusted dir; "Нет" exits 1 and
  writes nothing; "Да" records trust; trusted dir skips prompt; `-p`
  skips prompt; the `input()` fallback accepts only `y`/`д`.
- Server: 403 `workdir_not_trusted` on each route for an untrusted dir,
  400 without `workdir`, no check when `trust_check=None`, and `run_serve`
  wires the real check.
- Grouping and `relative_time`: fixed `now` at day boundaries (23:59 /
  00:01), yesterday, 6 and 8 days back, Russian month names.
- `get_history`: returns tool entries between human/ai messages, with
  `ok=False` for an errored ToolMessage.
- TUI (Textual pilot): no `ActivitySidebar`; sidebar renders group headers
  and skips empty groups; selected row class follows the open thread;
  Ctrl+N starts a new session; tool row transitions from pulse to `●` +
  `└` line; history replay renders tool rows.
- Before showing the finished screen: `critique` and `polish` skills on the
  real app in a pty.

## Out of scope

- Search / Ctrl+K session overlay (variant B's overlay).
- Renaming or deleting sessions from the sidebar.
- A projects list in the TUI and `POST /projects/trust` (future desktop).
- Revoking trust (`luna trust --revoke`) — can be added later on top of
  `luna_projects`.
