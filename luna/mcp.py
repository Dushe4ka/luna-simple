"""MCP server configuration (Claude-compatible ``mcp.json``) and tool discovery."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from luna.config import config_dir
from luna.providers import LunaConfigError

try:  # optional: pip install "luna-simple[mcp]"
    from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: F401

    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via monkeypatch
    MCP_AVAILABLE = False

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_INSTALL_HINT = 'MCP support not installed. Run: pip install "luna-simple[mcp]"'


def mcp_files(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> list[Path]:
    """User ``mcp.json`` first, then the project ``./.luna/mcp.json``."""
    return [config_dir(env) / "mcp.json", Path(workdir) / ".luna" / "mcp.json"]


def _expand(value, environ: Mapping[str, str]):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [_expand(v, environ) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, environ) for k, v in value.items()}
    return value


def _read(path: Path) -> dict:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise LunaConfigError(f"Cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LunaConfigError(f"Invalid JSON in {path}: {exc}") from exc
    servers = data.get("mcpServers", {})
    return servers if isinstance(servers, dict) else {}


def load_mcp_config(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Return the merged ``mcpServers`` map, ``${ENV}`` expanded (project wins)."""
    environ = os.environ if env is None else env
    merged: dict[str, dict] = {}
    for path in mcp_files(workdir, env=env):
        merged.update(_read(path))
    return {name: _expand(spec, environ) for name, spec in merged.items()}


def to_connections(servers: Mapping[str, dict]) -> dict[str, dict]:
    """Translate Claude-style server specs into langchain connection dicts."""
    out: dict[str, dict] = {}
    for name, spec in servers.items():
        kind = spec.get("type") or spec.get("transport")
        if "command" in spec and kind in (None, "stdio"):
            conn = {
                "transport": "stdio",
                "command": spec["command"],
                "args": list(spec.get("args", [])),
            }
            if spec.get("env"):
                conn["env"] = dict(spec["env"])
        elif kind in ("http", "streamable_http") and spec.get("url"):
            conn = {"transport": "streamable_http", "url": spec["url"]}
            if spec.get("headers"):
                conn["headers"] = dict(spec["headers"])
        elif kind == "sse" and spec.get("url"):
            conn = {"transport": "sse", "url": spec["url"]}
            if spec.get("headers"):
                conn["headers"] = dict(spec["headers"])
        else:
            raise LunaConfigError(
                f"MCP server {name!r}: need a 'command' (stdio) or a 'url' with type 'http'/'sse'."
            )
        out[name] = conn
    return out


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
    doc = {"mcpServers": {}}
    if path.exists():
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise LunaConfigError(f"Invalid JSON in {path}: {exc}") from exc
        doc.setdefault("mcpServers", {})
    doc["mcpServers"][name] = spec
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def remove_server(
    name: str,
    *,
    project: bool = False,
    workdir: str = ".",
    env: Mapping[str, str] | None = None,
) -> bool:
    """Delete ``mcpServers.<name>``. Returns True if it was present."""
    path = _target_file(project, workdir, env)
    if not path.exists():
        return False
    doc = json.loads(path.read_text())
    if name not in doc.get("mcpServers", {}):
        return False
    del doc["mcpServers"][name]
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return True


def load_mcp_tools(
    connections: Mapping[str, dict],
    *,
    on_warn: Callable[[str], None] = print,
) -> list:
    """Discover tools from every configured MCP server.

    Returns ``[]`` (and warns) when the adapter is missing or a server fails —
    a broken server must never break Luna.
    """
    if not connections:
        return []
    if not MCP_AVAILABLE:
        on_warn(_INSTALL_HINT)
        return []
    import asyncio

    from langchain_mcp_adapters.client import MultiServerMCPClient

    try:
        client = MultiServerMCPClient(dict(connections), tool_name_prefix=True)
        return asyncio.run(client.get_tools())
    except Exception as exc:  # noqa: BLE001 - one bad server must not break Luna
        on_warn(f"MCP: could not start servers ({exc}). Try 'luna mcp test <name>'.")
        return []
