# Luna package restructure — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the flat 28-file `luna/` package into 5 subpackages
(`core`, `repl`, `config`, `turn`, `extensions`) grouped by responsibility,
with zero behavior change, then bring `AGENTS.md` up to date with both this
restructure and the 0.3.0 feature set it already lacks.

**Architecture:** Pure `git mv` + import-path rewrite, one subpackage per
task, in dependency order (leaves first, `core` last since `core/session.py`
is the only module that imports `repl/commands.py` at module level). Every
task ends with a green `pytest`/`ruff` run — a missed import is an immediate
`ImportError` at collection time, never a silent gap.

**Tech Stack:** Python 3.11+, pytest, ruff, hatchling (wheel packaging via
`packages = ["luna"]`, which auto-includes subpackages with no config change).

**Spec:** `docs/superpowers/specs/2026-09-12-luna-package-restructure-design.md`

## Global Constraints

- Zero behavior change. No test's assertions or fixtures change — only import
  statements (in `luna/*.py` and `tests/test_*.py`) and two non-import strings
  (a resource-loader path, two `__import__(...)` string literals).
- `luna/ui/` is not moved. `luna/cli.py`, `luna/__init__.py`, `luna/__main__.py`
  stay at the top level.
- Every new subpackage gets an empty `__init__.py` (no re-exports — every
  import stays an explicit `from luna.<pkg>.<module> import <name>`, never
  `from luna.<pkg> import <name>` relying on `__init__.py` magic, except
  where a file inside the destination package imports a *sibling* file in the
  same new package — those use `from luna.<pkg> import <module>` per Python
  convention for same-package imports, matching the existing codebase's style
  for grouped imports (e.g. `from luna import mcp, skills` today).
- `git mv` (not delete+recreate) for every file move — preserves git history.
- After every task: `uv run pytest -q` must show the same 271 total / 270
  passed / 1 skipped as before the task, and `uv run ruff check luna tests`
  must pass. `uv run ruff format luna tests` may reformat import blocks
  (line wrapping, ordering) — run it and include the result in the commit,
  don't hand-format.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Branch: stay on `main` (this repo's established pattern per its own
  history — small, verifiable, sequential commits directly on `main`, not a
  feature branch, since each task is independently green and this isn't
  shipping new user-facing behavior). If your executing skill's default is a
  feature branch, that's fine too — either way, never work directly with
  uncommitted changes spanning more than one task.

## Reference: every file's new dotted path

Use this table for every task below. It is complete — every `luna/*.py` file
and `models.toml` is listed exactly once.

| Old (flat) | New (dotted path) |
| --- | --- |
| `luna/config.py` | `luna.config.config` |
| `luna/credentials.py` | `luna.config.credentials` |
| `luna/providers.py` | `luna.config.providers` |
| `luna/prompts.py` | `luna.config.prompts` |
| `luna/usage.py` | `luna.config.usage` |
| `luna/models.toml` | `luna/config/models.toml` (data file, no dotted path) |
| `luna/context.py` | `luna.turn.context` |
| `luna/memory.py` | `luna.turn.memory` |
| `luna/undo.py` | `luna.turn.undo` |
| `luna/gitinfo.py` | `luna.turn.gitinfo` |
| `luna/fmt.py` | `luna.turn.fmt` |
| `luna/diagnose.py` | `luna.turn.diagnose` |
| `luna/verify.py` | `luna.turn.verify` |
| `luna/subagents.py` | `luna.extensions.subagents` |
| `luna/extension_tools.py` | `luna.extensions.extension_tools` |
| `luna/mcp.py` | `luna.extensions.mcp` |
| `luna/skills.py` | `luna.extensions.skills` |
| `luna/registry.py` | `luna.extensions.registry` |
| `luna/lspnav.py` | `luna.extensions.lspnav` |
| `luna/initgen.py` | `luna.extensions.initgen` |
| `luna/commands.py` | `luna.repl.commands` |
| `luna/setup_wizard.py` | `luna.repl.setup_wizard` |
| `luna/usercmd.py` | `luna.repl.usercmd` |
| `luna/agent.py` | `luna.core.agent` |
| `luna/session.py` | `luna.core.session` |
| `luna/persistence.py` | `luna.core.persistence` |
| `luna/toolguard.py` | `luna.core.toolguard` |
| `luna/permissions.py` | `luna.core.permissions` |

**The mechanical rule every task applies:** for each file the task moves,
find every occurrence of `from luna.<oldmodule> import <names>` (any name
list) or `from luna import <oldmodule>` (bare, possibly grouped with other
names on the same line) anywhere in `luna/` or `tests/`, and rewrite the
*module path only* — `<names>` (or the aliasing `as x`) never change. Grouped
lines that mix modules going to *different* new packages must be split (each
task calls these out explicitly where they occur — don't guess, use the
exact rewrite given). After editing, `grep -rn "from luna\.<oldmodule> import\|from luna import [^#]*\b<oldmodule>\b" luna tests` (repeat per module this task moved) must return zero hits.

---

### Task 1: `luna/config/`

**Files:**
- Create `luna/config/` (a new directory — no name conflict with the
  existing `luna/config.py` file, which keeps its own name and just moves
  inside the new directory) and `git mv` `config.py`, `credentials.py`,
  `providers.py`, `prompts.py`, `usage.py`, `models.toml` into it. Exact
  commands in Step 1.
- Test: no new test files; existing tests get import-path edits only (listed in Step 3).

**Interfaces:**
- Consumes: nothing new.
- Produces: `luna.config.config`, `luna.config.credentials`, `luna.config.providers`, `luna.config.prompts`, `luna.config.usage` — same public names as today, just at these new paths. `luna/config/models.toml` — same file, new location.

- [ ] **Step 1: Move the files**

