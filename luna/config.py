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


def _user_config_path(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "luna" / "config.toml"


@dataclass
class LunaConfig:
    """Fully resolved runtime settings."""

    provider: str = DEFAULT_PROVIDER
    model: str | None = None
    workdir: str = "."
    yolo: bool = False
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

    agent = data.get("agent", {})
    for key in ("yolo", "workdir", "temperature", "max_tokens"):
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
    _apply_toml(_load_toml(_user_config_path(env)), merged)
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
        workdir=str(merged.get("workdir", ".")),
        yolo=bool(merged.get("yolo", False)),
        show_splash=bool(merged.get("show_splash", True)),
        temperature=merged.get("temperature"),
        max_tokens=merged.get("max_tokens"),
        extra_model_kwargs=dict(merged.get("extra_model_kwargs", {})),
    )
