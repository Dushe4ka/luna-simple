"""Live + offline model-id discovery per provider (best-effort, never raises).

``list_models`` tries a provider's own API; ``known_models`` reads the
packaged offline fallback (``known_models.toml``, same loading pattern as
``luna/config/usage.py``'s ``models.toml``). Neither ever raises — a
closed-network environment or an unsupported provider must never hang or
crash the caller, only fall back.
"""

from __future__ import annotations

import os
import tomllib
from importlib import resources

import httpx

from luna.config.providers import ProviderSpec

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


_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"
_GOOGLE_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"

# Base URLs for providers reached via their own native LangChain
# integration rather than Luna's base_url passthrough mechanism (which
# already carries a base_url on their ProviderSpec, e.g. Cerebras and
# any [provider.custom.*] entry). Verified against each provider's docs
# on 2026-09-15.
#
# Fireworks is a documented exception: its real list-models endpoint is
# https://api.fireworks.ai/v1/accounts/{account_id}/models, which needs
# an account id Luna never collects. The generic GET {base}/models call
# below will not resolve to that endpoint, so list_models() returns None
# for Fireworks and callers fall back to known_models("fireworks") —
# accepted, not a bug.
_OPENAI_COMPATIBLE_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "together": "https://api.together.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
    "perplexity": "https://api.perplexity.ai/v1",
}


def _safe_get_json(url: str, headers: dict, *, timeout: float):
    """GET url, return parsed JSON, or None on any failure. Never raises."""
    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _ids_from(items, key: str) -> list[str]:
    return [item[key] for item in items if isinstance(item, dict) and key in item]


def list_models(
    provider: str,
    spec: ProviderSpec,
    *,
    api_key: str | None,
    timeout: float = 3.0,
) -> list[str] | None:
    """Live model ids for ``provider``, or ``None`` on any failure.

    Never raises. ``api_key`` may be ``None`` only when ``spec.env_var``
    is also ``None`` (Ollama, or a keyless custom endpoint) — such a
    provider is queried with no auth header.
    """
    if provider == "anthropic":
        if not api_key:
            return None
        data = _safe_get_json(
            _ANTHROPIC_MODELS_URL,
            {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION},
            timeout=timeout,
        )
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            return None
        return _ids_from(data["data"], "id")

    if provider == "google":
        if not api_key:
            return None
        data = _safe_get_json(_GOOGLE_MODELS_URL, {"x-goog-api-key": api_key}, timeout=timeout)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            return None
        names = _ids_from(data["models"], "name")
        return [n.removeprefix("models/") for n in names]

    if provider == "ollama":
        host = os.environ.get("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST).rstrip("/")
        data = _safe_get_json(f"{host}/api/tags", {}, timeout=timeout)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            return None
        return _ids_from(data["models"], "name")

    base = spec.base_url or _OPENAI_COMPATIBLE_BASE_URLS.get(provider)
    if base is None:
        return None
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = _safe_get_json(f"{base.rstrip('/')}/models", headers, timeout=timeout)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    return _ids_from(data["data"], "id")
