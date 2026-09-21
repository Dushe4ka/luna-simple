# Luna Skill-Persistence — Design

## Purpose

Roadmap item #3 (last in the agreed 1→2→4→3 order): let the agent save a
successful, reusable pattern it just executed as a local Luna skill —
Hermes-style — instead of that knowledge evaporating at the end of the
session. The next session (or the next `/reload`) can then draw on it.

## Existing Foundation (checked directly, not assumed)

- `luna/extensions/skills.py` already defines the storage convention: a
  skill is a directory containing `SKILL.md` with YAML frontmatter
  (`name`, `description`) plus a markdown body, stored under
  `skills_dirs(workdir)` — `~/.config/luna/skills/<name>/` (user scope) or
  `<workdir>/.luna/skills/<name>/` (project scope). `install()` currently
  only populates this from an external git source; `list_skills()` and
  `remove()` already work generically against whatever is on disk,
  regardless of how it got there.
- `luna/extensions/extension_tools.py` already has two precedents for
  exactly this shape of feature: `manage_skills` (installs a *third-party*
  skill, approval-gated via `EXTENSION_INTERRUPTS`) and `remember` (writes
  a note to `.luna/memory/*.md`, also approval-gated). A self-authored
  skill deserves at least the same scrutiny as installing someone else's —
  its content becomes part of a future session's system context.
- `luna/config/prompts.py`'s `LUNA_SYSTEM_PROMPT` already has a line
  instructing the agent to call `remember` on load-bearing decisions —
  this is the existing pattern for "the agent decides when to call a
  persistence tool," which this feature follows rather than inventing a
  new automatic/heuristic trigger.
- `tests/test_extension_tools.py::test_interrupts_registered` asserts
  `EXTENSION_INTERRUPTS` equals an exact dict literal — adding a new
  approval-gated tool means this test's expected literal must be updated
  in the same task that adds the tool, not left to drift.

## Success Criteria

- The agent has a tool to author a new local skill from a pattern it just
  used successfully — no new storage format, no new activation mechanism:
  it writes into the exact same `SKILL.md`/`skills_dirs()` convention
  `install()`/`list_skills()`/`remove()` already understand, so it shows
  up in `/skills` and activates on the next `/reload`, with zero changes
  needed to the REPL's existing skills commands.
- Defaults to project scope (`.luna/skills/`) — a self-authored pattern is
  almost always specific to the repo it was learned in, unlike
  `manage_skills`'s user-scope default for installing general-purpose
  third-party skills. The agent may pass `project=False` to save to user
  scope for a genuinely repo-independent pattern.
- Approval-gated, exactly like `manage_skills` and `remember` — the
  content is written to disk (and later loaded into a future session's
  context) only after the human confirms.
- The skill name is validated as a safe filesystem slug before any write
  happens — this tool, unlike `install()`, takes an agent-chosen name
  directly as a tool argument with no external SKILL.md/git-clone step in
  between, so path-traversal or otherwise unsafe names must be rejected
  up front rather than relying on downstream directory operations to fail
  safely.
- The system prompt nudges the agent to consider saving a skill after
  successfully completing a nontrivial, reusable multi-step pattern — the
  "auto" part of "Hermes-style auto-save" is the agent's own judgment
  call to invoke the tool; the human approval gate is what keeps the
  actual write deliberate, consistent with every other mutating capability
  in Luna.

## Architecture

### `luna/extensions/skills.py` (modified — one new function)

```python
def save(
    name: str,
    description: str,
    body: str,
    *,
    project: bool = True,
    workdir: str = ".",
    env: Mapping[str, str] | None = None,
) -> Path:
    """Write a new local skill's SKILL.md and return its path."""
```