```bash
mkdir -p luna/config
touch luna/config/__init__.py
git mv luna/config.py luna/config/config.py
git mv luna/credentials.py luna/config/credentials.py
git mv luna/providers.py luna/config/providers.py
git mv luna/prompts.py luna/config/prompts.py
git mv luna/usage.py luna/config/usage.py
git mv luna/models.toml luna/config/models.toml
git add luna/config/__init__.py
```

- [ ] **Step 2: Fix the one non-import reference — the packaged-resource path**

In `luna/config/usage.py`, find:
```python
        text = resources.files("luna").joinpath("models.toml").read_text()
```
Change to:
```python
        text = resources.files("luna.config").joinpath("models.toml").read_text()
```
(`importlib.resources.files("luna.config")` resolves to the `luna/config/`
package directory on disk regardless of install layout — this is the only
place in the codebase that hardcodes a dotted package name as a string
rather than as a real import, so it needs a manual fix; nothing else in this
task does.)

- [ ] **Step 3: Fix every import of the 5 moved modules**

Within the moved files themselves (same-package, since all 5 land in
`luna/config/` together):
- `luna/config/config.py:15`: `from luna.providers import DEFAULT_PROVIDER, PROVIDERS, LunaConfigError` → `from luna.config.providers import DEFAULT_PROVIDER, PROVIDERS, LunaConfigError`
- `luna/config/credentials.py:15`: `from luna.config import config_dir` → `from luna.config.config import config_dir`
- `luna/config/providers.py:70` (inside a function, lazy): `from luna.credentials import apply_stored_key` → `from luna.config.credentials import apply_stored_key`

Everywhere else in `luna/` (these files are NOT moving in this task — only
edit the specific import lines shown, nothing else in them):
- `luna/persistence.py:18`: `from luna.config import config_dir` → `from luna.config.config import config_dir`
- `luna/permissions.py:11`: `from luna.config import config_dir` → `from luna.config.config import config_dir`
- `luna/usercmd.py:11`: `from luna.config import config_dir` → `from luna.config.config import config_dir`
- `luna/subagents.py:12-13`: `from luna.config import config_dir` → `from luna.config.config import config_dir`; `from luna.providers import LunaConfigError` → `from luna.config.providers import LunaConfigError`
- `luna/skills.py:15-16`: same two-line pattern as `subagents.py` above
- `luna/registry.py:9-10`: same two-line pattern
- `luna/mcp.py:11-12`: same two-line pattern
- `luna/extension_tools.py:11`: `from luna.providers import LunaConfigError` → `from luna.config.providers import LunaConfigError`
- `luna/setup_wizard.py:10-12`:
  ```python
  from luna.config import set_config_values
  from luna.credentials import get_api_key, mask_key, set_api_key
  from luna.providers import DEFAULT_PROVIDER, PROVIDERS
  ```
  →
  ```python
  from luna.config.config import set_config_values
  from luna.config.credentials import get_api_key, mask_key, set_api_key
  from luna.config.providers import DEFAULT_PROVIDER, PROVIDERS
  ```
- `luna/commands.py:18-20`:
  ```python
  from luna.config import LunaConfig
  from luna.credentials import get_api_key
  from luna.providers import PROVIDERS, LunaConfigError
  ```
  →
  ```python
  from luna.config.config import LunaConfig
  from luna.config.credentials import get_api_key
  from luna.config.providers import PROVIDERS, LunaConfigError
  ```
