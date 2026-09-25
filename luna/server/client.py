"""Async HTTP+SSE client for talking to the local Luna server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx


class ServerClient:
    """Thin async client over the local server's REST+SSE endpoints."""

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
        resp.raise_for_status()
        return resp.json()["sessions"]

    async def create_session(self, workdir: str) -> str:
        """Create a new session and return its thread_id."""
        resp = await self._http.post(
            "/sessions", json={"workdir": workdir}, headers=self._auth_headers()
        )
        resp.raise_for_status()
        return resp.json()["thread_id"]

    async def _stream(self, path: str, body: dict) -> AsyncIterator[dict]:
        async with self._http.stream("POST", path, json=body, headers=self._auth_headers()) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    yield json.loads(line[len("data:") :].strip())

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
