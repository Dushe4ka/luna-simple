"""Live + offline model-id discovery per provider (best-effort, never raises).

``list_models`` tries a provider's own API; ``known_models`` reads the
packaged offline fallback (``known_models.toml``, same loading pattern as
``luna/config/usage.py``'s ``models.toml``). Neither ever raises — a
closed-network environment or an unsupported provider must never hang or
crash the caller, only fall back.
"""

from __future__ import annotations

import tomllib
from importlib import resources

_KNOWN_MODELS_CACHE: dict | None = None


def _load_known_models() -> dict:
    """Read the packaged known-models registry once, caching the result."""
    global _KNOWN_MODELS_CACHE
    if _KNOWN_MODELS_CACHE is not None:
        return _KNOWN_MODELS_CACHE
    try:
        text = resources.files("luna.config").joinpath("known_models.toml").read_text()
        _KNOWN_MODELS_CACHE = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError, ModuleNotFoundError):
        _KNOWN_MODELS_CACHE = {}
    return _KNOWN_MODELS_CACHE


def known_models(provider: str) -> list[str]:
    """Return static fallback model ids for ``provider``, or ``[]`` if none are known."""
    entry = _load_known_models().get(provider)
    if not isinstance(entry, dict):
        return []
    models = entry.get("models")
    return list(models) if isinstance(models, list) else []
