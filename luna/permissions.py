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
    """Allow / deny rule lists with ``deny``-wins matching."""

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    def _hit(self, rules: list[str], tool: str, subject: str) -> bool:
        for rule in rules:
            rtool, _, pattern = rule.partition(":")
            if rtool == tool and fnmatch(subject, pattern):
                return True
        return False

    def match(self, tool: str, args: dict) -> str | None:
        """Return ``"deny"``, ``"allow"``, or ``None`` for a tool call."""
        subject = _subject(tool, args)
        if self._hit(self.deny, tool, subject):
            return "deny"
        if self._hit(self.allow, tool, subject):
            return "allow"
        return None


def _read(path: Path) -> dict:
    """Parse a TOML file, returning ``{}`` on any read/parse error."""
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError):
        return {}


def _project_file(workdir: str) -> Path:
    """Return the path to a project's ``.luna/permissions.toml``."""
    return Path(workdir) / ".luna" / "permissions.toml"


def load_rules(workdir: str, env: Mapping[str, str] | None = None) -> RuleSet:
    """Merge user ``config.toml [permissions]`` with the project rules file."""
    rs = RuleSet()
    user = _read(config_dir(env) / "config.toml").get("permissions", {})
    proj = _read(_project_file(workdir))
    proj = proj.get("permissions", proj)  # allow bare or [permissions] table
    for src in (user, proj):
        rs.allow += [r for r in src.get("allow", []) if r not in rs.allow]
        rs.deny += [r for r in src.get("deny", []) if r not in rs.deny]
    return rs


def append_project_rule(workdir: str, rule: str) -> Path:
    """Append ``rule`` to ``allow`` in ``<workdir>/.luna/permissions.toml``."""
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
    """Propose a rule string for a tool call (for the ``[a] always`` prompt)."""
    if tool == "execute":
        first = str(args.get("command", "")).split()
        return f"execute:{first[0]} *" if first else "execute:*"
    return f"{tool}:{_subject(tool, args)}"