Behavior:
1. Validate `name` against a safe-slug pattern (letters, digits,
   `-`/`_` only, non-empty) — reject anything else with
   `LunaConfigError` (matching this module's existing error-reporting
   convention, e.g. `install()`'s `LunaConfigError` raises).
2. Validate `description` is non-empty (an empty description would
   silently produce a useless `/skills` listing entry and would fail
   `install()`'s own frontmatter check if this skill were ever
   re-installed elsewhere — keep the two paths' minimum bar consistent).
3. Compute `dest = skills_dirs(workdir, env=env)[1 if project else 0] /
   name`, create it (`mkdir(parents=True, exist_ok=True)`), and write
   `dest / "SKILL.md"` with YAML frontmatter (`name`, `description`)
   followed by `body` as the markdown content — same shape `install()`
   already writes when it copies a cloned skill's `SKILL.md` (frontmatter
   block delimited by `---` lines, matching `_frontmatter()`'s existing
   parser so `list_skills()` reads it back correctly).
4. Return the written path.

### `luna/extensions/extension_tools.py` (modified — one new tool)

```python
@tool
def save_skill(
    name: str,
    description: str,
    body: str,
    project: bool = True,
) -> str:
    """Save a successful, reusable pattern as a new local Luna skill.

    Use this after completing a nontrivial multi-step task you expect to
    repeat — `body` should be the actual reusable instructions/steps, not
    a narrative of what just happened. Defaults to project scope
    (.luna/skills/); pass project=False only for a pattern that is not
    specific to this repository. Activates after the user runs /reload.
    """
```

Calls `skills.save(...)`, catches `LunaConfigError` and returns its
message (matching `manage_mcp`/`manage_skills`'s existing
try/except-and-return-string convention rather than letting the tool
raise). Added to `EXTENSION_TOOLS` and to `EXTENSION_INTERRUPTS` (
`"save_skill": True`).

### `luna/config/prompts.py` (modified — one line added)

Alongside the existing `remember`-nudge line, one new sentence: after
completing a nontrivial, reusable pattern, consider `save_skill`.

## Data Flow

```
agent completes a multi-step task it judges reusable
  → save_skill(name="fix-flaky-ci-retry", description="...", body="...")
      → (yolo=False) human approval prompt, same as remember/manage_skills
      → skills.save(...) validates name, writes .luna/skills/<name>/SKILL.md
  → agent tells the user to run /reload to activate it
  → next /reload: skill_dirs picked up by build_agent → the new skill's
    SKILL.md is available to the model like any installed skill
  → /skills (or manage_skills(action="list")) shows it alongside
    externally-installed skills — no special-casing needed anywhere else
```

## Error Handling

`skills.save()` raises `LunaConfigError` (this module's existing error
type) for an unsafe name or empty description — never writes partial
state before validating. `save_skill` the tool catches that and returns
the message as a string (never raises out of a tool call), matching
`manage_mcp`/`manage_skills`'s existing convention exactly.

## Testing

Extends `tests/test_skills.py`'s convention (real filesystem via
`tmp_path`, `XDG_CONFIG_HOME` monkeypatched, no mocking of `skills.py`
internals) and `tests/test_extension_tools.py`'s convention (`.invoke({...})`
against the real `@tool`-decorated function). Covers: a valid save writes
a correctly-formed `SKILL.md` that `list_skills()` then reports; project
vs. user scope both write to the right directory; an unsafe name
(`../escape`, empty string, a name containing `/`) is rejected without
writing anything; an empty description is rejected; the saved skill
round-trips through `list_skills()` exactly like an `install()`-ed one
(proving no special-casing was needed); `EXTENSION_INTERRUPTS` includes
`save_skill` (updating the existing exact-dict-equality test in the same
task that adds the entry).

## Out of Scope

- Any fully-automatic background heuristic that decides on its own,
  without a human approval step, to persist a skill — this would be a
  materially larger trust/security surface (arbitrary agent-authored
  markdown becoming future context with no human in the loop) and was not
  requested; the approval gate is the deliberate boundary.
- Editing or versioning an existing self-authored skill in place — `save`
  always creates/overwrites at `name`; updating one is exactly `save`
  again with the same name (matching `install()`'s existing
  overwrite-on-reinstall behavior via `shutil.rmtree(dest,
  ignore_errors=True)` before `copytree`).
- Any new REPL command — `/skills list`/`manage_skills(action="list")`
  already show self-authored skills once written; no new surface needed.
