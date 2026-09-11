"""Custom slash commands: ``.luna/commands/<name>.md`` prompt templates."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from luna.config import config_dir

_SHELL_RE = re.compile(r"!`([^`]*)`")
_SHELL_TIMEOUT = 30
_SHELL_CAP = 4000


@dataclass
class UserCommand:
    """A parsed ``.luna/commands/<name>.md`` file."""

    name: str
    description: str
    body: str


def _dirs(workdir: str, env: Mapping[str, str] | None) -> list[Path]:
    return [config_dir(env) / "commands", Path(workdir) / ".luna" / "commands"]


def _frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1 :])
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("\"'")
    return {}, text  # unterminated frontmatter: treat the whole file as body


def load(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> dict[str, UserCommand]:
    """Load every ``*.md`` command file (project overrides user, by name)."""
    commands: dict[str, UserCommand] = {}
    for d in _dirs(workdir, env):
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            meta, body = _frontmatter(text)
            commands[path.stem] = UserCommand(
                name=path.stem, description=meta.get("description", ""), body=body
            )
    return commands


def _run_shell(match: re.Match) -> str:
    try:
        proc = subprocess.run(
            match.group(1), shell=True, capture_output=True, text=True, timeout=_SHELL_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip()[:_SHELL_CAP]


def expand(cmd: UserCommand, arg: str, workdir: str) -> str:
    """Render a command's body: ``$ARGUMENTS`` substitution, then `` !`shell` `` injection.

    ``@file``/``@agent`` tokens are left untouched — ``run_repl`` resolves those
    once, after any ``@agent`` rewrite has had a chance to see the raw text, so a
    command body starting with ``@<subagent>`` delegates cleanly instead of being
    polluted by an unrelated file-mention lookup, and ``@file`` mentions aren't
    expanded twice.
    """
    text = cmd.body.replace("$ARGUMENTS", arg)
    text = _SHELL_RE.sub(_run_shell, text)
    return text.strip()
