"""Built-in and user-defined subagents for delegation via the ``task`` tool."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from deepagents import SubAgent

from luna.config import config_dir
from luna.providers import LunaConfigError

VALID_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
        "write_todos",
    }
)

BUILTIN_SUBAGENTS: list[SubAgent] = [
    SubAgent(
        name="researcher",
        description=("Investigate the codebase, docs, or a question and report back. Read-only."),
        system_prompt=("You explore and report. You never modify files or run mutating commands."),
        tools=["ls", "read_file", "glob", "grep"],
    ),
    SubAgent(
        name="reviewer",
        description="Review a diff or file for bugs, risks, and simplifications. Read-only.",
        system_prompt=("You review code critically and return concrete, prioritized findings."),
        tools=["ls", "read_file", "glob", "grep"],
    ),
]


def _name(agent: SubAgent) -> str:
    return agent["name"]


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
        spec: dict = {"name": name, "description": cfg.get("description", name)}
        if cfg.get("prompt"):
            spec["system_prompt"] = cfg["prompt"]
        if tools is not None:
            spec["tools"] = list(tools)
        if cfg.get("model"):
            spec["model"] = cfg["model"]
        agents = [a for a in agents if _name(a) != name] + [SubAgent(**spec)]
    return agents


def subagent_summaries(
    workdir: str = ".", *, env: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """``(name, description)`` for every available subagent."""
    return [(_name(a), a["description"]) for a in load_subagents(workdir, env=env)]
