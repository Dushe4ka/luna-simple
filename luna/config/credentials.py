"""Local API-key storage: ``$XDG_CONFIG_HOME/luna/credentials.toml`` (mode 0600).

Kept separate from ``config.toml`` so the config file stays safe to share or
commit. Environment variables always take precedence over stored keys.
"""

from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path

from luna.config.config import config_dir
from luna.config.providers import PROVIDERS, LunaConfigError, ProviderSpec

_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` in a file that is 0600 from the moment it exists.

    The mode is passed to ``open(2)`` (no window with umask permissions) and
    ``fchmod`` tightens a file that already existed with looser ones.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, _FILE_MODE)
    try:
        os.fchmod(fd, _FILE_MODE)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def credentials_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the path to the credentials file."""
    return config_dir(env) / "credentials.toml"


def load_credentials(env: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Return the parsed credentials file, or ``{}`` when it does not exist."""
    path = credentials_path(env)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LunaConfigError(f"Cannot read {path}: {exc}") from exc
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def get_api_key(provider: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Return the stored API key for ``provider``, or ``None``."""
    section = load_credentials(env).get(provider, {})
    key = section.get("api_key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _write(data: Mapping[str, Mapping[str, str]], path: Path) -> None:
    lines: list[str] = []
    for provider, values in data.items():
        lines.append(f"[{provider}]")
        for key, value in values.items():
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
        lines.append("")
    write_private(path, "\n".join(lines).rstrip() + "\n")


def set_api_key(
    provider: str,
    api_key: str,
    *,
    env: Mapping[str, str] | None = None,
    registry: dict[str, ProviderSpec] | None = None,
) -> Path:
    """Store ``api_key`` for ``provider``. Returns the credentials file path."""
    reg = PROVIDERS if registry is None else registry
    if provider not in reg:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(sorted(reg))}."
        )
    if not api_key.strip():
        raise LunaConfigError("API key must not be empty.")
    data = {k: dict(v) for k, v in load_credentials(env).items()}
    data.setdefault(provider, {})["api_key"] = api_key.strip()
    path = credentials_path(env)
    _write(data, path)
    return path


def unset_api_key(
    provider: str,
    *,
    env: Mapping[str, str] | None = None,
    registry: dict[str, ProviderSpec] | None = None,
) -> bool:
    """Remove the stored key for ``provider``. Returns True if one was removed."""
    data = {k: dict(v) for k, v in load_credentials(env).items()}
    if data.pop(provider, None) is None:
        return False
    _write(data, credentials_path(env))
    return True


def mask_key(api_key: str) -> str:
    """Return a display-safe fingerprint like ``****ab12``."""
    tail = api_key.strip()[-4:]
    return f"****{tail}" if tail else "****"
