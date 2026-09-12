"""Built-in and user-defined subagents for delegation via the ``task`` tool."""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

from deepagents import SubAgent
from deepagents.middleware import FilesystemMiddleware

from luna.config.config import config_dir
from luna.config.providers import LunaConfigError

# Filesystem tools a subagent can be restricted to (deepagents FsToolName set).
# Read-only tools are always allowed. Declarative subagents inherit the parent's
# ``interrupt_on``, so a mutating tool call inside one still raises an approval
# prompt. Subagents operate on the real repository (same backend as the main
# agent), so ``unsafe = true`` is a REQUIRED gate: a subagent that requests a
# mutating tool without it is a hard config error at startup / ``/reload``.
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
    backend: object | None = None,
) -> SubAgent:
    mw: list = []
    if guard is not None:
        mw.append(guard)
    if fs_tools is not None:
        mw.append(FilesystemMiddleware(backend=backend, tools=fs_tools))
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
    backend: object | None = None,
) -> list[SubAgent]:
    """Built-in subagents plus any defined in ``subagents.toml`` (user + project).

    When ``fast_model`` is set, every built-in subagent that has no explicit
    model of its own is rebuilt to run on that cheaper/faster model.

    ``guard`` (a ``tool_guard`` middleware) is attached first to every subagent,
    so deny rules and undo snapshots apply inside delegated work too. ``backend``
    is the real ``LocalShellBackend`` of the main agent; it is threaded into every
    subagent's ``FilesystemMiddleware`` so subagents read and write the actual
    repository (not an ephemeral in-memory filesystem).

    Because subagents genuinely operate on the repo, a user subagent that
    requests a mutating tool (``write_file`` / ``edit_file`` / ``delete`` /
    ``execute``) **must** set ``unsafe = true`` (a real TOML boolean) in its
    ``[subagent.<name>]`` block — otherwise this raises ``LunaConfigError`` at
    startup / ``/reload``. Deny rules, undo snapshots and approval prompts still
    apply regardless. A user subagent with no ``tools`` key is restricted to the
    read-only set rather than inheriting the full default toolset.
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
                backend=backend,
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
            if mutating and cfg.get("unsafe") is not True:
                raise LunaConfigError(
                    f"subagent {name!r} requests {sorted(mutating)} — it can write to / run "
                    f"commands in the repository. Add 'unsafe = true' to its [subagent.{name}] "
                    f"block to acknowledge this (deny rules, approval prompts, and undo snapshots "
                    f"still apply), or drop those tools."
                )
            if mutating and on_warn is not None:  # only reached when unsafe is True
                on_warn(
                    f"subagent {name!r} may write to / run commands in the repo "
                    f"(deny rules, approval prompts, and undo all apply)"
                )
        agent = _subagent(
            name,
            cfg.get("description", name),
            cfg.get("prompt", f"You are the {name} subagent."),
            list(tools) if tools is not None else list(_READ_ONLY),
            cfg.get("model"),
            guard=guard,
            backend=backend,
        )
        agents = [a for a in agents if a["name"] != name] + [agent]
    return agents


def subagent_summaries(
    workdir: str = ".", *, env: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """``(name, description)`` for every available subagent."""
    return [(a["name"], a["description"]) for a in load_subagents(workdir, env=env)]
