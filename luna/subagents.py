"""Built-in and user-defined subagents for delegation via the ``task`` tool."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from deepagents import SubAgent
from deepagents.middleware import FilesystemMiddleware

from luna.config import config_dir
from luna.providers import LunaConfigError

# Filesystem tools a subagent can be restricted to (deepagents FsToolName set).
VALID_TOOLS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}
)
_READ_ONLY = ["ls", "read_file", "glob", "grep"]


def _subagent(
    name: str, description: str, prompt: str, fs_tools: list[str] | None, model: str | None = None
) -> SubAgent:
    spec: dict = {"name": name, "description": description, "system_prompt": prompt}
    if fs_tools is not None:
        spec["middleware"] = [FilesystemMiddleware(tools=fs_tools)]
    if model:
        spec["model"] = model
    return SubAgent(**spec)


BUILTIN_SUBAGENTS: list[SubAgent] = [
    _subagent(
        "researcher",
        "Investigate the codebase, docs, or a question and report back. Read-only.",
        "You explore and report. You never modify files or run mutating commands.",
        _READ_ONLY,
    ),
    _subagent(
        "reviewer",
        "Review a diff or file for bugs, risks, and simplifications. Read-only.",
        "You review code critically and return concrete, prioritized findings.",
        _READ_ONLY,
    ),
]


def _config_files(workdir: str, env: Mapping[str, str] | None) -> list[Path]:
    return [
        config_dir(env) / "subagents.toml",
        Path(workdir) / ".luna" / "subagents.toml",
    ]


def _load_raw(workdir: str, env: Mapping[str, str] | None) -> dict:
    merged: dict = {}
    for path in _config_files(workdir, env):
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise LunaConfigError(f"Cannot read {path}: {exc}") from exc
        merged.update(data.get("subagent", {}))
    return merged


def load_subagents(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> list[SubAgent]:
    """Built-in subagents plus any defined in ``subagents.toml`` (user + project)."""
    agents = list(BUILTIN_SUBAGENTS)
    for name, cfg in _load_raw(workdir, env).items():
        tools = cfg.get("tools")
        if tools is not None:
            bad = set(tools) - VALID_TOOLS
            if bad:
                raise LunaConfigError(
                    f"subagent {name!r}: unknown tools {sorted(bad)}. "
                    f"Valid: {', '.join(sorted(VALID_TOOLS))}"
                )
        agent = _subagent(
            name,
            cfg.get("description", name),
            cfg.get("prompt", f"You are the {name} subagent."),
            list(tools) if tools is not None else None,
            cfg.get("model"),
        )
        agents = [a for a in agents if a["name"] != name] + [agent]
    return agents


def subagent_summaries(
    workdir: str = ".", *, env: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """``(name, description)`` for every available subagent."""
    return [(a["name"], a["description"]) for a in load_subagents(workdir, env=env)]
