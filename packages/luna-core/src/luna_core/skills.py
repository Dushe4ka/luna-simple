"""Agent Skills: SKILL.md folders with YAML frontmatter."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from luna_core.errors import LunaCoreError
from luna_core.tools.base import Tool, ToolContext, ToolOutput

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85  "


class SkillError(LunaCoreError):
    """A skill could not be read, saved or installed."""


@dataclass(frozen=True)
class SkillInfo:
    """One installed skill."""

    name: str
    description: str
    path: Path
    scope: str


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``---`` YAML frontmatter from the body (``({}, text)`` without one)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return {}, text
    try:
        meta = yaml.safe_load(parts[0][3:]) or {}
    except yaml.YAMLError:
        return {}, text
    body = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
    return (meta if isinstance(meta, dict) else {}), body


def discover(dirs: list[tuple[str, Path]]) -> list[SkillInfo]:
    """Skills in ``(scope, dir)`` order; a later directory overrides a name."""
    found: dict[str, SkillInfo] = {}
    for scope, root in dirs:
        root = Path(root)
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            md = child / "SKILL.md"
            if not md.is_file():
                continue
            meta, _ = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            name, desc = meta.get("name"), meta.get("description")
            if isinstance(name, str) and isinstance(desc, str):
                found[child.name] = SkillInfo(child.name, desc.strip(), child, scope)
    return sorted(found.values(), key=lambda s: s.name)


def load_body(name: str, dirs: list[tuple[str, Path]]) -> str:
    """Return the instructions of skill ``name``."""
    for info in discover(dirs):
        if info.name == name:
            return parse_frontmatter((info.path / "SKILL.md").read_text(encoding="utf-8"))[1]
    raise SkillError(f"no skill named {name!r}")


def _validate_name(name: str) -> None:
    """Raise :class:`SkillError` unless ``name`` is safe to use as one path segment."""
    if not _NAME.fullmatch(name):
        raise SkillError(f"{name!r} is not a valid skill name (letters, digits, - and _, max 64)")


def _validate(name: str, description: str) -> None:
    _validate_name(name)
    if not description.strip():
        raise SkillError("description must not be empty")
    if any(ch in description for ch in _LINE_BREAKS):
        raise SkillError("description must be a single line")


def save(name: str, description: str, body: str, dest_dir: Path) -> Path:
    """Write ``dest_dir/<name>/SKILL.md`` and return its path."""
    _validate(name, description)
    target = Path(dest_dir) / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    front = yaml.safe_dump(
        {"name": name, "description": description},
        allow_unicode=True,
        sort_keys=False,
        width=10_000,
    )
    target.write_text(f"---\n{front}---\n\n{body.rstrip()}\n", encoding="utf-8")
    return target


def remove(name: str, dirs: list[tuple[str, Path]]) -> bool:
    """Delete skill ``name`` from the first directory that has it."""
    _validate_name(name)
    for _scope, root in dirs:
        root = Path(root)
        target = (root / name).resolve()
        if not target.is_relative_to(root.resolve()):
            continue
        if (target / "SKILL.md").is_file():
            shutil.rmtree(target)
            return True
    return False


def _source(source: str, resolve: Callable[[str], dict] | None) -> tuple[str, str | None, str]:
    src = source.rstrip("/")
    local = Path(src).expanduser()
    if "://" in src or src.startswith(("/", "~", ".")):
        if local.exists():
            probe = local if local.is_dir() else local.parent
            root = probe
            while root != root.parent and not (root / ".git").exists():
                root = root.parent
            if (root / ".git").exists():
                sub = probe.relative_to(root).as_posix()
                return str(root), (None if sub == "." else sub), probe.name
        return src, None, Path(src).name
    parts = src.split("/")
    if len(parts) == 2:
        return f"https://github.com/{src}.git", None, parts[1]
    if len(parts) >= 3:
        return f"https://github.com/{parts[0]}/{parts[1]}.git", "/".join(parts[2:]), parts[-1]
    if resolve is None:
        raise SkillError(f"unknown skill source {source!r}")
    spec = resolve(src)
    return (
        f"https://github.com/{spec['repo']}.git",
        spec.get("path"),
        Path(spec.get("path") or src).name,
    )


def _reject_external_symlinks(src: Path) -> None:
    """Refuse a skill folder containing a symlink that points outside it.

    ``install`` copies with ``symlinks=True`` so a link is copied as a link rather
    than having its target's content read into the install directory, but a link
    that escapes the skill folder could still be followed later from inside the
    installed skill — so it is rejected outright instead of being installed.
    """
    root = src.resolve()
    for path in src.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise SkillError(f"skill contains a symlink escaping its folder: {path.name}")


def install(
    source: str,
    dest_dir: Path,
    *,
    name: str | None = None,
    run: Callable = subprocess.run,
    resolve: Callable[[str], dict] | None = None,
) -> str:
    """Clone ``source`` and copy the skill into ``dest_dir``; returns a status line."""
    clone, subdir, default = _source(source, resolve)
    skill_name = name or default
    _validate_name(skill_name)
    tmp = Path(tempfile.mkdtemp(prefix="luna-skill-"))
    try:
        try:
            run(
                ["git", "clone", "--depth", "1", clone, str(tmp / "repo")],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise SkillError("git is required to install skills") from exc
        except subprocess.CalledProcessError as exc:
            raise SkillError(f"git clone failed: {(exc.stderr or str(exc)).strip()}") from exc
        src = tmp / "repo" / (subdir or "")
        md = src / "SKILL.md"
        if not md.is_file():
            raise SkillError(f"no SKILL.md at {source}")
        meta, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
        if not isinstance(meta.get("name"), str) or not isinstance(meta.get("description"), str):
            raise SkillError(
                f"{source}: SKILL.md needs YAML frontmatter with 'name' and 'description'"
            )
        _reject_external_symlinks(src)
        dest_root = Path(dest_dir).resolve()
        dest = (dest_root / skill_name).resolve()
        if not dest.is_relative_to(dest_root):
            raise SkillError(f"{skill_name!r} escapes the destination directory")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"), symlinks=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f"installed skill {skill_name!r}"


def skill_tool(dirs_fn: Callable[[], list[tuple[str, Path]]]) -> Tool:
    """Build the ``skill`` tool: load a skill's instructions by name."""

    async def run(ctx: ToolContext, args: dict) -> ToolOutput:
        name = str(args.get("name", ""))
        try:
            return ToolOutput.text(load_body(name, dirs_fn()), f"skill {name}")
        except SkillError:
            names = ", ".join(s.name for s in discover(dirs_fn())) or "(none installed)"
            return ToolOutput.error(f"no skill named {name!r}; available: {names}")

    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name from the Skills list."}
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    return Tool(
        "skill",
        "Load the full instructions of a skill listed in the system prompt.",
        schema,
        run,
        raw=True,
    )


def save_skill_tool(dest_fn: Callable[[bool], Path]) -> Tool:
    """Build the ``save_skill`` tool: keep a reusable multi-step pattern as a skill."""

    async def run(ctx: ToolContext, args: dict) -> ToolOutput:
        project = bool(args.get("project", True))
        try:
            path = save(
                str(args.get("name", "")),
                str(args.get("description", "")),
                str(args.get("body", "")),
                dest_fn(project),
            )
        except SkillError as exc:
            return ToolOutput.error(str(exc))
        return ToolOutput.text(
            f"saved skill at {path.as_posix()}; it is listed from the next session (or /reload)",
            f"saved skill {args.get('name')}",
        )

    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Letters, digits, - and _."},
            "description": {"type": "string", "description": "One line: when to use it."},
            "body": {"type": "string", "description": "The reusable steps."},
            "project": {
                "type": "boolean",
                "description": "Project scope (default) or user scope.",
                "default": True,
            },
        },
        "required": ["name", "description", "body"],
        "additionalProperties": False,
    }
    return Tool(
        "save_skill",
        "Save a reusable multi-step pattern you just completed as a skill.",
        schema,
        run,
        raw=True,
        mutating=True,
    )
