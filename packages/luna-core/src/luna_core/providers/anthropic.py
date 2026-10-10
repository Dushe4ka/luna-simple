"""Anthropic Messages API adapter: prompt caching, thinking blocks, tool use."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

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
from luna_core.messages import Image, Message, Text, Thinking, ToolCall, ToolResult
from luna_core.providers.base import (
    ModelRequest,
    RetryPolicy,
    default_client,
    post_sse,
    with_retries,
)
from luna_core.usage import Usage

_CACHE = {"type": "ephemeral"}
_STOP = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "pause_turn": "end",
}
_ERROR_KINDS = {
    "overloaded_error": "server",
    "api_error": "server",
    "rate_limit_error": "rate_limit",
    "authentication_error": "auth",
    "permission_error": "auth",
    "invalid_request_error": "bad_request",
}


def _image(image: Image) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": image.media_type, "data": image.data},
    }


class AnthropicMessages:
    """Streams ``POST {base_url}/v1/messages``."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://api.anthropic.com",
        name: str = "anthropic",
        client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
        default_max_tokens: int = 16000,
        thinking_budgets: dict | None = None,
    ) -> None:
        self.name = name
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client = client or default_client()
        self.retry = retry or RetryPolicy()
        self.default_max_tokens = default_max_tokens
        self.thinking_budgets = thinking_budgets or {"low": 2048, "medium": 8192, "high": 24000}

    def build_payload(self, request: ModelRequest) -> dict:
        """Build the JSON body for ``request``."""
        messages: list[dict] = []
        for message in request.messages:
            role, blocks = self._convert(message)
            if not blocks:
                continue
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})
        budget = self.thinking_budgets.get(request.reasoning or "", 0)
        max_tokens = request.max_tokens or self.default_max_tokens
        if budget and max_tokens <= budget:
            max_tokens = budget + 4096
        body: dict = {
            "model": request.model,
            "max_tokens": max_tokens,
            "messages": messages,
            "stream": True,
        }
        if request.system:
            system = {"type": "text", "text": request.system}
            if request.cache:
                system["cache_control"] = _CACHE
            body["system"] = [system]
        if request.tools:
            tools = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
            if request.cache:
                tools[-1]["cache_control"] = _CACHE
            body["tools"] = tools
        if budget:
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif request.temperature is not None:
            body["temperature"] = request.temperature
        if request.cache and messages:
            last = messages[-1]
            if last["role"] == "user" and last["content"]:
                last["content"][-1] = {**last["content"][-1], "cache_control": _CACHE}
        return body

    def _convert(self, message: Message) -> tuple[str, list[dict]]:
        if message.role == "user":
            blocks: list[dict] = []
            for b in message.content:
                if isinstance(b, Text) and b.text:
                    blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, Image):
                    blocks.append(_image(b))
            return "user", blocks
        if message.role == "assistant":
            blocks = []
            for b in message.content:
                if isinstance(b, Thinking):
                    if b.provider != self.name or not b.signature:
                        continue
                    if b.text:
                        blocks.append(
                            {"type": "thinking", "thinking": b.text, "signature": b.signature}
                        )
                    else:  # redacted thinking: the signature holds the encrypted data
                        blocks.append({"type": "redacted_thinking", "data": b.signature})
                elif isinstance(b, Text) and b.text:
                    blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, ToolCall):
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.args})
            return "assistant", blocks
        blocks = []
        for r in message.content:
            if isinstance(r, ToolResult):
                parts = [
                    {"type": "text", "text": p.text} if isinstance(p, Text) else _image(p)
                    for p in r.content
                ]
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": parts or [{"type": "text", "text": "(no output)"}],
                        "is_error": r.is_error,
                    }
                )
        return "user", blocks

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream one message (retrying transient failures before output)."""
        async for event in with_retries(lambda: self._once(request), self.retry):
            yield event

    async def _once(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        headers = {"anthropic-version": "2023-06-01", "accept": "text/event-stream"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        kinds: dict[int, str] = {}
        stop = "end"
        input_usage = Usage()
        url = f"{self.base_url}/v1/messages"
        async for _name, data in post_sse(
            self.client, url, headers=headers, json=self.build_payload(request)
        ):
            event = json.loads(data)
            kind = event.get("type")
            if kind == "message_start":
                u = (event.get("message") or {}).get("usage") or {}
                cache_read = int(u.get("cache_read_input_tokens") or 0)
                cache_write = int(u.get("cache_creation_input_tokens") or 0)
                input_usage = Usage(
                    input=int(u.get("input_tokens") or 0) + cache_read + cache_write,
                    cache_read=cache_read,
                    cache_write=cache_write,
                )
            elif kind == "content_block_start":
                index = int(event["index"])
                block = event.get("content_block") or {}
                kinds[index] = block.get("type", "")
                if kinds[index] == "tool_use":
                    yield ToolCallStart(index, block["id"], block["name"])
                elif kinds[index] == "redacted_thinking":
                    yield ThinkingDelta("", signature=block.get("data"))
            elif kind == "content_block_delta":
                index = int(event["index"])
                delta = event.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    yield TextDelta(delta.get("text", ""))
                elif dtype == "thinking_delta":
                    yield ThinkingDelta(delta.get("thinking", ""))
                elif dtype == "signature_delta":
                    yield ThinkingDelta("", signature=delta.get("signature"))
                elif dtype == "input_json_delta":
                    yield ToolCallDelta(index, delta.get("partial_json", ""))
            elif kind == "message_delta":
                stop = _STOP.get((event.get("delta") or {}).get("stop_reason") or "", stop)
                out = int((event.get("usage") or {}).get("output_tokens") or 0)
                yield UsageReport(
                    Usage(input_usage.input, out, input_usage.cache_read, input_usage.cache_write)
                )
            elif kind == "error":
                err = event.get("error") or {}
                raise ProviderError(
                    f"stream error: {err.get('message', err)}",
                    _ERROR_KINDS.get(err.get("type", ""), "unknown"),
                )
        yield StopEvent(stop)
