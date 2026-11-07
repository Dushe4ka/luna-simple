"""``POST /credentials``: store a provider API key out of band.

The key travels only in this request's body — never as a slash-command line,
so it never reaches the transcript, the event log or any history. It is
written with :func:`luna.config.credentials.set_api_key` (0600 file, the same
as ``luna config set-key``), never exported to ``os.environ`` and never logged:
error answers carry only a machine-readable code.
"""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.config.config import load_config
from luna.config.credentials import set_api_key
from luna.config.providers import PROVIDERS, LunaConfigError, merge_providers
from luna.server.trust import trust_error


def _error(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _registry(workdir: str | None) -> dict:
    """Built-in providers plus the custom ones visible from ``workdir``."""
    try:
        cfg = load_config(cwd=workdir) if workdir else load_config()
    except (LunaConfigError, OSError):
        return dict(PROVIDERS)
    return merge_providers(cfg.custom_providers)


async def post_credentials(request: Request) -> JSONResponse:
    """Validate and store ``{"provider", "key"}``; answer ``{"ok", "provider"}``."""
    try:
        body = json.loads(await request.body())
    except ValueError:
        return _error("bad_request")
    if not isinstance(body, dict):
        return _error("bad_request")
    provider, key, workdir = body.get("provider"), body.get("key"), body.get("workdir")
    if not isinstance(provider, str) or not isinstance(key, str):
        return _error("bad_request")
    if workdir is not None and not isinstance(workdir, str):
        return _error("bad_request")
    if (error := trust_error(request, workdir)) is not None:
        return error
    registry = await asyncio.to_thread(_registry, workdir)
    spec = registry.get(provider)
    if spec is None:
        return _error("unknown_provider")
    if not spec.env_var:
        return _error("key_not_needed")
    key = key.strip()
    if not key:
        return _error("empty_key")
    try:
        await asyncio.to_thread(set_api_key, provider, key, registry=registry)
    except (LunaConfigError, OSError):
        return _error("cannot_store", 500)
    return JSONResponse({"ok": True, "provider": provider})
