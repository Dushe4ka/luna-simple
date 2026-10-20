"""GigaChat adapter: OAuth token exchange, one-function-per-reply calling."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable

import httpx

from luna_core.errors import ProviderError
from luna_core.events import (
    ModelEvent,
    StopEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallStart,
    UsageReport,
)
from luna_core.messages import Image, ToolCall
from luna_core.providers.base import (
    ModelRequest,
    RetryPolicy,
    error_from_response,
    post_sse,
    with_retries,
)
from luna_core.usage import Usage

_FINISH = {
    "stop": "end",
    "function_call": "tool_use",
    "length": "max_tokens",
    "blacklist": "refusal",
}
_REFRESH_MARGIN = 60.0


class GigaChat:
    """Streams GigaChat chat completions, managing the OAuth access token."""

    def __init__(
        self,
        *,
        credentials: str | None,
        scope: str = "GIGACHAT_API_PERS",
        base_url: str = "https://gigachat.devices.sberbank.ru/api/v1",
        auth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        name: str = "gigachat",
        client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
        ca_bundle: str | None = None,
        verify: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = name
        self.credentials = credentials
        self.scope = scope
        self.base_url = base_url.rstrip("/")
        self.auth_url = auth_url
        self.retry = retry or RetryPolicy()
        self.clock = clock
        if client is None:
            client = httpx.AsyncClient(
                verify=ca_bundle or verify,
                timeout=httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0),
            )
        self.client = client
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def token(self, *, force: bool = False) -> str:
        """Return a valid access token, fetched or refreshed as needed."""
        async with self._lock:
            if not force and self._token and self.clock() < self._expires - _REFRESH_MARGIN:
                return self._token
            if not self.credentials:
                raise ProviderError("GigaChat credentials are not set", "auth")
            headers = {
                "Authorization": f"Basic {self.credentials}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
            try:
                response = await self.client.post(
                    self.auth_url, headers=headers, content=f"scope={self.scope}"
                )
            except httpx.TransportError as exc:
                raise ProviderError(f"GigaChat OAuth network error: {exc}", "network") from exc
            if response.status_code >= 400:
                raise error_from_response(response.status_code, response.text, response.headers)
            data = response.json()
            expires = float(data.get("expires_at") or 0)
            self._expires = expires / 1000 if expires > 10**12 else expires
            self._token = data["access_token"]
            return self._token

    def build_payload(self, request: ModelRequest) -> dict:
        """Build the JSON body for ``request``."""
        messages: list[dict | None] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        pending: dict[str, int] = {}
        names: dict[str, str] = {}
        for message in request.messages:
            if message.role == "user":
                text = message.text
                if any(isinstance(b, Image) for b in message.content):
                    text = f"{text}\n[image omitted]" if text else "[image omitted]"
                messages.append({"role": "user", "content": text})
            elif message.role == "assistant":
                calls = message.tool_calls
                if not calls:
                    messages.append({"role": "assistant", "content": message.text})
                    continue
                self._append_calls(messages, calls, message.text, pending, names)
            else:
                for result in message.results:
                    index = pending.pop(result.call_id, None)
                    if index is None:
                        continue
                    messages[index] = {
                        "role": "function",
                        "name": result.name or names.get(result.call_id, ""),
                        "content": json.dumps(
                            {"result": result.text or "(no output)"}, ensure_ascii=False
                        ),
                    }
        body: dict = {
            "model": request.model,
            "messages": [m for m in messages if m is not None],
            "stream": True,
        }
        if request.tools:
            body["functions"] = [
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in request.tools
            ]
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        return body

    @staticmethod
    def _append_calls(
        messages: list[dict | None],
        calls: list[ToolCall],
        text: str,
        pending: dict[str, int],
        names: dict[str, str],
    ) -> None:
        # GigaChat allows one function call per assistant message; several
        # calls in one Luna message are replayed as call/result pairs in
        # order, each followed by a placeholder slot for its own result.
        for i, call in enumerate(calls):
            out: dict = {
                "role": "assistant",
                "content": text if i == 0 else "",
                "function_call": {"name": call.name, "arguments": call.args},
            }
            if call.extra.get("functions_state_id"):
                out["functions_state_id"] = call.extra["functions_state_id"]
            messages.append(out)
            names[call.id] = call.name
            pending[call.id] = len(messages)
            messages.append(None)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream one completion (retrying transient failures before output)."""
        async for event in with_retries(lambda: self._once(request), self.retry):
            yield event

    async def _once(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        try:
            async for event in self._attempt(request, await self.token()):
                yield event
        except ProviderError as exc:
            if exc.kind != "auth" or exc.status != 401 or not self._token:
                raise
            # Expired or revoked token: one forced refresh, then a single retry.
            async for event in self._attempt(request, await self.token(force=True)):
                yield event

    async def _attempt(self, request: ModelRequest, token: str) -> AsyncIterator[ModelEvent]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        stop = "end"
        started = False
        url = f"{self.base_url}/chat/completions"
        async for _name, data in post_sse(
            self.client, url, headers=headers, json=self.build_payload(request)
        ):
            if data.strip() == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("usage"):
                u = chunk["usage"]
                yield UsageReport(
                    Usage(
                        input=int(u.get("prompt_tokens") or 0),
                        output=int(u.get("completion_tokens") or 0),
                        cache_read=int(u.get("precached_prompt_tokens") or 0),
                    )
                )
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    yield TextDelta(delta["content"])
                fc = delta.get("function_call")
                if fc and not started:
                    started = True
                    extra = (
                        {"functions_state_id": delta["functions_state_id"]}
                        if delta.get("functions_state_id")
                        else {}
                    )
                    yield ToolCallStart(
                        0, f"call_{uuid.uuid4().hex[:12]}", fc.get("name", ""), extra
                    )
                    args = fc.get("arguments")
                    yield ToolCallDelta(
                        0,
                        args
                        if isinstance(args, str)
                        else json.dumps(args or {}, ensure_ascii=False),
                    )
                if choice.get("finish_reason"):
                    stop = _FINISH.get(choice["finish_reason"], "end")
        yield StopEvent(stop)
