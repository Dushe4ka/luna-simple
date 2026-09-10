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

from luna.providers import DEFAULT_PROVIDER, PROVIDERS, LunaConfigError

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
    show_splash: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    extra_model_kwargs: dict = field(default_factory=dict)

    @property
    def model_kwargs(self) -> dict:
        """Keyword arguments forwarded to ``init_chat_model``."""
        kwargs: dict = dict(self.extra_model_kwargs)
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        return kwargs


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
    if "fast" in model:
        into["fast_model"] = model["fast"]
    if isinstance(model.get("pricing"), dict):
        into["pricing"] = {k: dict(v) for k, v in model["pricing"].items() if isinstance(v, dict)}

    agent = data.get("agent", {})
    for key in ("yolo", "workdir", "verify_command", "format_command", "temperature", "max_tokens"):
        if key in agent:
            into[key] = agent[key]
    if isinstance(agent.get("extra"), dict):
        into["extra_model_kwargs"] = dict(agent["extra"])

    ui = data.get("ui", {})
    if "splash" in ui:
        into["show_splash"] = bool(ui["splash"])


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

    provider = merged.get("provider", DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(PROVIDERS)}."
        )

    return LunaConfig(
        provider=provider,
        model=merged.get("model"),
        fast_model=merged.get("fast_model"),
        pricing=dict(merged.get("pricing", {})),
        workdir=str(merged.get("workdir", ".")),
        yolo=bool(merged.get("yolo", False)),
        verify_command=str(merged.get("verify_command", "")),
        format_command=str(merged.get("format_command", "auto")),
        show_splash=bool(merged.get("show_splash", True)),
        temperature=merged.get("temperature"),
        max_tokens=merged.get("max_tokens"),
        extra_model_kwargs=dict(merged.get("extra_model_kwargs", {})),
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
