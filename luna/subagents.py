"""Built-in and user-defined subagents for delegation via the ``task`` tool."""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

from deepagents import SubAgent
from deepagents.middleware import FilesystemMiddleware

from luna.config import config_dir
from luna.providers import LunaConfigError

# Filesystem tools a subagent can be restricted to (deepagents FsToolName set).
# Read-only tools are always allowed; mutating ones need ``unsafe = true`` because
# a subagent runs without the approval prompt the main agent gets.
_SAFE_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})
_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "delete", "execute"})
VALID_TOOLS = _SAFE_TOOLS | _MUTATING_TOOLS
_READ_ONLY = ["ls", "read_file", "glob", "grep"]


def _subagent(
    name: str,
    description: str,
    prompt: str,
    fs_tools: list[str] | None,
    model: str | None = None,
    guard: object | None = None,
) -> SubAgent:
    mw: list = []
    if guard is not None:
        mw.append(guard)
    if fs_tools is not None:
        mw.append(FilesystemMiddleware(tools=fs_tools))
    spec: dict = {"name": name, "description": description, "system_prompt": prompt}
    if mw:
        spec["middleware"] = mw
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


def load_subagents(
    workdir: str = ".",
    *,
    env: Mapping[str, str] | None = None,
    fast_model: str | None = None,
    guard: object | None = None,
    on_warn: Callable[[str], None] | None = None,
) -> list[SubAgent]:
    """Built-in subagents plus any defined in ``subagents.toml`` (user + project).

    When ``fast_model`` is set, every built-in subagent that has no explicit
    model of its own is rebuilt to run on that cheaper/faster model.

    ``guard`` (a ``tool_guard`` middleware) is attached first to every subagent,
    so deny rules and undo snapshots apply inside delegated work too. A user
    subagent that requests a mutating tool must set ``unsafe = true`` in its
    ``[subagent.<name>]`` block; such subagents run without an approval prompt
    (deny rules and undo snapshots still apply) and trigger an ``on_warn`` line.
    """
    agents: list[SubAgent] = []
    for a in BUILTIN_SUBAGENTS:
        model = fast_model if (fast_model and "model" not in a) else a.get("model")
        agents.append(
            _subagent(
                a["name"],
                a["description"],
                a["system_prompt"],
                _READ_ONLY,
                model=model,
                guard=guard,
            )
        )
    for name, cfg in _load_raw(workdir, env).items():
        tools = cfg.get("tools")
        if tools is not None:
            bad = set(tools) - VALID_TOOLS
            if bad:
                raise LunaConfigError(
                    f"subagent {name!r}: unknown tools {sorted(bad)}. "
                    f"Valid: {', '.join(sorted(VALID_TOOLS))}"
                )
            mutating = set(tools) & _MUTATING_TOOLS
            if mutating and not cfg.get("unsafe", False):
                raise LunaConfigError(
                    f"subagent {name!r} requests {sorted(mutating)} but is not marked "
                    f"unsafe. Add 'unsafe = true' to its [subagent.{name}] block to allow "
                    f"write/execute tools in a subagent (they run without an approval "
                    f"prompt), or drop those tools."
                )
            if mutating and on_warn is not None:
                on_warn(f"subagent {name!r} runs {sorted(mutating)} with no approval prompt")
        agent = _subagent(
            name,
            cfg.get("description", name),
            cfg.get("prompt", f"You are the {name} subagent."),
            list(tools) if tools is not None else None,
            cfg.get("model"),
            guard=guard,
        )
        agents = [a for a in agents if a["name"] != name] + [agent]
    return agents


def subagent_summaries(
    workdir: str = ".", *, env: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """``(name, description)`` for every available subagent."""
    return [(a["name"], a["description"]) for a in load_subagents(workdir, env=env)]
