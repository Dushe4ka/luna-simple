"""Async client for a running Luna server (``luna serve``)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from luna_core.errors import LunaCoreError
from luna_core.policy import Decision

_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)


class ServerError(LunaCoreError):
    """The server answered with an error or could not be reached.

    ``status`` is the HTTP status of an error answer, ``None`` when the server was unreachable.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        """Build the error; ``status`` is the HTTP status, if the server answered."""
        super().__init__(message)
        self.status = status


def _error(resp: httpx.Response) -> ServerError:
    try:
        message = resp.json().get("error") or resp.text
    except ValueError:
        message = resp.text or resp.reason_phrase
    return ServerError(f"HTTP {resp.status_code}: {message}", resp.status_code)


class LunaClient:
    """REST + SSE client for the Luna HTTP API (protocol v1)."""

    def __init__(
        self, base_url: str, token: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        """Build a client for ``base_url``, authenticating every request with ``token``."""
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=httpx.Timeout(30.0)
        )

    async def _json(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = await self._http.request(method, path, headers=self._headers, **kwargs)
        except httpx.TransportError as exc:
            raise ServerError(f"cannot reach the Luna server: {type(exc).__name__}") from exc
        if resp.is_error:
            raise _error(resp)
        return resp.json()

    async def _stream(self, method: str, path: str, **kwargs) -> AsyncIterator[dict]:
        try:
            async with self._http.stream(
                method, path, headers=self._headers, timeout=_STREAM_TIMEOUT, **kwargs
            ) as resp:
                if resp.is_error:
                    await resp.aread()
                    raise _error(resp)
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        yield json.loads(line[5:].strip())
        except httpx.TransportError as exc:
            raise ServerError(
                f"connection to the Luna server dropped: {type(exc).__name__}"
            ) from exc

    async def health(self) -> dict:
        """Server version and protocol."""
        return await self._json("GET", "/health")

    async def list_sessions(self, workdir: str) -> list[dict]:
        """Sessions of a project, newest first."""
        return (await self._json("GET", "/sessions", params={"workdir": workdir}))["sessions"]

    async def create_session(self, workdir: str) -> str:
        """Create a session; returns its id."""
        return (await self._json("POST", "/sessions", json={"workdir": workdir}))["id"]

    async def delete_session(self, session_id: str) -> None:
        """Delete a session."""
        await self._json("DELETE", f"/sessions/{session_id}")

    async def history(self, session_id: str, workdir: str) -> list[dict]:
        """Transcript entries, oldest first."""
        path = f"/sessions/{session_id}/messages"
        return (await self._json("GET", path, params={"workdir": workdir}))["messages"]

    async def state(self, session_id: str, workdir: str) -> dict:
        """Status-bar state of a session."""
        return await self._json("GET", f"/sessions/{session_id}/state", params={"workdir": workdir})

    def send(
        self,
        session_id: str,
        text: str,
        workdir: str,
        *,
        images: list[dict] | tuple = (),
        reload_after: bool = False,
    ) -> AsyncIterator[dict]:
        """Run a turn; yields wire events until the turn ends."""
        body: dict = {"content": text, "workdir": workdir}
        if images:
            body["images"] = list(images)
        if reload_after:
            body["reload_after"] = True
        return self._stream("POST", f"/sessions/{session_id}/messages", json=body)

    async def decide(self, session_id: str, call_id: str, decision: dict | Decision) -> None:
        """Answer an approval; the open turn stream continues."""
        wire = decision.to_dict() if isinstance(decision, Decision) else decision
        path = f"/sessions/{session_id}/approve"
        await self._json("POST", path, json={"call_id": call_id, "decision": wire})

    async def cancel(self, session_id: str) -> bool:
        """Cancel the running turn."""
        return bool((await self._json("POST", f"/sessions/{session_id}/cancel"))["cancelled"])

    async def commands(self, workdir: str) -> list[dict]:
        """Available slash commands."""
        return (await self._json("GET", "/commands", params={"workdir": workdir}))["commands"]

    async def run_command(self, session_id: str, line: str, workdir: str) -> dict:
        """Run a slash command."""
        path = f"/sessions/{session_id}/command"
        body = {"line": line, "workdir": workdir}
        return await self._json("POST", path, json=body, timeout=_STREAM_TIMEOUT)

    async def background(self, session_id: str, text: str, workdir: str) -> dict:
        """Run a turn detached from this client."""
        path = f"/sessions/{session_id}/background"
        return await self._json("POST", path, json={"content": text, "workdir": workdir})

    async def agents(self) -> dict:
        """Background runs and tasks."""
        return await self._json("GET", "/agents")

    def attach(self, session_id: str, *, after: int = -1) -> AsyncIterator[dict]:
        """Replay and follow a background run's events."""
        path = f"/sessions/{session_id}/events"
        return self._stream("GET", path, params={"after": after})

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()
