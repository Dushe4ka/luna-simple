"""Layered configuration resolution for Luna.

Precedence, highest first: CLI overrides > environment > ``./.luna.toml`` >
``~/.config/luna/config.toml`` > built-in defaults.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from luna_core.hooks import POINTS

from luna.config.providers import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    LunaConfigError,
    ProviderSpec,
    merge_providers,
)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

# Dotted keys accepted by ``set_config_values`` / ``luna config set``.
_SETTABLE: dict[str, str] = {
    "model.provider": "str",
    "model.name": "str",
    "model.fast": "str",
    "agent.yolo": "bool",
    "agent.workdir": "str",
    "agent.verify_command": "str",
    "agent.format_command": "str",
    "agent.diagnose_command": "str",
    "agent.language": "str",
    "agent.temperature": "float",
    "agent.max_tokens": "int",
    "ui.splash": "bool",
}


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return Luna's config directory (``$XDG_CONFIG_HOME/luna`` or ``~/.config/luna``)."""
    env = os.environ if env is None else env
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "luna"


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the path to the user config file."""
    return config_dir(env) / "config.toml"


@dataclass
class LunaConfig:
    """Fully resolved runtime settings."""

    provider: str = DEFAULT_PROVIDER
    model: str | None = None
    fast_model: str | None = None
    pricing: dict = field(default_factory=dict)
    workdir: str = "."
    yolo: bool = False
    verify_command: str = ""
    format_command: str = "auto"
    diagnose_command: str = "auto"
    language: str = ""
    show_splash: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning: str | None = None
    max_cost: float | None = None
    max_parallel: int = 4
    roles: dict[str, str] = field(default_factory=dict)
    sandbox: str = "local"
    sandbox_image: str = "python:3.12-slim"
    sandbox_network: str = "none"
    hooks: list[dict] = field(default_factory=list)
    ignored_extra: bool = False
    custom_providers: dict[str, ProviderSpec] = field(default_factory=dict)


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LunaConfigError(f"Cannot read config {path}: {exc}") from exc


def _apply_toml(data: dict, into: dict) -> None:
    model = data.get("model", {})
    if "provider" in model:
        into["provider"] = model["provider"]
    if "name" in model:
        into["model"] = model["name"]
    roles = dict(into.get("roles", {}))
    for role in ("fast", "plan", "vision", "summarize"):
        if role in model:
            roles[role] = str(model[role])
    if roles:
        into["roles"] = roles
    if isinstance(model.get("pricing"), dict):
        into["pricing"] = {k: dict(v) for k, v in model["pricing"].items() if isinstance(v, dict)}

    agent = data.get("agent", {})
    for key in (
        "yolo",
        "workdir",
        "verify_command",
        "format_command",
        "diagnose_command",
        "language",
        "temperature",
        "max_tokens",
        "reasoning",
        "max_cost",
        "max_parallel",
    ):
        if key in agent:
            into[key] = agent[key]
    if isinstance(agent.get("extra"), dict) and agent["extra"]:
        into["ignored_extra"] = True

    sandbox = data.get("sandbox", {})
    for src, dst in (
        ("kind", "sandbox"),
        ("image", "sandbox_image"),
        ("network", "sandbox_network"),
    ):
        if src in sandbox:
            into[dst] = str(sandbox[src])
    if isinstance(data.get("hooks"), list):
        into["hooks"] = [dict(h) for h in data["hooks"] if isinstance(h, dict)]

    ui = data.get("ui", {})
    if "splash" in ui:
        into["show_splash"] = bool(ui["splash"])

    provider = data.get("provider", {})
    if isinstance(provider.get("custom"), dict):
        into["custom_providers"] = {
            k: dict(v) for k, v in provider["custom"].items() if isinstance(v, dict)
        }


def _validate_hook(index: int, entry: dict) -> None:
    """Raise ``LunaConfigError`` naming the ``[[hooks]]`` entry unless it is well formed."""
    where = f"[[hooks]] entry {index}"
    if entry.get("event") not in POINTS:
        raise LunaConfigError(
            f"{where}: event must be one of {', '.join(POINTS)}, got {entry.get('event')!r}."
        )
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        raise LunaConfigError(f"{where}: command must be a non-empty string.")
    if "match" in entry and not isinstance(entry["match"], str):
        raise LunaConfigError(f"{where}: match must be a string, got {entry['match']!r}.")
    timeout = entry.get("timeout")
    if "timeout" in entry and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
    ):
        raise LunaConfigError(f"{where}: timeout must be a number > 0, got {timeout!r}.")


def _apply_env(env: Mapping[str, str], into: dict) -> None:
    if env.get("LUNA_PROVIDER"):
        into["provider"] = env["LUNA_PROVIDER"]
    if env.get("LUNA_MODEL"):
        into["model"] = env["LUNA_MODEL"]
    if env.get("LUNA_WORKDIR"):
        into["workdir"] = env["LUNA_WORKDIR"]
    if "LUNA_YOLO" in env:
        into["yolo"] = env["LUNA_YOLO"].strip().lower() in _TRUTHY


def load_config(
    cli_overrides: Mapping[str, object] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> LunaConfig:
    """Merge every configuration layer into a :class:`LunaConfig`."""
    env = os.environ if env is None else env
    cwd_path = Path(cwd) if cwd is not None else Path.cwd()

    merged: dict = {}
    _apply_toml(_load_toml(config_path(env)), merged)
    _apply_toml(_load_toml(cwd_path / ".luna.toml"), merged)
    _apply_env(env, merged)

    for key, value in (cli_overrides or {}).items():
        if value is not None:
            merged[key] = value

    custom_providers: dict[str, ProviderSpec] = {}
    for name, fields in merged.get("custom_providers", {}).items():
        base_url = fields.get("base_url")
        env_var = fields.get("env_var")
        if not base_url:
            raise LunaConfigError(
                f"Custom provider {name!r} needs base_url in [provider.custom.{name}]."
            )
        custom_providers[name] = ProviderSpec(
            key=name,
            api="openai_chat",
            default_model=fields.get("default_model") or "gpt-4o",
            # Optional: a keyless local endpoint (vLLM, LM Studio) needs no
            # API key at all — same as the built-in ``ollama`` provider.
            env_var=env_var or None,
            base_url=base_url,
        )

    registry = merge_providers(custom_providers)
    provider = merged.get("provider", DEFAULT_PROVIDER)
    if provider not in registry:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(sorted(registry))}."
        )

    sandbox = merged.get("sandbox", "local")
    if sandbox not in ("local", "docker"):
        raise LunaConfigError(f"[sandbox] kind must be 'local' or 'docker', got {sandbox!r}.")
    sandbox_network = merged.get("sandbox_network", "none")
    if sandbox_network not in ("none", "bridge"):
        raise LunaConfigError(
            f"[sandbox] network must be 'none' or 'bridge', got {sandbox_network!r}."
        )
    reasoning = merged.get("reasoning")
    if reasoning not in (None, "low", "medium", "high"):
        raise LunaConfigError(
            f"[agent] reasoning must be 'low', 'medium' or 'high', got {reasoning!r}."
        )
    max_parallel = merged.get("max_parallel", 4)
    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel < 1:
        raise LunaConfigError(
            f"[agent] max_parallel must be an integer >= 1, got {max_parallel!r}."
        )
    max_cost = merged.get("max_cost")
    if max_cost is not None and (
        isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)) or max_cost < 0
    ):
        raise LunaConfigError(f"[agent] max_cost must be a number >= 0, got {max_cost!r}.")
    hooks = list(merged.get("hooks", []))
    for index, entry in enumerate(hooks, 1):
        _validate_hook(index, entry)
    roles = dict(merged.get("roles", {}))

    return LunaConfig(
        provider=provider,
        model=merged.get("model"),
        fast_model=roles.get("fast"),
        pricing=dict(merged.get("pricing", {})),
        workdir=str(merged.get("workdir", ".")),
        yolo=bool(merged.get("yolo", False)),
        verify_command=str(merged.get("verify_command", "")),
        format_command=str(merged.get("format_command", "auto")),
        diagnose_command=str(merged.get("diagnose_command", "auto")),
        language=str(merged.get("language", "")),
        show_splash=bool(merged.get("show_splash", True)),
        temperature=merged.get("temperature"),
        max_tokens=merged.get("max_tokens"),
        reasoning=reasoning,
        max_cost=max_cost,
        max_parallel=max_parallel,
        roles=roles,
        sandbox=sandbox,
        sandbox_image=merged.get("sandbox_image", "python:3.12-slim"),
        sandbox_network=sandbox_network,
        hooks=hooks,
        ignored_extra=bool(merged.get("ignored_extra", False)),
        custom_providers=custom_providers,
    )


def _coerce(value: str, kind: str) -> object:
    if kind == "bool":
        low = value.strip().lower()
        if low in _TRUTHY:
            return True
        if low in _FALSY:
            return False
        raise LunaConfigError(f"Expected a boolean (true/false), got {value!r}.")
    if kind == "int":
        try:
            return int(value)
        except ValueError:
            raise LunaConfigError(f"Expected an integer, got {value!r}.") from None
    if kind == "float":
        try:
            return float(value)
        except ValueError:
            raise LunaConfigError(f"Expected a number, got {value!r}.") from None
    return value


def _dump_toml(data: Mapping[str, Mapping[str, object]]) -> str:
    lines: list[str] = []
    for section, values in data.items():
        if not values:
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = repr(value)
            else:
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                rendered = f'"{escaped}"'
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def set_config_values(
    updates: Mapping[str, str],
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Write ``dotted.key -> value`` pairs into the user config file.

    Values arrive as strings (from the CLI) and are coerced to the type the
    key expects. Returns the path written.
    """
    path = config_path(env)
    data: dict[str, dict[str, object]] = {}
    for section, values in _load_toml(path).items():
        if isinstance(values, dict):
            data[section] = dict(values)

    for dotted, raw in updates.items():
        if dotted not in _SETTABLE:
            raise LunaConfigError(
                f"Unknown setting {dotted!r}. Valid keys: {', '.join(_SETTABLE)}."
            )
        section, key = dotted.split(".", 1)
        value = _coerce(raw, _SETTABLE[dotted])
        if section == "model" and key == "provider" and value not in PROVIDERS:
            raise LunaConfigError(
                f"Unknown provider {value!r}. Choose one of: {', '.join(PROVIDERS)}."
            )
        data.setdefault(section, {})[key] = value

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_toml(data))
    return path
