"""Install, list, and remove Anthropic-style Agent Skills for Luna.

A skill is a directory containing ``SKILL.md`` with YAML frontmatter that
declares at least ``name`` and ``description``.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from luna.config.config import config_dir
from luna.config.providers import LunaConfigError
from luna.registry import resolve_skill


def skills_dirs(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> list[Path]:
    """User skills dir first, then the project ``./.luna/skills`` dir."""
    return [config_dir(env) / "skills", Path(workdir) / ".luna" / "skills"]


def existing_skill_dirs(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> list[Path]:
    """Only the skill source dirs that actually exist."""
    return [d for d in skills_dirs(workdir, env=env) if d.is_dir()]


def _frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip("\"'")
    return {}


def _parse_source(source: str, *, env: Mapping[str, str] | None) -> tuple[str, str | None, str]:
    """Return ``(clone_target, subdir, default_name)``."""
    src = source.rstrip("/")
    expanded = Path(src).expanduser()
    if "://" in src or src.startswith(("/", "~", ".")):
        # local path or explicit URL: find the repo root (dir holding .git)
        if expanded.exists():
            probe = expanded if expanded.is_dir() else expanded.parent
            root = probe
            while root != root.parent and not (root / ".git").is_dir():
                root = root.parent
            if (root / ".git").is_dir():
                sub = probe.relative_to(root)
                return str(root), (str(sub) if str(sub) != "." else None), probe.name
        return src, None, Path(src).name
    parts = src.split("/")
    if len(parts) == 2:  # owner/repo
        return f"https://github.com/{src}.git", None, parts[1]
    if len(parts) >= 3:  # owner/repo/sub/dir
        repo = f"https://github.com/{parts[0]}/{parts[1]}.git"
        sub = "/".join(parts[2:])
        return repo, sub, parts[-1]
    spec = resolve_skill(src, env=env)  # registry name
    return (
        f"https://github.com/{spec['repo']}.git",
        spec.get("path"),
        Path(spec.get("path") or src).name,
    )


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
    clone_target, subdir, default_name = _parse_source(source, env=env)
    skill_name = name or default_name
    dest = skills_dirs(workdir, env=env)[1 if project else 0] / skill_name

    tmp = Path(tempfile.mkdtemp(prefix="luna-skill-"))
    try:
        try:
            run(
                ["git", "clone", "--depth", "1", clone_target, str(tmp / "repo")],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise LunaConfigError("git is required to install skills.") from exc
        except subprocess.CalledProcessError as exc:
            raise LunaConfigError(f"git clone failed: {exc.stderr or exc}".strip()) from exc

        src = tmp / "repo" / (subdir or "")
        skill_md = src / "SKILL.md"
        if not skill_md.is_file():
            raise LunaConfigError(f"No SKILL.md at {source} (looked in {subdir or '<root>'}).")
        meta = _frontmatter(skill_md.read_text())
        if "name" not in meta or "description" not in meta:
            raise LunaConfigError(
                f"{source}: SKILL.md needs YAML frontmatter with 'name' and 'description'."
            )

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    scope = "project" if project else "user"
    return f"installed skill {skill_name!r} ({scope}). Run /reload to activate."


def remove(
    name: str,
    *,
    project: bool = False,
    workdir: str = ".",
    env: Mapping[str, str] | None = None,
) -> bool:
    """Delete an installed skill. Returns True if it existed."""
    dest = skills_dirs(workdir, env=env)[1 if project else 0] / name
    if not dest.is_dir():
        return False
    shutil.rmtree(dest)
    return True


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
                meta = _frontmatter(skill_md.read_text())
                rows.append((scope, child.name, meta.get("description", "")))
    return rows
