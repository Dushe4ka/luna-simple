"""MCP server configuration (Claude-compatible ``mcp.json``) on top of ``luna_core.mcp``.

Connecting to servers and exposing their tools is ``luna_core.mcp.MCPManager``'s job;
this module only knows where the app keeps its config files.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping
from pathlib import Path

from luna_core import mcp as core_mcp

from luna.config.config import config_dir
from luna.config.providers import LunaConfigError

# optional: pip install "luna-simple[mcp]"
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


def mcp_files(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> list[Path]:
    """User ``mcp.json`` first, then the project ``./.luna/mcp.json``."""
    return [config_dir(env) / "mcp.json", Path(workdir) / ".luna" / "mcp.json"]


def load_mcp_config(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Return the merged ``mcpServers`` map, ``${ENV}`` expanded (project wins)."""
    environ = os.environ if env is None else env
    try:
        return core_mcp.load_config(mcp_files(workdir, env=env), environ)
    except core_mcp.MCPConfigError as exc:
        raise LunaConfigError(str(exc)) from exc


def _target_file(project: bool, workdir: str, env: Mapping[str, str] | None) -> Path:
    return mcp_files(workdir, env=env)[1 if project else 0]


def add_server(
    name: str,
    spec: dict,
    *,
    project: bool = False,
    workdir: str = ".",
    env: Mapping[str, str] | None = None,
) -> Path:
    """Write ``spec`` under ``mcpServers.<name>`` in the user or project file."""
    path = _target_file(project, workdir, env)
    try:
        return core_mcp.add_server(path, name, spec)
    except ValueError as exc:  # json.JSONDecodeError
        raise LunaConfigError(f"Invalid JSON in {path}: {exc}") from exc


def remove_server(
    name: str,
    *,
    project: bool = False,
    workdir: str = ".",
    env: Mapping[str, str] | None = None,
) -> bool:
    """Delete ``mcpServers.<name>``. Returns True if it was present."""
    path = _target_file(project, workdir, env)
    try:
        return core_mcp.remove_server(path, name)
    except ValueError as exc:  # json.JSONDecodeError
        raise LunaConfigError(f"Invalid JSON in {path}: {exc}") from exc
