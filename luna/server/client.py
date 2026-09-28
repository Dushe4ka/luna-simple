"""Async HTTP+SSE client for talking to the local Luna server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

#: A turn streams for as long as the agent works: the model can stay silent
#: well past httpx's default 5 s read timeout (sse-starlette only pings every
#: 15 s), so reads never time out; connecting still does.
_STREAM_TIMEOUT = httpx.Timeout(5.0, read=None)

#: Human-readable text for the server's machine-readable error codes.
_ERROR_TEXT = {
    "workdir_not_trusted": (
        "Папка не отмечена как доверенная. Перезапустите luna в ней и подтвердите доверие."
    ),
    "workdir_not_absolute": "Путь к папке проекта должен быть абсолютным.",
    "workdir_required": "Не указана папка проекта.",
}


class ServerError(Exception):
    """A request to the local server failed; ``str()`` is safe to show to the user."""


def _server_error(resp: httpx.Response) -> ServerError:
    try:
        code = resp.json().get("error", "")
    except (ValueError, AttributeError):
        code = ""
    fallback = f"Сервер Luna ответил {resp.status_code} {code}".rstrip()
    return ServerError(_ERROR_TEXT.get(code) or fallback)


def _check(resp: httpx.Response) -> None:
    if resp.is_error:
        raise _server_error(resp)


class ServerClient:
    """Thin async client over the local server's REST+SSE endpoints.

    HTTP failures surface as :class:`ServerError` with a readable message, so
    the TUI can show them instead of crashing on an ``HTTPStatusError``.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"}
        )

    def _auth_headers(self) -> dict[str, str]:
        # Attached explicitly (not just relied on as self._http's default
        # headers) so auth still works if a caller swaps out self._http
        # for a differently-configured client, e.g. a test using
        # httpx.ASGITransport in-process.
        return {"Authorization": f"Bearer {self._token}"}

    async def list_sessions(self, workdir: str) -> list[dict]:
        """Return the session list for ``workdir``, newest first."""
        resp = await self._http.get(
            "/sessions", params={"workdir": workdir}, headers=self._auth_headers()
        )
        _check(resp)
        return resp.json()["sessions"]

    async def create_session(self, workdir: str) -> str:
        """Create a new session and return its thread_id."""
        resp = await self._http.post(
            "/sessions", json={"workdir": workdir}, headers=self._auth_headers()
        )
        _check(resp)
        return resp.json()["thread_id"]

    async def get_history(self, thread_id: str, workdir: str) -> list[dict]:
        """Return the thread's prior human/assistant turns, oldest first."""
        resp = await self._http.get(
            f"/sessions/{thread_id}/messages",
            params={"workdir": workdir},
            headers=self._auth_headers(),
        )
        _check(resp)
        return resp.json()["messages"]

    async def _stream(self, path: str, body: dict) -> AsyncIterator[dict]:
        try:
            async with self._http.stream(
                "POST", path, json=body, headers=self._auth_headers(), timeout=_STREAM_TIMEOUT
            ) as resp:
                if resp.is_error:
                    await resp.aread()  # a streamed body must be read before .json()
                    raise _server_error(resp)
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        yield json.loads(line[len("data:") :].strip())
        except httpx.TransportError as exc:
            raise ServerError(f"Связь с сервером Luna прервалась: {type(exc).__name__}") from exc

    def send_message(self, thread_id: str, content: str, workdir: str) -> AsyncIterator[dict]:
        """Send a user message; yields parsed SSE event dicts."""
        return self._stream(
            f"/sessions/{thread_id}/messages", {"content": content, "workdir": workdir}
        )

    def approve(self, thread_id: str, decision: dict, workdir: str) -> AsyncIterator[dict]:
        """Resume a paused turn with ``decision``; yields parsed SSE event dicts."""
        return self._stream(
            f"/sessions/{thread_id}/approve", {"decision": decision, "workdir": workdir}
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()
