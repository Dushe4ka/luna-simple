r"""Local API-key storage: ``$XDG_CONFIG_HOME/luna/credentials.toml`` (mode 0600).

On Windows the directory is ``%APPDATA%\luna``.

Kept separate from ``config.toml`` so the config file stays safe to share or
commit. Environment variables always take precedence over stored keys.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

from luna_core.filelock import file_lock
from luna_core.fsperm import restrict

from luna.config.config import config_dir
from luna.config.providers import PROVIDERS, LunaConfigError, ProviderSpec, normalize_base_url

#: Provider names a credentials table may have (written quoted either way).
_PROVIDER_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class BadApiKeyError(LunaConfigError):
    """The key has characters an API key never has (control, whitespace, non-ASCII)."""


def check_api_key(api_key: str) -> str:
    """Return ``api_key`` stripped; raise unless it is non-empty printable ASCII.

    Whitespace inside, control characters, non-ASCII letters and lone
    surrogates are refused: none occurs in a real key, and a newline or a
    control character would corrupt the credentials file.
    """
    key = api_key.strip()
    if not key:
        raise LunaConfigError("API key must not be empty.")
    if not all("!" <= ch <= "~" for ch in key):
        raise BadApiKeyError("API key may contain only printable ASCII characters without spaces.")
    return key


def _lock_path(env: Mapping[str, str] | None) -> Path:
    path = credentials_path(env)
    return path.with_name(path.name + ".lock")


def write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` in a file that is 0600 from birth.

    The text is encoded before anything touches the disk, then written to a
    fresh temporary file in the same directory (``mkstemp``: 0600, ``O_EXCL``,
    never follows a symlink), flushed with ``fsync`` and moved over ``path``
    with ``os.replace``. A failure at any step leaves the old file intact.
    A symlinked ``path`` (dotfiles managers such as stow) keeps its link: the
    replace happens next to the link's real target.
    """
    data = text.encode("utf-8")
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            restrict(fd)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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


def _write(data: Mapping[str, Mapping[str, object]], path: Path) -> None:
    lines: list[str] = []
    for provider, values in data.items():
        _emit_table([provider], values, lines)
    write_private(path, "\n".join(lines).rstrip() + "\n")


def _emit_table(names: list[str], values: Mapping[str, object], lines: list[str]) -> None:
    """Write one table and its sub-tables (a legacy ``[my.llm]`` stays nested)."""
    scalars = [(k, _scalar(v)) for k, v in values.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in values.items() if isinstance(v, dict)]
    if scalars or not tables:
        lines.append("[" + ".".join(_quote(n) for n in names) + "]")
        # a value TOML can hold but we cannot write back (arrays, dates) is dropped
        lines += [f"{_quote(k)} = {v}" for k, v in scalars if v is not None]
        lines.append("")
    for key, sub in tables:
        _emit_table([*names, key], sub, lines)


def _scalar(value: object) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    return None


def _quote(text: str) -> str:
    """Return ``text`` as a TOML basic string (control characters escaped)."""
    out = []
    for ch in text:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def set_api_key(
    provider: str,
    api_key: str,
    *,
    env: Mapping[str, str] | None = None,
    registry: dict[str, ProviderSpec] | None = None,
) -> Path:
    """Store ``api_key`` for ``provider``. Returns the credentials file path.

    The read-modify-write runs under an inter-process file lock, so a
    concurrent ``luna config set-key`` and ``POST /credentials`` never lose
    each other's key.
    """
    reg = PROVIDERS if registry is None else registry
    if provider not in reg:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(sorted(reg))}."
        )
    if not _PROVIDER_NAME.fullmatch(provider):
        raise LunaConfigError(
            f"Provider name {provider!r} may contain only letters, digits, '_', '.' and '-'."
        )
    key = check_api_key(api_key)
    path = credentials_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(_lock_path(env)):
        data = {k: dict(v) for k, v in load_credentials(env).items()}
        section = data.setdefault(provider, {})
        section["api_key"] = key
        spec = reg[provider]
        if getattr(spec, "project", False):
            # a project provider's key is valid only for this endpoint
            section["base_url"] = normalize_base_url(spec.base_url)
        else:
            section.pop("base_url", None)
        _write(data, path)
    return path


def unset_api_key(
    provider: str,
    *,
    env: Mapping[str, str] | None = None,
    registry: dict[str, ProviderSpec] | None = None,
) -> bool:
    """Remove the stored key for ``provider``. Returns True if one was removed."""
    if not credentials_path(env).exists():
        return False
    with file_lock(_lock_path(env)):
        data = {k: dict(v) for k, v in load_credentials(env).items()}
        if data.pop(provider, None) is None:
            return False
        _write(data, credentials_path(env))
    return True


def mask_key(api_key: str) -> str:
    """Return a display-safe fingerprint like ``****ab12``."""
    tail = api_key.strip()[-4:]
    return f"****{tail}" if tail else "****"
