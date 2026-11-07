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
from luna.config.credentials import BadApiKeyError, check_api_key, set_api_key
from luna.config.providers import PROVIDERS, LunaConfigError, merge_providers
from luna.server.trust import trust_error

#: A key is well under 1 KiB; anything bigger is refused before it is read in full.
MAX_BODY = 8 * 1024


async def _read_capped(request: Request) -> bytes | None:
    """Return the body, or ``None`` when it exceeds :data:`MAX_BODY`."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


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
    raw = await _read_capped(request)
    if raw is None:
        return _error("too_large", 413)
    try:
        body = json.loads(raw)
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
    if not key.strip():
        return _error("empty_key")
    try:
        key = check_api_key(key)
    except BadApiKeyError:
        return _error("bad_key")
    try:
        await asyncio.to_thread(set_api_key, provider, key, registry=registry)
    except Exception:  # noqa: BLE001 - only a code goes back: a message could quote the key
        return _error("cannot_store", 500)
    finally:
        del key
    return JSONResponse({"ok": True, "provider": provider})
