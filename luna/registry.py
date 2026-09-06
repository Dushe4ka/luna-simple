"""Curated registry of well-known MCP servers and skills, plus user overrides."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from luna.config import config_dir
from luna.providers import LunaConfigError

MCP_REGISTRY: dict[str, dict] = {
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
    },
    "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
    },
    "git": {"command": "uvx", "args": ["mcp-server-git"]},
    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
    "sequential-thinking": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
    },
    "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
    "time": {"command": "uvx", "args": ["mcp-server-time"]},
    "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
}

SKILL_REGISTRY: dict[str, dict] = {
    "pdf": {"repo": "anthropics/skills", "path": "document-skills/pdf"},
    "docx": {"repo": "anthropics/skills", "path": "document-skills/docx"},
    "xlsx": {"repo": "anthropics/skills", "path": "document-skills/xlsx"},
    "pptx": {"repo": "anthropics/skills", "path": "document-skills/pptx"},
}


def _registry_path(env: Mapping[str, str] | None) -> Path:
    return config_dir(env) / "registry.toml"


def load_user_registry(*, env: Mapping[str, str] | None = None) -> dict:
    """Return ``{"mcp": {...}, "skills": {...}}`` from ``registry.toml``."""
    path = _registry_path(env)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {"mcp": {}, "skills": {}}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LunaConfigError(f"Cannot read {path}: {exc}") from exc
    return {"mcp": dict(data.get("mcp", {})), "skills": dict(data.get("skills", {}))}


def _merged(kind: str, builtin: dict, env: Mapping[str, str] | None) -> dict:
    merged = dict(builtin)
    merged.update(load_user_registry(env=env)[kind])
    return merged


def known_mcp(*, env: Mapping[str, str] | None = None) -> list[str]:
    """Names of every MCP server Luna knows (built-in + user registry)."""
    return sorted(_merged("mcp", MCP_REGISTRY, env))


def known_skills(*, env: Mapping[str, str] | None = None) -> list[str]:
    """Names of every skill Luna knows (built-in + user registry)."""
    return sorted(_merged("skills", SKILL_REGISTRY, env))


def resolve_mcp(name: str, *, env: Mapping[str, str] | None = None) -> dict:
    """Return the MCP server spec for ``name`` or raise :class:`LunaConfigError`."""
    reg = _merged("mcp", MCP_REGISTRY, env)
    if name not in reg:
        raise LunaConfigError(
            f"Unknown MCP server {name!r}. Known: {', '.join(sorted(reg))}. "
            f"Or pass an explicit command: luna mcp add {name} -- npx -y <pkg>"
        )
    return dict(reg[name])


def resolve_skill(name: str, *, env: Mapping[str, str] | None = None) -> dict:
    """Return ``{"repo": ..., "path": ...}`` for ``name`` or raise."""
    reg = _merged("skills", SKILL_REGISTRY, env)
    if name not in reg:
        raise LunaConfigError(
            f"Unknown skill {name!r}. Known: {', '.join(sorted(reg))}. "
            f"Or pass a repo: luna skills add owner/repo[/subdir]"
        )
    return dict(reg[name])
