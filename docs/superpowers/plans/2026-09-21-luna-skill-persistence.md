# Luna Skill-Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the agent save a successful, reusable pattern as a new local
Luna skill (Hermes-style), reusing the existing `SKILL.md`/`skills_dirs()`
storage convention exactly — no new storage format, no new activation
mechanism, approval-gated like every other mutating extension tool.

**Architecture:** One new function `skills.save(...)` writing a
`SKILL.md` in the same shape `install()` already produces; one new
approval-gated tool `save_skill` in `extension_tools.py`, following the
exact precedent of `manage_skills`/`remember`; one line added to the
system prompt nudging the agent to use it after a nontrivial reusable
success.

**Tech Stack:** Python 3.11+ stdlib only — no new dependency. Tests use
`tests/test_skills.py`'s real-filesystem convention (`tmp_path`,
`XDG_CONFIG_HOME` monkeypatched) and `tests/test_extension_tools.py`'s
`.invoke({...})` convention against the real `@tool` function.

**Spec:** `docs/superpowers/specs/2026-09-21-luna-skill-persistence-design.md`

## Global Constraints

- Python 3.11+, PEP 8 / PEP 257, clean `uv run ruff check .` /
  `uv run ruff format --check .`. D401 imperative-mood docstrings.
- `skills.save()` raises `LunaConfigError` (already imported in
  `skills.py` from `luna.config.providers`) for any validation failure —
  never partially writes before validating name/description.
- `save_skill` the tool never raises — catches `LunaConfigError` and
  returns its message as a string, matching `manage_mcp`/
  `manage_skills`'s existing convention in the same file.
- `save_skill` MUST be added to `EXTENSION_INTERRUPTS` (approval-gated) —
  this also means `tests/test_extension_tools.py::test_interrupts_registered`,
  which currently asserts `EXTENSION_INTERRUPTS` equals an exact 3-key
  dict literal, must be updated in the same task that adds the tool, or
  the pre-existing test will fail.
- Default scope is `project=True` (writes to `.luna/skills/`, not the
  user-global skills dir) — the opposite default from `manage_skills`,
  deliberately, per the spec's Success Criteria.
- No new REPL command, no changes to `luna/repl/commands.py` — `/skills`
  and `manage_skills(action="list")` already read from `skills_dirs()`
  generically and need no changes to show a self-authored skill.

---

### Task 1: `skills.save()` + tests

**Files:**
- Modify: `luna/extensions/skills.py`
- Test: `tests/test_skills.py` (extend the existing file)

**Interfaces:**
- Produces: `luna.extensions.skills.save(name, description, body, *,
  project=True, workdir=".", env=None) -> Path`. Task 2 imports and calls
  this exactly, catching `LunaConfigError`.
- Consumes: nothing new — reuses this module's existing `skills_dirs()`
  and the `LunaConfigError` already imported at the top of the file.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_skills.py`:

```python
from luna.extensions.skills import save


def test_save_writes_a_skill_that_list_skills_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    path = save(
        "fix-flaky-retry",
        "retries a flaky step with backoff",
        "1. Detect the flaky step.\n2. Wrap it with a 3-attempt retry.\n",
        workdir=str(tmp_path),
    )
    assert path.is_file()
    assert path == tmp_path / ".luna" / "skills" / "fix-flaky-retry" / "SKILL.md"
    assert ("project", "fix-flaky-retry", "retries a flaky step with backoff") in list_skills(
        str(tmp_path)
    )


def test_save_user_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    path = save("greet", "say hi", "Say hi.\n", project=False, workdir=str(tmp_path))
    assert path == tmp_path / ".config" / "luna" / "skills" / "greet" / "SKILL.md"


