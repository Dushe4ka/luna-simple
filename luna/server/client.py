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
    "session_busy": "Дождитесь окончания ответа, затем повторите.",
    "client_command": "Эту команду выполняет сам TUI.",
    "workdir_mismatch": "Сессия принадлежит другой папке.",
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


#: History roles as the TUI's transcript renderer names them.
_ROLES = {"user": "human", "assistant": "ai"}


class ServerClient:
    """Thin async client over the local server's REST+SSE endpoints (protocol v1).

    HTTP failures surface as :class:`ServerError` with a readable message, so
    the TUI can show them instead of crashing on an ``HTTPStatusError``.

    It keeps the TUI's original call shapes until the TUI moves to
    ``luna_core.client.LunaClient``: rows also carry ``thread_id``, history
    uses the ``human``/``ai`` roles, and a turn stream *ends* at an
    ``approval_needed`` event — :meth:`approve` then answers it with the
    request's ``call_id`` and follows the same run through
    ``GET /sessions/{id}/events`` from the last event seen.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"}
        )
        #: per session: index of the last run event seen, and the pending call ids
        self._last_index: dict[str, int] = {}
        self._pending: dict[str, list[str]] = {}

    def _auth_headers(self) -> dict[str, str]:
        # Attached explicitly (not just relied on as self._http's default
        # headers) so auth still works if a caller swaps out self._http
        # for a differently-configured client, e.g. a test using
        # httpx.ASGITransport in-process.
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One non-streaming request; every failure becomes a readable ``ServerError``."""
        try:
            resp = await self._http.request(method, path, headers=self._auth_headers(), **kwargs)
        except httpx.TransportError as exc:
            raise ServerError(f"Связь с сервером Luna прервалась: {type(exc).__name__}") from exc
        _check(resp)
        return resp

    async def list_sessions(self, workdir: str) -> list[dict]:
        """Return the session list for ``workdir``, newest first."""
        resp = await self._request("GET", "/sessions", params={"workdir": workdir})
        return [{**row, "thread_id": row["id"]} for row in resp.json()["sessions"]]

    async def create_session(self, workdir: str) -> str:
        """Create a new session and return its thread_id."""
        resp = await self._request("POST", "/sessions", json={"workdir": workdir})
        return resp.json()["id"]

    async def get_history(self, thread_id: str, workdir: str) -> list[dict]:
        """Return the thread's prior turns, oldest first."""
        resp = await self._request(
            "GET", f"/sessions/{thread_id}/messages", params={"workdir": workdir}
        )
        return [{**m, "role": _ROLES.get(m["role"], m["role"])} for m in resp.json()["messages"]]

    async def run_command(self, thread_id: str, line: str, workdir: str) -> dict:
        """Run a slash command; returns the structured CommandResult JSON."""
        resp = await self._request(
            "POST",
            f"/sessions/{thread_id}/command",
            json={"line": line, "workdir": workdir},
            timeout=_STREAM_TIMEOUT,  # /compact and /verify can take a while
        )
        return resp.json()

    async def list_commands(self, workdir: str) -> list[dict]:
        """Built-in + project slash commands."""
        resp = await self._request("GET", "/commands", params={"workdir": workdir})
        return resp.json()["commands"]

    async def get_state(self, thread_id: str, workdir: str) -> dict:
        """Session settings for the status bar."""
        resp = await self._request(
            "GET", f"/sessions/{thread_id}/state", params={"workdir": workdir}
        )
        return resp.json()

    async def _stream(
        self, thread_id: str, method: str, path: str, **kwargs
    ) -> AsyncIterator[dict]:
        """Yield a turn's events; stop after an ``approval_needed`` (see the class doc)."""
        try:
            async with self._http.stream(
                method, path, headers=self._auth_headers(), timeout=_STREAM_TIMEOUT, **kwargs
            ) as resp:
                if resp.is_error:
                    await resp.aread()  # a streamed body must be read before .json()
                    raise _server_error(resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[len("data:") :].strip())
                    if "index" in event:
                        self._last_index[thread_id] = event["index"]
                    yield event
                    if event["event"] == "approval_needed":
                        requests = event["value"].get("action_requests") or []
                        self._pending[thread_id] = [r["call_id"] for r in requests]
                        return
        except httpx.TransportError as exc:
            raise ServerError(f"Связь с сервером Luna прервалась: {type(exc).__name__}") from exc

    def send_message(
        self, thread_id: str, content: str, workdir: str, reload_after: bool = False
    ) -> AsyncIterator[dict]:
        """Send a user message; yields parsed SSE event dicts.

        ``reload_after`` asks the server to rebuild the session's agent once
        this turn finishes (``/init`` writes AGENTS.md, which is read at build).
        """
        body = {"content": content, "workdir": workdir}
        if reload_after:
            body["reload_after"] = True
        self._last_index.pop(thread_id, None)
        return self._stream(thread_id, "POST", f"/sessions/{thread_id}/messages", json=body)

    async def approve(self, thread_id: str, decision: dict, workdir: str) -> AsyncIterator[dict]:
        """Answer the paused turn with ``decision``; yields the rest of its events."""
        pending = self._pending.pop(thread_id, [])
        if not pending:
            raise ServerError("Нет запроса, ожидающего подтверждения.")
        await self._request(
            "POST",
            f"/sessions/{thread_id}/approve",
            json={"call_id": pending[0], "decision": decision},
        )
        after = self._last_index.get(thread_id, -1)
        async for event in self._stream(
            thread_id, "GET", f"/sessions/{thread_id}/events", params={"after": after}
        ):
            yield event

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()
