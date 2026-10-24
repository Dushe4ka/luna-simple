"""Shared provider machinery: request type, retries, SSE transport, accumulation."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from httpx_sse import aconnect_sse

from luna_core.errors import ProviderError
from luna_core.events import (
    ModelEvent,
    StopEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallStart,
    UsageReport,
)
from luna_core.messages import Block, Message, Text, Thinking, ToolCall
from luna_core.tools.base import ToolSpec
from luna_core.usage import Usage


@dataclass
class ModelRequest:
    """One model call."""

    model: str
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning: str | None = None  # None | "low" | "medium" | "high"
    cache: bool = True


class Provider(Protocol):
    """A model provider adapter."""

    name: str

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream the model's response as :data:`ModelEvent`s."""
        ...


@dataclass(frozen=True)
class RetryPolicy:
    """Retry transient failures that happen before any output arrived."""

    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0


async def with_retries(
    open_stream: Callable[[], AsyncIterator[ModelEvent]],
    policy: RetryPolicy,
    sleep: Callable[[float], object] = asyncio.sleep,
) -> AsyncIterator[ModelEvent]:
    """Yield from ``open_stream()``, retrying retryable errors before the first event.

    Once any event was yielded the error propagates: retrying then would
    duplicate text the caller has already shown.
    """
    attempt = 0
    while True:
        started = False
        try:
            async for event in open_stream():
                started = True
                yield event
            return
        except ProviderError as exc:
            if started or not exc.retryable or attempt + 1 >= policy.attempts:
                raise
            delay = exc.retry_after or min(policy.max_delay, policy.base_delay * 2**attempt)
            attempt += 1
            result = sleep(delay)
            if asyncio.iscoroutine(result):
                await result


_CONTEXT_MARKERS = (
    "context length",
    "context window",
    "prompt is too long",
    "too many tokens",
    "maximum context",
    "context_length_exceeded",
    "input is too long",
)


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:500] or "no response body"
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        return str(err.get("message") or err.get("msg") or err)[:500]
    if isinstance(data, dict) and "message" in data:
        return str(data["message"])[:500]
    return str(err)[:500]


def error_from_response(status: int, body: str, headers: Mapping[str, str]) -> ProviderError:
    """Classify a failed HTTP response."""
    message = _error_message(body)
    lowered = message.lower()
    retry_after = None
    raw = headers.get("retry-after") if headers else None
    if raw:
        try:
            retry_after = float(raw)
        except ValueError:
            retry_after = None
    if status in (401, 403):
        kind = "auth"
    elif status == 429:
        kind = "rate_limit"
    elif status in (400, 413, 422) and any(m in lowered for m in _CONTEXT_MARKERS):
        kind = "context_length"
    elif 400 <= status < 500:
        kind = "bad_request"
    elif status >= 500:
        kind = "server"
    else:
        kind = "unknown"
    return ProviderError(f"HTTP {status}: {message}", kind, status, retry_after)


def default_client() -> httpx.AsyncClient:
    """Build an HTTP client tuned for long model streams."""
    return httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0))


async def post_sse(
    client: httpx.AsyncClient, url: str, *, headers: Mapping[str, str], json: dict
) -> AsyncIterator[tuple[str, str]]:
    """POST ``json`` and yield ``(event, data)`` from the SSE response."""
    try:
        async with aconnect_sse(client, "POST", url, headers=dict(headers), json=json) as source:
            response = source.response
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise error_from_response(response.status_code, body, response.headers)
            async for sse in source.aiter_sse():
                yield sse.event, sse.data
    except httpx.TimeoutException as exc:
        raise ProviderError(f"request timed out: {exc}", "network") from exc
    except httpx.TransportError as exc:
        raise ProviderError(f"network error: {exc}", "network") from exc


# What parsing an unexpected stream shape raises (``json.JSONDecodeError`` is a
# ``ValueError``); :func:`guard_stream` turns these into :class:`ProviderError`.
MALFORMED_ERRORS = (KeyError, TypeError, IndexError, ValueError, AttributeError)