def test_save_rejects_unsafe_names(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    for bad in ("../escape", "a/b", "", "  ", "a b"):
        with pytest.raises(LunaConfigError):
            save(bad, "desc", "body", workdir=str(tmp_path))
    assert not (tmp_path / ".luna" / "skills").exists()


def test_save_rejects_empty_or_multiline_description(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    with pytest.raises(LunaConfigError):
        save("x", "", "body", workdir=str(tmp_path))
    with pytest.raises(LunaConfigError):
        save("x", "line one\nline two", "body", workdir=str(tmp_path))


def test_save_overwrites_an_existing_same_named_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    save("x", "first version", "first body", workdir=str(tmp_path))
    save("x", "second version", "second body", workdir=str(tmp_path))
    entries = list_skills(str(tmp_path))
    assert [e for e in entries if e[1] == "x"] == [("project", "x", "second version")]
```

`pytest` and `LunaConfigError` are already imported at the top of
`tests/test_skills.py`... **verify this against the actual current file
before assuming it** — if `pytest` is not already imported there, add
`import pytest` at the top alongside the existing imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_skills.py -v -k save`
Expected: FAIL — `ImportError: cannot import name 'save'`.

- [ ] **Step 3: Implement `save()` in `luna/extensions/skills.py`**

Add after the existing `install()` function (or wherever fits the file's
existing organization — `install`/`remove`/`list_skills` are the natural
neighbors):

```python
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


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
    if not _SAFE_NAME.match(name):
        raise LunaConfigError(
            f"{name!r} is not a valid skill name (letters, digits, - and _ only)."
        )
    if not description.strip():
        raise LunaConfigError("description must not be empty.")
    if "\n" in description:
        raise LunaConfigError("description must be a single line.")

    dest = skills_dirs(workdir, env=env)[1 if project else 0] / name
    dest.mkdir(parents=True, exist_ok=True)
    skill_md = dest / "SKILL.md"
    skill_md.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}")
    return skill_md
```

Add `import re` to the top of the file alongside the existing imports
(`shutil`, `subprocess`, `tempfile`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_skills.py -v`
Expected: all pass, including every pre-existing test in the file.

- [ ] **Step 5: Lint**

Run: `uv run ruff check luna/extensions/skills.py tests/test_skills.py && uv run ruff format --check luna/extensions/skills.py tests/test_skills.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add luna/extensions/skills.py tests/test_skills.py
git commit -m "feat: skills.save() writes a self-authored local skill"
```

---

### Task 2: `save_skill` tool + system prompt nudge + tests + docs

**Files:**
- Modify: `luna/extensions/extension_tools.py`
- Modify: `luna/config/prompts.py`
- Modify: `AGENTS.md` (wherever it documents `EXTENSION_TOOLS` /
  `manage_skills` / `remember` — check the actual current text first)
- Modify: `CHANGELOG.md`
- Test: `tests/test_extension_tools.py` (extend the existing file)

**Interfaces:**
- Consumes: Task 1's `luna.extensions.skills.save(...)` and the
  already-imported `LunaConfigError`.
- Produces: nothing further downstream (terminal task).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_extension_tools.py` (and update the existing
`test_interrupts_registered`, which currently asserts an exact 3-key
dict — this is a required edit, not optional, or the suite will fail):

```python
def test_interrupts_registered():
    assert EXTENSION_INTERRUPTS == {
        "manage_mcp": True,
        "manage_skills": True,
        "remember": True,
        "save_skill": True,
    }


def test_save_skill_writes_and_lists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    out = save_skill.invoke(
        {
            "name": "fix-flaky-retry",
            "description": "retries a flaky step with backoff",
            "body": "1. Detect the flaky step.\n2. Retry with backoff.\n",
        }
    )
    assert "/reload" in out
    assert (tmp_path / ".luna" / "skills" / "fix-flaky-retry" / "SKILL.md").is_file()


def test_save_skill_reports_validation_errors_as_a_string(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    out = save_skill.invoke({"name": "../escape", "description": "d", "body": "b"})
    assert "not a valid skill name" in out
```

Add `save_skill` to this test file's existing top-of-file import line
(`from luna.extensions.extension_tools import (EXTENSION_INTERRUPTS,
manage_mcp, manage_skills, remember)` → add `save_skill` to that tuple).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_extension_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'save_skill'`, and
`test_interrupts_registered` fails on the updated literal (both expected
at this point — Step 3 fixes both).

- [ ] **Step 3: Implement `save_skill` in `luna/extensions/extension_tools.py`**

Add after the existing `manage_skills` function:

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
    repeat — body should be the actual reusable instructions/steps, not a
    narrative of what just happened. Defaults to project scope
    (.luna/skills/); pass project=False only for a pattern that is not
    specific to this repository. Activates after the user runs /reload.
    """
    try:
        path = skills.save(name, description, body, project=project)
    except LunaConfigError as exc:
        return str(exc)
    return f"saved skill at {path.as_posix()}. Run /reload to activate."
```

Update the module's bottom two lines:

```python
EXTENSION_TOOLS = [manage_mcp, manage_skills, remember, save_skill]
EXTENSION_INTERRUPTS = {
    "manage_mcp": True,
    "manage_skills": True,
    "remember": True,
    "save_skill": True,
}
```

`skills` and `LunaConfigError` are already imported at the top of this
file (`from luna.extensions import mcp, skills` and `from
luna.config.providers import LunaConfigError`) — no new imports needed.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_extension_tools.py -v`
Expected: all pass, including the updated `test_interrupts_registered`.

- [ ] **Step 5: Add the system-prompt nudge**

In `luna/config/prompts.py`, the existing line:

```
- When you hit a dead end or make a load-bearing decision, record it with the
  `remember` tool (use `failures` especially — future runs read it back).
```

Replace with:

```
- When you hit a dead end or make a load-bearing decision, record it with the
  `remember` tool (use `failures` especially — future runs read it back).
- After successfully completing a nontrivial, reusable multi-step pattern,
  consider saving it with `save_skill` so future sessions can reuse it.
```

- [ ] **Step 6: Check and update `AGENTS.md`**

Read `AGENTS.md`'s description of `EXTENSION_TOOLS` / `manage_skills` /
`remember` (search for `manage_skills` in the file) and add `save_skill`
to whatever line documents that tool list, using the file's actual
current wording as the basis for the edit.

- [ ] **Step 7: Add a CHANGELOG entry**

Add to `CHANGELOG.md`'s `## [Unreleased]` → `### Добавлено` subsection
(read the file first to find its current last bullet — add immediately
after it):

```markdown
- Новый инструмент `save_skill`: агент может сохранить успешный,
  повторно применимый паттерн как локальный skill (по умолчанию —
  на уровне проекта, `.luna/skills/`), переиспользуя существующий формат
  `SKILL.md`. Подтверждается пользователем, как и `manage_skills`/
  `remember`; активируется через `/reload`.
```

- [ ] **Step 8: Run the full verification gate**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all clean — final gate for the whole feature.

- [ ] **Step 9: Commit**

```bash
git add luna/extensions/extension_tools.py luna/config/prompts.py AGENTS.md CHANGELOG.md tests/test_extension_tools.py
git commit -m "feat: save_skill tool for agent-authored reusable skills"
```
