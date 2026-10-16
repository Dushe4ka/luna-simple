"""Subagent definitions for the app: user + project subagents.toml over the core built-ins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from luna_core.subagents import SubagentConfigError, SubagentSpec
from luna_core.subagents import load_subagents as _load

from luna.config.config import config_dir
from luna.config.providers import LunaConfigError


def subagent_files(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> list[Path]:
    """User file first, then the project's ``.luna/subagents.toml``."""
    return [config_dir(env) / "subagents.toml", Path(workdir) / ".luna" / "subagents.toml"]


def load_subagents(
    workdir: str = ".",
    *,
    env: Mapping[str, str] | None = None,
    on_warn: Callable[[str], None] | None = None,
) -> dict[str, SubagentSpec]:
    """All subagents (raises ``LunaConfigError`` for an invalid definition)."""
    try:
        return _load(subagent_files(workdir, env=env), on_warn=on_warn)
    except SubagentConfigError as exc:
        raise LunaConfigError(str(exc)) from exc


def subagent_summaries(
    workdir: str = ".", *, env: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """``(name, description)`` for every available subagent."""
    return [(s.name, s.description) for s in load_subagents(workdir, env=env).values()]