def malformed(detail: str) -> ProviderError:
    """Build the error for a stream event whose shape the adapter cannot use.

    ``kind="server"``: the provider (or a proxy in between) sent it, and a
    retry before any output may well get a sound stream.
    """
    return ProviderError(f"malformed stream event ({detail})", "server")


def _event_problem(event: ModelEvent, opened: set[int]) -> str | None:
    """Describe why ``event`` has the wrong field types, or ``None`` when it is sound."""
    if isinstance(event, TextDelta) and not isinstance(event.text, str):
        return f"text is {type(event.text).__name__}, not str"
    if isinstance(event, ThinkingDelta) and (
        not isinstance(event.text, str)
        or not (event.signature is None or isinstance(event.signature, str))
    ):
        return "thinking text/signature is not a string"
    if isinstance(event, ToolCallStart):
        if not isinstance(event.index, int) or not isinstance(event.extra, dict):
            return "tool call index/extra has the wrong type"
        if not isinstance(event.id, str) or not isinstance(event.name, str):
            return "tool call id/name is not a string"
        opened.add(event.index)
    elif isinstance(event, ToolCallDelta):
        if not isinstance(event.args_json, str):
            return "tool call arguments are not a string"
        if event.index not in opened:
            return f"arguments for tool call {event.index!r}, which never started"
    return None


async def guard_stream(
    provider_name: str, events: AsyncIterator[ModelEvent]
) -> AsyncIterator[ModelEvent]:
    """Pass ``events`` through, turning malformed stream shapes into :class:`ProviderError`.

    Adapters parse provider JSON with plain indexing; an unexpected shape
    (missing key, wrong type, non-JSON data) raises one of
    :data:`MALFORMED_ERRORS` inside ``events``, and a wrong-typed field
    would only blow up later. Both become ``ProviderError`` here, as does a
    tool-call delta for an index that was never started.
    """
    opened: set[int] = set()
    async with contextlib.aclosing(events) as stream:
        try:
            async for event in stream:
                problem = _event_problem(event, opened)
                if problem is not None:
                    raise malformed(problem)
                yield event
        except MALFORMED_ERRORS as exc:
            raise malformed(f"{type(exc).__name__}: {exc}") from exc


class StreamAccumulator:
    """Assembles model events into the assistant :class:`Message`, in order."""

    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name
        self.usage = Usage()
        self.stop_reason = "end"
        self._segments: list[list] = []  # [kind, payload...]
        self._calls: dict[int, list] = {}

    def feed(self, event: ModelEvent) -> None:
        """Consume one event."""
        if isinstance(event, TextDelta):
            if self._segments and self._segments[-1][0] == "text":
                self._segments[-1][1].append(event.text)
            else:
                self._segments.append(["text", [event.text]])
        elif isinstance(event, ThinkingDelta):
            last = self._segments[-1] if self._segments else None
            if last is not None and last[0] == "thinking" and last[2] is None:
                last[1].append(event.text)
                last[2] = event.signature
            else:
                self._segments.append(["thinking", [event.text], event.signature])
        elif isinstance(event, ToolCallStart):
            call = ["call", event.id, event.name, [], dict(event.extra)]
            self._calls[event.index] = call
            self._segments.append(call)
        elif isinstance(event, ToolCallDelta):
            call = self._calls.get(event.index)
            if call is None:
                raise malformed(f"arguments for tool call {event.index!r}, which never started")
            call[3].append(event.args_json)
        elif isinstance(event, UsageReport):
            self.usage = self.usage + event.usage
        elif isinstance(event, StopEvent):
            self.stop_reason = event.reason

    def message(self) -> Message:
        """Return the assistant message so far."""
        blocks: list[Block] = []
        for seg in self._segments:
            if seg[0] == "text":
                blocks.append(Text("".join(seg[1])))
            elif seg[0] == "thinking":
                blocks.append(Thinking("".join(seg[1]), seg[2], self.provider_name))
            else:
                raw = "".join(seg[3]) or "{}"
                try:
                    args = json.loads(raw)
                    if not isinstance(args, dict):
                        args = {"__invalid_json__": raw}
                except ValueError:
                    args = {"__invalid_json__": raw}
                blocks.append(ToolCall(seg[1], seg[2], args, seg[4]))
        return Message.assistant(blocks)
