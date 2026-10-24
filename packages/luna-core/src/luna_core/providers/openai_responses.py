"""OpenAI Responses API adapter (stateless: ``store`` false, encrypted reasoning)."""

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
from luna_core.messages import Image, Message, Text, Thinking, ToolCall
from luna_core.providers.base import (
    ModelRequest,
    RetryPolicy,
    default_client,
    guard_stream,
    post_sse,
    with_retries,
)
from luna_core.usage import Usage


def _usage(raw: dict) -> Usage:
    return Usage(
        input=int(raw.get("input_tokens") or 0),
        output=int(raw.get("output_tokens") or 0),
        cache_read=int((raw.get("input_tokens_details") or {}).get("cached_tokens") or 0),
        reasoning=int((raw.get("output_tokens_details") or {}).get("reasoning_tokens") or 0),
    )


class OpenAIResponses:
    """Streams ``POST {base_url}/responses``."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://api.openai.com/v1",
        name: str = "openai_responses",
        client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.name = name
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client = client or default_client()
        self.retry = retry or RetryPolicy()

    def build_payload(self, request: ModelRequest) -> dict:
        """Build the JSON body for ``request``."""
        items: list[dict] = []
        for message in request.messages:
            items.extend(self._convert(message))
        body: dict = {
            "model": request.model,
            "input": items,
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if request.system:
            body["instructions"] = request.system
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in request.tools
            ]
        if request.reasoning:
            body["reasoning"] = {"effort": request.reasoning, "summary": "auto"}
        if request.max_tokens:
            body["max_output_tokens"] = request.max_tokens
        if request.temperature is not None and not request.reasoning:
            body["temperature"] = request.temperature
        return body

    def _convert(self, message: Message) -> list[dict]:
        if message.role == "user":
            parts: list[dict] = []
            for b in message.content:
                if isinstance(b, Text) and b.text:
                    parts.append({"type": "input_text", "text": b.text})
                elif isinstance(b, Image):
                    parts.append(
                        {"type": "input_image", "image_url": f"data:{b.media_type};base64,{b.data}"}
                    )
            return [{"role": "user", "content": parts}]
        if message.role == "assistant":
            out: list[dict] = []
            for b in message.content:
                if isinstance(b, Thinking) and b.provider == self.name and b.signature:
                    state = json.loads(b.signature)
                    summary = [{"type": "summary_text", "text": b.text}] if b.text else []
                    out.append(
                        {
                            "type": "reasoning",
                            "id": state["id"],
                            "encrypted_content": state["encrypted_content"],
                            "summary": summary,
                        }
                    )
                elif isinstance(b, Text) and b.text:
                    out.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": b.text}],
                        }
                    )
                elif isinstance(b, ToolCall):
                    out.append(
                        {
                            "type": "function_call",
                            "call_id": b.id,
                            "name": b.name,
                            "arguments": json.dumps(b.args, ensure_ascii=False),
                        }
                    )
            return out
        out = []
        images: list[dict] = []
        for r in message.results:
            out.append(
                {
                    "type": "function_call_output",
                    "call_id": r.call_id,
                    "output": r.text or "(no output)",
                }
            )
            images.extend(
                {"type": "input_image", "image_url": f"data:{p.media_type};base64,{p.data}"}
                for p in r.content
                if isinstance(p, Image)
            )
        if images:
            out.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Images returned by the tools above:"},
                        *images,
                    ],
                }
            )
        return out

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream one response (retrying transient failures before output)."""
        async for event in with_retries(
            lambda: guard_stream(self.name, self._once(request)), self.retry
        ):
            yield event

    async def _once(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        headers = {"Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        saw_call = False
        stop = "end"
        url = f"{self.base_url}/responses"
        async for _name, data in post_sse(
            self.client, url, headers=headers, json=self.build_payload(request)
        ):
            event = json.loads(data)
            kind = event.get("type", "")
            if kind == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    saw_call = True
                    yield ToolCallStart(int(event["output_index"]), item["call_id"], item["name"])
            elif kind == "response.output_text.delta":
                yield TextDelta(event.get("delta", ""))
            elif kind == "response.reasoning_summary_text.delta":
                yield ThinkingDelta(event.get("delta", ""))
            elif kind == "response.function_call_arguments.delta":
                yield ToolCallDelta(int(event["output_index"]), event.get("delta", ""))
            elif kind == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "reasoning" and item.get("encrypted_content"):
                    state = {"id": item["id"], "encrypted_content": item["encrypted_content"]}
                    yield ThinkingDelta("", signature=json.dumps(state))
            elif kind in ("response.completed", "response.incomplete"):
                response = event.get("response") or {}
                yield UsageReport(_usage(response.get("usage") or {}))
                reason = (response.get("incomplete_details") or {}).get("reason")
                if response.get("status") == "incomplete":
                    stop = "max_tokens" if reason == "max_output_tokens" else "refusal"
            elif kind == "response.failed":
                err = (event.get("response") or {}).get("error") or {}
                raise ProviderError(f"response failed: {err.get('message', err)}", "server")
            elif kind == "error":
                raise ProviderError(f"stream error: {event.get('message', event)}", "server")
        if stop == "end" and saw_call:
            stop = "tool_use"
        yield StopEvent(stop)