- `luna/session.py:29`: `from luna.config import LunaConfig` → `from luna.config.config import LunaConfig`
- `luna/session.py:37`: `from luna.usage import SessionUsage, TurnUsage, indicator_line, price` → `from luna.config.usage import SessionUsage, TurnUsage, indicator_line, price`
- `luna/agent.py:21`: `from luna.config import LunaConfig` → `from luna.config.config import LunaConfig`
- `luna/agent.py:25`: `from luna.prompts import LUNA_SYSTEM_PROMPT` → `from luna.config.prompts import LUNA_SYSTEM_PROMPT`
- `luna/agent.py:26`: `from luna.providers import build_model` → `from luna.config.providers import build_model`
- `luna/cli.py:12`: `from luna import __version__, mcp, skills` → split, keep only the `__version__` part for this task: `from luna import __version__` (the `mcp, skills` part is fixed in Task 3 — leave a `from luna import mcp, skills` line for now if you split it, Task 3 will change it again; simplest is to leave this whole line untouched in this task and let Task 3 do the full split, since `__version__` doesn't need `luna.config` at all — **do nothing to this line in Task 1**)
- `luna/cli.py:14`: `from luna.config import config_path, load_config, set_config_values` → `from luna.config.config import config_path, load_config, set_config_values`
- `luna/cli.py:15-21`:
  ```python
  from luna.credentials import (
      credentials_path,
      get_api_key,
      mask_key,
      set_api_key,
      unset_api_key,
  )
  ```
  → same names, `from luna.config.credentials import (`
- `luna/cli.py:23`: `from luna.providers import PROVIDERS, LunaConfigError` → `from luna.config.providers import PROVIDERS, LunaConfigError`

Tests — every occurrence of `from luna.config import <names>` becomes
`from luna.config.config import <names>` (names unchanged), in:
`tests/test_repl_flow.py`, `tests/test_agent.py` (2 occurrences, one inline
inside a test function), `tests/test_verify.py`, `tests/test_usercmd.py`
(inline), `tests/test_config.py` (4 occurrences), `tests/test_session.py`,
`tests/test_undo.py` (14 occurrences, all inline inside test functions —
same rewrite every time), `tests/test_persistence.py` (2 occurrences),
`tests/test_setup_wizard.py`, `tests/test_end_to_end.py`,
`tests/test_commands.py`, `tests/test_permissions.py` (inline),
`tests/test_cli.py` (inline).

Every occurrence of `from luna.credentials import <names>` becomes
`from luna.config.credentials import <names>`, in: `tests/test_credentials.py`,
`tests/test_setup_wizard.py` (2 occurrences).

Every occurrence of `from luna.providers import <names>` becomes
`from luna.config.providers import <names>`, in: `tests/test_registry.py`,
`tests/test_providers.py`, `tests/test_credentials.py`, `tests/test_subagents.py`,
`tests/test_skills.py`.

Every occurrence of `from luna.prompts import <names>` becomes
`from luna.config.prompts import <names>`, in: `tests/test_agent.py`.

Every occurrence of `from luna.usage import <names>` becomes
`from luna.config.usage import <names>`, in: `tests/test_verify.py`,
`tests/test_usage.py` (module docstring `:mod:\`luna.usage\`` at the top of
the file also becomes `:mod:\`luna.config.usage\``; 5 more `from luna.usage
import` occurrences below it, all inline inside test functions),
`tests/test_session_reload.py`.

- [ ] **Step 4: Run the checks**

```bash
uv run pytest -q
uv run ruff check luna tests
uv run ruff format luna tests
```
Expected: 271 total (270 passed, 1 skipped), same as before this task. If
anything fails to import, `grep -rn "from luna\.\(config\|credentials\|providers\|prompts\|usage\) import\|from luna import [^#]*\b\(config\|credentials\|providers\|prompts\|usage\)\b" luna tests` and check every hit is either already fixed (`luna.config.config`, etc.) or the one deliberately-untouched `luna/cli.py:12` line.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: move config/credentials/providers/prompts/usage into luna/config/"
```

---

### Task 2: `luna/turn/`

**Files:**
- `git mv` `context.py`, `memory.py`, `undo.py`, `gitinfo.py`, `fmt.py`, `diagnose.py`, `verify.py` into `luna/turn/`.
- Test: import-path edits only.

**Interfaces:**
- Consumes: nothing new (this task doesn't depend on Task 1's new paths — none of these 7 files import `config`/`credentials`/`providers`/`prompts`/`usage`).
- Produces: `luna.turn.context`, `luna.turn.memory`, `luna.turn.undo`, `luna.turn.gitinfo`, `luna.turn.fmt`, `luna.turn.diagnose`, `luna.turn.verify`.

- [ ] **Step 1: Move the files**

```bash
mkdir -p luna/turn
touch luna/turn/__init__.py
git mv luna/context.py luna/turn/context.py
git mv luna/memory.py luna/turn/memory.py
git mv luna/undo.py luna/turn/undo.py
git mv luna/gitinfo.py luna/turn/gitinfo.py
git mv luna/fmt.py luna/turn/fmt.py
git mv luna/diagnose.py luna/turn/diagnose.py
git mv luna/verify.py luna/turn/verify.py
git add luna/turn/__init__.py
```

- [ ] **Step 2: Fix every import of the 7 moved modules**

Within the moved files (same-package, `undo.py` imports its sibling `gitinfo.py`):
- `luna/turn/undo.py:21`: `from luna import gitinfo` → `from luna.turn import gitinfo`

Everywhere else in `luna/` (edit only the lines shown):
- `luna/toolguard.py:8`: `from luna import gitinfo` → `from luna.turn import gitinfo`
- `luna/toolguard.py:10`: `from luna.undo import snapshot` → `from luna.turn.undo import snapshot`
- `luna/session.py:26`: `from luna import diagnose, fmt, gitinfo, permissions, undo, usercmd` — this line spans THREE destination packages (this task's `turn`, plus `core`'s `permissions` and `repl`'s `usercmd`, neither moved yet). For this task, rewrite it to:
  ```python
  from luna import permissions, usercmd
  from luna.turn import diagnose, fmt, gitinfo, undo
  ```
  (the `permissions, usercmd` line stays in the old flat form for now — Task 4 will move `usercmd` out of it and Task 5 will move `permissions` out of it; this task's job is only to carve out the `turn`-bound names).
- `luna/session.py:30`: `from luna.context import PinnedFiles, expand_mentions, render_pinned` → `from luna.turn.context import PinnedFiles, expand_mentions, render_pinned`
- `luna/session.py:38`: `from luna.verify import run_verify` → `from luna.turn.verify import run_verify`
- `luna/agent.py:23`: `from luna.memory import memory_files` → `from luna.turn.memory import memory_files`
- `luna/extension_tools.py:10`: `from luna.memory import append_note` → `from luna.turn.memory import append_note`
- `luna/commands.py:23`: `from luna.undo import peek_last, session_diff, undo_last` → `from luna.turn.undo import peek_last, session_diff, undo_last`
- `luna/commands.py:24`: `from luna.verify import run_verify` → `from luna.turn.verify import run_verify`
- `luna/commands.py:330` (inside `_undo`, lazy): `from luna.undo import undo as git_undo` → `from luna.turn.undo import undo as git_undo`
- `luna/commands.py:369` (inside `_redo`, lazy): `from luna.undo import redo as git_redo` → `from luna.turn.undo import redo as git_redo`
- `luna/commands.py:455` (inside the unknown-command fallthrough, lazy): `from luna.usercmd import expand` — **leave untouched**, `usercmd` moves in Task 4, not this one.
- `luna/cli.py`: find the lazy `from luna.gitinfo import dirty_paths` (inside `main()`) → `from luna.turn.gitinfo import dirty_paths`; find the lazy `from luna import undo` (inside `main()`, before `undo.gc(...)`) → `from luna.turn import undo`

Tests — every occurrence of `from luna.undo import <names>` becomes
`from luna.turn.undo import <names>` (names unchanged), in:
`tests/test_undo.py` (this file has ~30 such lines, every one inline inside
a different test function — same rewrite every time, including the ones
importing private names like `_snapshot_tree`, and the one importing
`forget_messages`), `tests/test_commands.py` (3 occurrences, inline).

Every occurrence of `from luna.gitinfo import <names>` becomes
`from luna.turn.gitinfo import <names>`, in: `tests/test_gitinfo.py`.

Every occurrence of `luna.fmt` (both `from luna.fmt import <names>` and
`import luna.fmt as fmt_module`) becomes `luna.turn.fmt`, in: `tests/test_fmt.py`
(5 `from` occurrences), `tests/test_repl_flow.py` (2 occurrences of
`import luna.fmt as fmt_module`, inline).

Every occurrence of `from luna.diagnose import <names>` becomes
`from luna.turn.diagnose import <names>`, in: `tests/test_diagnose.py` (5 occurrences).

Every occurrence of `from luna.context import <names>` becomes
`from luna.turn.context import <names>`, in: `tests/test_context.py`,
`tests/test_commands.py` (1 inline occurrence).

Every occurrence of `from luna.memory import <names>` becomes
`from luna.turn.memory import <names>`, in: `tests/test_memory.py`.

Every occurrence of `from luna.verify import <names>` becomes
`from luna.turn.verify import <names>`, in: `tests/test_verify.py` (2 occurrences,
one aliased `as real_run_verify` — keep the alias, only the module path changes).

- [ ] **Step 3: Run the checks**

```bash
uv run pytest -q
uv run ruff check luna tests
uv run ruff format luna tests
```
Expected: same 271/270/1 as always. `grep -rn "from luna\.\(context\|memory\|undo\|gitinfo\|fmt\|diagnose\|verify\) import\|from luna import [^#]*\b\(context\|memory\|undo\|gitinfo\|fmt\|diagnose\|verify\)\b" luna tests` should show zero unfixed hits (the `permissions, usercmd` line in `session.py` and the `luna.usercmd import expand` line in `commands.py` are expected survivors — not this task's job).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move context/memory/undo/gitinfo/fmt/diagnose/verify into luna/turn/"
```

---

### Task 3: `luna/extensions/`

**Files:**
- `git mv` `subagents.py`, `extension_tools.py`, `mcp.py`, `skills.py`, `registry.py`, `lspnav.py`, `initgen.py` into `luna/extensions/`.
- Test: import-path edits only.

**Interfaces:**
- Consumes: `luna.config.providers.LunaConfigError`, `luna.config.config.config_dir` (from Task 1 — already at their new paths by now).
- Produces: `luna.extensions.subagents`, `luna.extensions.extension_tools`, `luna.extensions.mcp`, `luna.extensions.skills`, `luna.extensions.registry`, `luna.extensions.lspnav`, `luna.extensions.initgen`.

- [ ] **Step 1: Move the files**

```bash
mkdir -p luna/extensions
touch luna/extensions/__init__.py
git mv luna/subagents.py luna/extensions/subagents.py
git mv luna/extension_tools.py luna/extensions/extension_tools.py
git mv luna/mcp.py luna/extensions/mcp.py
git mv luna/skills.py luna/extensions/skills.py
git mv luna/registry.py luna/extensions/registry.py
git mv luna/lspnav.py luna/extensions/lspnav.py
git mv luna/initgen.py luna/extensions/initgen.py
git add luna/extensions/__init__.py
```

- [ ] **Step 2: Fix every import of the 7 moved modules**

Within the moved files (same-package):
- `luna/extensions/extension_tools.py:9`: `from luna import mcp, skills` → `from luna.extensions import mcp, skills`
- `luna/extensions/extension_tools.py:12`: `from luna.registry import known_mcp, known_skills, resolve_mcp` → `from luna.extensions.registry import known_mcp, known_skills, resolve_mcp`
- `luna/extensions/skills.py:17`: `from luna.registry import resolve_skill` → `from luna.extensions.registry import resolve_skill`

(These 3 files already had Task 1's `config`/`providers` lines fixed to
`luna.config.*` in Task 1 — don't touch those again here, only the
`extensions`-internal cross-references above.)

Everywhere else in `luna/` (edit only the lines shown):
- `luna/session.py:33`: `from luna.subagents import subagent_summaries` → `from luna.extensions.subagents import subagent_summaries`
- `luna/agent.py:17-20`:
  ```python
  from luna import lspnav
  from luna import mcp as mcp_mod
  from luna import skills as skills_mod
  from luna import subagents as subagents_mod
  ```
  →
  ```python
  from luna.extensions import lspnav
  from luna.extensions import mcp as mcp_mod
  from luna.extensions import skills as skills_mod
  from luna.extensions import subagents as subagents_mod
  ```
- `luna/agent.py:22`: `from luna.extension_tools import EXTENSION_INTERRUPTS, EXTENSION_TOOLS` → `from luna.extensions.extension_tools import EXTENSION_INTERRUPTS, EXTENSION_TOOLS`
- `luna/commands.py:21`: `from luna.subagents import subagent_summaries` → `from luna.extensions.subagents import subagent_summaries`
- `luna/commands.py:379` (inside `_init`, lazy): `from luna.initgen import init_prompt` → `from luna.extensions.initgen import init_prompt`
- `luna/cli.py:12`: `from luna import __version__, mcp, skills` → split into two lines:
  ```python
  from luna import __version__
  from luna.extensions import mcp, skills
  ```
- `luna/cli.py:22`: `from luna.initgen import init_prompt` → `from luna.extensions.initgen import init_prompt`
- `luna/cli.py:24`: `from luna.registry import known_mcp, known_skills, resolve_mcp` → `from luna.extensions.registry import known_mcp, known_skills, resolve_mcp`
- `luna/cli.py:27`: `from luna.subagents import subagent_summaries` → `from luna.extensions.subagents import subagent_summaries`

Tests — every occurrence of `from luna.subagents import <names>` becomes
`from luna.extensions.subagents import <names>`, in: `tests/test_agent.py`
(inline), `tests/test_subagents.py`.

Every occurrence of `from luna.extension_tools import <names>` becomes
`from luna.extensions.extension_tools import <names>`, in: `tests/test_extension_tools.py`.

Every occurrence of `luna.mcp` (`from luna.mcp import <names>` and
`import luna.mcp as m`) becomes `luna.extensions.mcp`, in:
`tests/test_mcp_config.py` (2 occurrences), `tests/test_extension_tools.py`,
`tests/test_cli.py` (inline).

Every occurrence of `from luna.skills import <names>` becomes
`from luna.extensions.skills import <names>`, in: `tests/test_skills.py`.

Every occurrence of `from luna.registry import <names>` becomes
`from luna.extensions.registry import <names>`, in: `tests/test_registry.py`.

Every occurrence of `luna.lspnav` (`from luna.lspnav import <names>`,
`import luna.lspnav as lspnav`, and the string literal
`__import__("luna.lspnav", fromlist=["available"])`) becomes
`luna.extensions.lspnav`, in: `tests/test_lspnav.py` (6 occurrences total,
including the `__import__` string form — rewrite that one to
`__import__("luna.extensions.lspnav", fromlist=["available"])`).

Every occurrence of `from luna.initgen import <names>` becomes
`from luna.extensions.initgen import <names>`, in: `tests/test_initgen.py`
(also fix the `__import__("luna.agent", fromlist=["build_agent"])` string
literal in this same file on **its own line 25** — **do not touch it in this
task**, `agent.py` moves in Task 5, this note is just so you don't miss it
later).

- [ ] **Step 3: Run the checks**

```bash
uv run pytest -q
uv run ruff check luna tests
uv run ruff format luna tests
```
Expected: same 271/270/1. `grep -rn "from luna\.\(subagents\|extension_tools\|mcp\|skills\|registry\|lspnav\|initgen\) import\|from luna import [^#]*\b\(mcp\|skills\)\b\|__import__(\"luna\.lspnav" luna tests` should show zero unfixed hits.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move subagents/extension_tools/mcp/skills/registry/lspnav/initgen into luna/extensions/"
```

---

### Task 4: `luna/repl/`

**Files:**
- `git mv` `commands.py`, `setup_wizard.py`, `usercmd.py` into `luna/repl/`.
- Test: import-path edits only.

**Interfaces:**
- Consumes: `luna.config.config.LunaConfig`, `luna.config.credentials.get_api_key`,
  `luna.config.providers.{PROVIDERS,LunaConfigError,DEFAULT_PROVIDER}`,
  `luna.config.config.{config_dir,set_config_values}`,
  `luna.turn.undo.{peek_last,session_diff,undo_last,undo,redo}`,
  `luna.turn.verify.run_verify`, `luna.extensions.subagents.subagent_summaries`,
  `luna.extensions.initgen.init_prompt` (all from Tasks 1-3, already at their
  new paths by now).
- Produces: `luna.repl.commands`, `luna.repl.setup_wizard`, `luna.repl.usercmd`.
  `core/session.py` (Task 5) will depend on `luna.repl.commands`.

- [ ] **Step 1: Move the files**

```bash
mkdir -p luna/repl
touch luna/repl/__init__.py
git mv luna/commands.py luna/repl/commands.py
git mv luna/setup_wizard.py luna/repl/setup_wizard.py
git mv luna/usercmd.py luna/repl/usercmd.py
git add luna/repl/__init__.py
```

- [ ] **Step 2: Fix every import**

`luna/repl/commands.py` currently has (after Tasks 1-3 already rewrote most
of its lines):
```python
from luna.config.config import LunaConfig
from luna.config.credentials import get_api_key
from luna.config.providers import PROVIDERS, LunaConfigError
from luna.extensions.subagents import subagent_summaries
from luna.ui.theme import PALETTE
from luna.turn.undo import peek_last, session_diff, undo_last
from luna.turn.verify import run_verify
```
These are all already correct (they point at real files, nothing in this
task's move affects them) — **do not edit them**, just confirm they still
look like this before moving on. The two lazy imports inside
`luna/repl/commands.py` also need no change in this task:
`from luna.session import compact_thread` (line ~260) and
`from luna.session import run_once` (line ~380) still correctly point at
`luna.session`, because `session.py` hasn't moved yet — Task 5 will update
these two lines when it moves `session.py`. The lazy
`from luna.initgen import init_prompt` (now `from luna.extensions.initgen
import init_prompt` after Task 3) and lazy `from luna.turn.undo import undo
as git_undo` / `from luna.turn.undo import redo as git_redo` (after Task 2)
are also already correct — confirm, don't re-edit.
`luna/repl/usercmd.py`'s own `from luna.config.config import config_dir`
(rewritten in Task 1) needs no change either.

`luna/repl/setup_wizard.py` similarly already has its 3 import lines pointed
at `luna.config.config`, `luna.config.credentials`, `luna.config.providers`
from Task 1 — confirm, don't re-edit.

Everywhere else in `luna/` (edit only the lines shown):
- `luna/session.py:27-28`:
  ```python
  from luna.commands import HELP as SLASH_COMMANDS
  from luna.commands import CommandContext, dispatch
  ```
  →
  ```python
  from luna.repl.commands import HELP as SLASH_COMMANDS
  from luna.repl.commands import CommandContext, dispatch
  ```
- `luna/session.py`'s carve-out line from Task 2, currently
  `from luna import permissions, usercmd` → split:
  ```python
  from luna import permissions
  from luna.repl import usercmd
  ```
  (`permissions` stays in the old flat form — Task 5 moves it.)
- `luna/cli.py:26`: `from luna.setup_wizard import run_setup` → `from luna.repl.setup_wizard import run_setup`

- [ ] **Step 3: Run the checks**

```bash
uv run pytest -q
uv run ruff check luna tests
uv run ruff format luna tests
```
Expected: same 271/270/1. `grep -rn "from luna\.\(commands\|setup_wizard\|usercmd\) import\|from luna import [^#]*\busercmd\b" luna tests` should show zero unfixed hits except the `from luna import permissions` line in `session.py` (expected survivor, Task 5's job).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move commands/setup_wizard/usercmd into luna/repl/"
```

---

### Task 5: `luna/core/`

**Files:**
- `git mv` `agent.py`, `session.py`, `persistence.py`, `toolguard.py`, `permissions.py` into `luna/core/`.
- Modify: `luna/ui/approve.py` (imports `permissions`, which moves in this task — `luna/ui/` itself does not move).
- Test: import-path edits only.

**Interfaces:**
- Consumes: everything from Tasks 1-4 (`luna.config.*`, `luna.turn.*`,
  `luna.extensions.*`, `luna.repl.*`) — all already at their final paths.
- Produces: `luna.core.agent`, `luna.core.session`, `luna.core.persistence`,
  `luna.core.toolguard`, `luna.core.permissions`. This is the last subpackage
  move — after this task, no file in `luna/` (other than `cli.py`,
  `__init__.py`, `__main__.py`) is at its old flat path.

- [ ] **Step 1: Move the files**

```bash
mkdir -p luna/core
touch luna/core/__init__.py
git mv luna/agent.py luna/core/agent.py
git mv luna/session.py luna/core/session.py
git mv luna/persistence.py luna/core/persistence.py
git mv luna/toolguard.py luna/core/toolguard.py
git mv luna/permissions.py luna/core/permissions.py
git add luna/core/__init__.py
```

- [ ] **Step 2: Fix every import of the 5 moved modules**

Within the moved files (same-package):
- `luna/core/agent.py:24`: `from luna.permissions import load_rules` → `from luna.core.permissions import load_rules`
- `luna/core/agent.py:27`: `from luna.toolguard import tool_guard` → `from luna.core.toolguard import tool_guard`
- `luna/core/session.py`'s carve-out line from Task 4, currently
  `from luna import permissions` → since `permissions.py` now lives in the
  same package as `session.py` (`luna/core/`), rewrite to:
  ```python
  from luna.core import permissions
  ```
- `luna/core/session.py:27-28` (from Task 4): `from luna.repl.commands import HELP as SLASH_COMMANDS` / `from luna.repl.commands import CommandContext, dispatch` — already correct, no change.
- `luna/core/session.py:31`: `from luna.permissions import load_rules` → `from luna.core.permissions import load_rules`
- `luna/core/session.py:32`: `from luna.persistence import SessionIndex, make_title` → `from luna.core.persistence import SessionIndex, make_title`
- `luna/core/toolguard.py:9`: `from luna.permissions import RuleSet` → `from luna.core.permissions import RuleSet`

Everywhere else in `luna/` (edit only the lines shown):
- `luna/ui/approve.py:14`: `from luna.permissions import suggest_rule` → `from luna.core.permissions import suggest_rule`
- `luna/repl/commands.py` (the two lazy imports left untouched in Task 4):
  `from luna.session import compact_thread` → `from luna.core.session import compact_thread`;
  `from luna.session import run_once` → `from luna.core.session import run_once`
- `luna/cli.py:13`: `from luna.agent import build_agent` → `from luna.core.agent import build_agent`
- `luna/cli.py:25`: `from luna.session import run_once, run_repl` → `from luna.core.session import run_once, run_repl`
- `luna/cli.py`'s lazy `from luna.persistence import SessionIndex, checkpointer` (inside `main()`) → `from luna.core.persistence import SessionIndex, checkpointer`

- [ ] **Step 3: Fix the three docstring/prose mentions of old dotted paths**

These don't affect imports or tests, but they'd be actively misleading
documentation after this move — fix them as part of this task since it's the
one that makes them stale:

- `luna/core/agent.py:3-5`, currently:
  ```python
  """Assemble the Luna deep agent from a resolved config.

  All ``deepagents`` / ``langgraph`` / ``langchain_mcp_adapters`` imports are
  confined to this module and ``luna.session``.
  """
  ```
  → change `` ``luna.session`` `` to `` ``luna.core.session`` ``.
- `luna/core/session.py:1-5`, currently:
  ```python
  """Streaming REPL / one-shot session loop with approval handling.

  Together with :mod:`luna.agent` this is the only module that touches
  ``deepagents`` / ``langgraph`` directly.
  """
  ```
  → change `:mod:\`luna.agent\`` to `:mod:\`luna.core.agent\``.
- `luna/repl/commands.py:1-7`, currently:
  ```python
  """REPL slash-command dispatch table.

  ``run_repl`` in :mod:`luna.session` delegates every ``/command`` line to
  :func:`dispatch`. Each handler takes ``(ctx, arg)`` and either returns a
  :class:`DispatchResult` or ``None`` (treated as handled/no-op). Later tasks
  register additional handlers by adding an entry to ``_TABLE``.
  """
  ```
  → change `:mod:\`luna.session\`` to `:mod:\`luna.core.session\``.

- [ ] **Step 4: Fix the two remaining `__import__` string literals**

- `tests/test_initgen.py:25`: `__import__("luna.agent", fromlist=["build_agent"])` → `__import__("luna.core.agent", fromlist=["build_agent"])`
- `tests/test_cli.py:154`: same pattern → `__import__("luna.core.agent", fromlist=["build_agent"])`

- [ ] **Step 5: Fix every remaining test import of the 5 moved modules**

Every occurrence of `from luna.agent import <names>` becomes
`from luna.core.agent import <names>`, in: `tests/test_agent.py` (2
occurrences, one inline), `tests/test_session.py`, `tests/test_repl_flow.py`,
`tests/test_persistence.py` (2 occurrences), `tests/test_end_to_end.py`,
`tests/test_undo.py` (14 occurrences, all inline), `tests/test_commands.py`
(3 occurrences, inline), `tests/test_permissions.py` (inline).

Every occurrence of `from luna.session import <names>` becomes
`from luna.core.session import <names>`, in: `tests/test_session.py` (2
occurrences), `tests/test_repl_flow.py`, `tests/test_persistence.py`,
`tests/test_end_to_end.py`, `tests/test_undo.py` (2 occurrences, inline),
`tests/test_session_reload.py`.

Every occurrence of `from luna.persistence import <names>` becomes
`from luna.core.persistence import <names>`, in: `tests/test_repl_flow.py`,
`tests/test_persistence.py`, `tests/test_commands.py` (3 occurrences,
inline), `tests/test_resume.py` (2 occurrences).

Every occurrence of `from luna.toolguard import <names>` becomes
`from luna.core.toolguard import <names>`, in: `tests/test_agent.py` (inline).

Every occurrence of `from luna.permissions import <names>` becomes
`from luna.core.permissions import <names>`, in: `tests/test_agent.py`
(inline), `tests/test_permissions.py`.

- [ ] **Step 6: Run the checks**

```bash
uv run pytest -q
uv run ruff check luna tests
uv run ruff format luna tests
```
Expected: same 271/270/1. `grep -rn "from luna\.\(agent\|session\|persistence\|toolguard\|permissions\) import\|from luna import [^#]*\bpermissions\b\|__import__(\"luna\.agent" luna tests` should show zero hits anywhere in the repo now — this is the task that closes out every remaining old-path reference except `cli.py`'s own module identity (which never moves).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: move agent/session/persistence/toolguard/permissions into luna/core/"
```

---

### Task 6: Documentation + final cleanup

**Files:**
- Modify: `AGENTS.md`, `CHANGELOG.md`.
- No code changes (Tasks 1-5 already completed the restructure).

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new — this task verifies and documents.

- [ ] **Step 1: Rewrite `AGENTS.md`'s "Структура" section**

Read the current section first — it still lists all 28 files flat (one line
each, from `luna/config.py` through `luna/cli.py`, per the file's own current
content). Replace it with a per-subpackage listing, same one-line-per-file
level of detail, grouped under each new subpackage heading. Use this content
(verify each description still matches the file's actual current
docstring/purpose before pasting — files may have gained features since
these one-liners were last written; don't blindly copy without checking):

```markdown
## Структура

- `luna/cli.py` — точка входа на argparse (`setup`/`config`/`mcp`/`skills`/`agents`/`init`)
- `luna/core/` — рантайм агента и его защита
  - `agent.py` — сборка `create_deep_agent` (вызовы фреймворка живут здесь)
  - `session.py` — потоковый REPL / режим одного запроса, подтверждения, `/reload`, `compact_thread`
  - `persistence.py` — SqliteSaver + индекс сессий
  - `toolguard.py` — middleware: deny-правила + снапшоты + `/plan`
  - `permissions.py` — правила allow/deny
- `luna/repl/` — интерактивный слой поверх core
  - `commands.py` — диспетчер slash-команд
  - `setup_wizard.py` — интерактивный мастер `luna setup`
  - `usercmd.py` — пользовательские slash-команды из `.luna/commands/*.md`
- `luna/config/` — настройки, ключи, провайдеры, цены
  - `config.py` — слоистое разрешение настроек в `LunaConfig`; запись `config.toml`
  - `credentials.py` — API-ключи в `~/.config/luna/credentials.toml` (права 0600)
  - `providers.py` — реестр провайдеров → chat-модель LangChain
  - `prompts.py` — системный промпт Luna
  - `usage.py` — учёт токенов, `$`-стоимость (`models.toml`)
  - `models.toml` — реестр моделей: окно контекста, цена input/output
- `luna/turn/` — всё, что крутится вокруг одного хода
  - `context.py` — `@file` + закреплённые файлы
  - `memory.py` — `.luna/memory/*.md`
  - `undo.py` — журнал снапшотов, `/diff` `/undo` `/redo` (в git — снапшоты
    дерева + разговора через `git commit-tree`, вне git — файловый журнал)
  - `gitinfo.py` — проверка git-дерева
  - `fmt.py` — автоформатирование тронутых файлов после правок
  - `diagnose.py` — диагностика после правок, `/diagnose`
  - `verify.py` — verify-команда
- `luna/extensions/` — подключаемые возможности
  - `subagents.py` — встроенные субагенты + из `subagents.toml`
  - `extension_tools.py` — инструменты агента `manage_mcp` / `manage_skills`
  - `mcp.py` — чтение/трансляция `mcp.json`; обнаружение MCP-инструментов
  - `skills.py` — установка/список/удаление скилов в стиле Anthropic
  - `registry.py` — курируемый реестр MCP-серверов / скилов (+ `registry.toml`)
  - `lspnav.py` — LSP-навигация (`goto_definition` / `find_references` /
    `hover`), extra `luna-simple[lsp]`
  - `initgen.py` — `luna init` / `/init`
- `luna/ui/` — тема `rich`, заставка, консоль, диалог подтверждения, оформление реплик
```

- [ ] **Step 2: Update `AGENTS.md`'s "Соглашения" section for the new paths**

Current text (verify live, this is exact):
```markdown
- Все импорты `deepagents` / `langgraph` держать внутри `luna/agent.py`,
  `luna/session.py`, `luna/persistence.py` и `luna/toolguard.py`. Известные
  исключения: `luna/subagents.py` держит модульные импорты `SubAgent` /
  `FilesystemMiddleware` из `deepagents` (нужны для сборки декларативных
  субагентов); `luna/undo.py` — `undo()`/`redo()` принимают уже собранного
  агента параметром и лениво импортируют `langchain_core.messages` только
  внутри этих двух функций, остальной модуль framework-free.
- ID моделей — в `luna/providers.py` или конфиге, никогда в логике агента.
```
Rewrite both bullets' paths (substance of each rule stays identical, only
paths change):
```markdown
- Все импорты `deepagents` / `langgraph` держать внутри `luna/core/agent.py`,
  `luna/core/session.py`, `luna/core/persistence.py` и
  `luna/core/toolguard.py`. Известные исключения:
  `luna/extensions/subagents.py` держит модульные импорты `SubAgent` /
  `FilesystemMiddleware` из `deepagents` (нужны для сборки декларативных
  субагентов); `luna/turn/undo.py` — `undo()`/`redo()` принимают уже
  собранного агента параметром и лениво импортируют
  `langchain_core.messages` только внутри этих двух функций, остальной
  модуль framework-free.
- ID моделей — в `luna/config/providers.py` или конфиге, никогда в логике агента.
```

- [ ] **Step 3: Add a `CHANGELOG.md` entry**

Add a new `## [Unreleased]` section above the `## [0.3.0]` entry (check the
file's current top for the exact existing format/heading style — match it),
`### Изменено`, one bullet:

```markdown
## [Unreleased]

### Изменено

- Пакет `luna/` реорганизован из плоского списка в 28 файлов в 5 подпакетов
  по смыслу — `core/` (рантайм агента), `repl/` (интерактивный слой),
  `config/` (настройки/ключи/провайдеры/цены), `turn/` (всё вокруг одного
  хода — контекст, память, undo, форматирование, диагностика, verify),
  `extensions/` (подключаемые возможности). Поведение не изменилось, только
  расположение файлов и пути импорта.
```

- [ ] **Step 4: Clean up stale `__pycache__` directories**

```bash
find luna -name __pycache__ -type d -exec rm -rf {} +
```
(Old `.pyc` files under the previous flat locations are now orphaned —
harmless to pytest/ruff since Python regenerates `__pycache__` fresh per
directory, but leaving stale ones around is just clutter this whole task
exists to remove.)

- [ ] **Step 5: Verify the package still builds correctly**

```bash
uv build --wheel
python3 -c "
import zipfile, glob
wheel = sorted(glob.glob('dist/*.whl'))[-1]
with zipfile.ZipFile(wheel) as z:
    names = z.namelist()
    for pkg in ('core', 'repl', 'config', 'turn', 'extensions', 'ui'):
        matches = [n for n in names if n.startswith(f'luna/{pkg}/')]
        print(pkg, '->', len(matches), 'files')
        assert matches, f'{pkg} missing from wheel!'
    assert any(n == 'luna/config/models.toml' for n in names), 'models.toml missing!'
print('OK — all subpackages present in the wheel')
"
rm -rf dist/ build/ *.egg-info
```
Expected: every subpackage prints a nonzero file count, `models.toml` is
present, script prints `OK`. This is the practical proof that hatchling's
`packages = ["luna"]` auto-includes the new subpackages with no
`pyproject.toml` change — if this fails, something about the package
structure is wrong and must be fixed before proceeding (do not edit
`pyproject.toml` to "fix" this without first confirming the actual cause —
report what the assertion error says).

- [ ] **Step 6: Final full verification**

```bash
uv run pytest -q
uv run ruff check luna tests
uv run ruff format --check .
git status --porcelain
```
Expected: 271 total (270 passed, 1 skipped), both ruff commands clean, and
`git status` shows only the files this task itself modified (`AGENTS.md`,
`CHANGELOG.md`) plus the `__pycache__` removal (which should be a no-op for
git status, since `__pycache__` is gitignored) — no unexpected leftover diffs
from Tasks 1-5 (those are already committed).

- [ ] **Step 7: Commit**

```bash
git add AGENTS.md CHANGELOG.md
git commit -m "docs: update AGENTS.md structure section and add changelog entry for the luna/ package restructure"
```

---

## Self-Review

**Spec coverage:** every item in the spec's "Целевая структура" section maps
to Tasks 1-5 (one task per new subpackage, in the dependency order the spec's
"Риски" section derived from the real import graph — verified by `grep`, not
assumed). The spec's "Документация" section maps to Task 6. The spec's
"Критерии готовности" map directly to each task's Step-N verification block
and Task 6's Steps 5-6 (wheel build, final full check).

**Placeholder scan:** every import rewrite in Tasks 1-5 gives the exact
before/after line or an exact "apply this exact prefix substitution
everywhere it occurs" rule plus the exhaustive list of files it occurs in
(verified by `grep` against the actual repo while writing this plan, not
guessed) — no "handle similarly" or "add appropriate imports" language
anywhere. `AGENTS.md`'s new "Структура" section content is given verbatim in
Task 6, not described.

**Type/name consistency:** the "Reference: every file's new dotted path"
table is the single source of truth every task's Interfaces section and
import-rewrite instructions draw from — no task invents a different path for
the same file.

**Scope check:** one cohesive refactor (no behavior change), six tasks, each
independently testable and green. Task ordering is a hard dependency chain
(1,2,3 independent leaves → 4 depends on 1-3 → 5 depends on 1-4 → 6 depends
on 1-5 being complete), so tasks must land in order — no parallel dispatch.

## Execution notes

- Tasks 1, 2, 3 are mutually independent (verified: none of the files they
  move import each other, or import anything from `core`/`repl` candidates)
  and could in principle run in any relative order — the plan sequences them
  1→2→3 for a readable history, not because of a technical constraint.
- Task 4 must land after 1, 2, 3. Task 5 must land after 4. Task 6 must land
  after 5. This chain IS a hard technical constraint (see Global Constraints
  and each task's Interfaces section) — never dispatch these out of order or
  in parallel.
- Every task's "Run the checks" step is the real gate: if `pytest` fails to
  collect (an `ImportError` at collection time is the single most likely
  failure mode for this kind of refactor), the fix is always "find the
  remaining old-path import the traceback names and rewrite it" — the
  per-task grep commands given are the fastest way to confirm nothing was
  missed before moving to the next task.
