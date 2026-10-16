"""Install, list, and remove Anthropic-style Agent Skills for Luna (on ``luna_core.skills``).

A skill is a directory containing ``SKILL.md`` with YAML frontmatter that
declares at least ``name`` and ``description``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from luna_core import skills as core_skills
from luna_core.skills import SkillError

from luna.config.config import config_dir
from luna.config.providers import LunaConfigError
from luna.extensions.registry import resolve_skill


def skills_dirs(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> list[Path]:
    """User skills dir first, then the project ``./.luna/skills`` dir."""
    return [config_dir(env) / "skills", Path(workdir) / ".luna" / "skills"]


def existing_skill_dirs(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> list[Path]:
    """Only the skill source dirs that actually exist."""
    return [d for d in skills_dirs(workdir, env=env) if d.is_dir()]


def _frontmatter(text: str) -> dict:
    return core_skills.parse_frontmatter(text)[0]


def install(
    source: str,
    *,
    name: str | None = None,
    project: bool = False,
    workdir: str = ".",
    env: Mapping[str, str] | None = None,
    run: Callable = subprocess.run,
) -> str:
    """Clone ``source`` and install the skill it points at. Returns a status line."""
    dest = skills_dirs(workdir, env=env)[1 if project else 0]
    try:
        message = core_skills.install(
            source, dest, name=name, run=run, resolve=lambda n: resolve_skill(n, env=env)
        )
    except SkillError as exc:
        raise LunaConfigError(str(exc)) from exc
    scope = "project" if project else "user"
    return f"{message} ({scope}). Run /reload to activate."


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
    dest = skills_dirs(workdir, env=env)[1 if project else 0]
    try:
        return core_skills.save(name, description, body, dest)
    except SkillError as exc:
        raise LunaConfigError(str(exc)) from exc
    except OSError as exc:
        raise LunaConfigError(f"could not write the skill: {exc}") from exc


def remove(
    name: str,
    *,
    project: bool = False,
    workdir: str = ".",
    env: Mapping[str, str] | None = None,
) -> bool:
    """Delete an installed skill, preferred scope first. Returns True if it existed."""
    user, proj = skills_dirs(workdir, env=env)
    ordered = (
        [("project", proj), ("user", user)] if project else [("user", user), ("project", proj)]
    )
    try:
        return core_skills.remove(name, ordered)
    except SkillError:
        return False


def list_skills(
    workdir: str = ".", *, env: Mapping[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """Return ``(scope, name, description)`` for every installed skill."""
    rows: list[tuple[str, str, str]] = []
    for scope, root in zip(("user", "project"), skills_dirs(workdir, env=env), strict=True):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                meta = _frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
                rows.append((scope, child.name, str(meta.get("description", ""))))
    return rows
