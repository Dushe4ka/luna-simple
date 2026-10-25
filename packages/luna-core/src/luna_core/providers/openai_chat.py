"""OpenAI Chat Completions adapter (also every OpenAI-compatible endpoint)."""

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
from luna_core.messages import Image, Message, Thinking
from luna_core.providers.base import (
    ModelRequest,
    RetryPolicy,
    default_client,
    guard_stream,
    post_sse,
    with_retries,
)
from luna_core.usage import Usage

_FINISH = {
    "stop": "end",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _image_part(image: Image) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{image.media_type};base64,{image.data}"},
    }


def parse_usage(raw: dict) -> Usage:
    """Normalise OpenAI-style usage (incl. DeepSeek cache fields)."""
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    cached = prompt_details.get("cached_tokens") or raw.get("prompt_cache_hit_tokens") or 0
    return Usage(
        input=int(raw.get("prompt_tokens") or 0),
        output=int(raw.get("completion_tokens") or 0),
        cache_read=int(cached),
        reasoning=int(completion_details.get("reasoning_tokens") or 0),
    )


class OpenAIChat:
    """Streams ``POST {base_url}/chat/completions``.

    ``send_reasoning`` returns earlier reasoning as ``reasoning_content``
    (DeepSeek thinking mode needs it inside a tool loop); ``reasoning_param``
    sends ``reasoning_effort`` for reasoning models.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://api.openai.com/v1",
        name: str = "openai_chat",
        client: httpx.AsyncClient | None = None,
        headers: dict | None = None,
        retry: RetryPolicy | None = None,
        send_reasoning: bool = False,
        reasoning_param: bool = False,
        max_tokens_field: str = "max_tokens",
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = client or default_client()
        self.headers = dict(headers or {})
        self.retry = retry or RetryPolicy()
        self.send_reasoning = send_reasoning
        self.reasoning_param = reasoning_param
        self.max_tokens_field = max_tokens_field

    def build_payload(self, request: ModelRequest) -> dict:
        """Build the JSON body for ``request``."""
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            messages.extend(self._convert(message))
        body: dict = {
            "model": request.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        if request.max_tokens:
            body[self.max_tokens_field] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.reasoning and self.reasoning_param:
            body["reasoning_effort"] = request.reasoning
        return body

    def _convert(self, message: Message) -> list[dict]:
        if message.role == "user":
            images = [b for b in message.content if isinstance(b, Image)]
            if not images:
                return [{"role": "user", "content": message.text}]
            parts: list[dict] = [{"type": "text", "text": message.text}] if message.text else []
            parts.extend(_image_part(i) for i in images)
            return [{"role": "user", "content": parts}]
        if message.role == "assistant":
            out: dict = {"role": "assistant", "content": message.text or None}
            thinking = [
                b for b in message.content if isinstance(b, Thinking) and b.provider == self.name
            ]
            if self.send_reasoning and thinking:
                out["reasoning_content"] = "".join(t.text for t in thinking)
            if message.tool_calls:
                out["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.args, ensure_ascii=False),
                        },
                    }
                    for c in message.tool_calls
                ]
            if out["content"] is None and "tool_calls" not in out:
                out["content"] = ""
            return [out]
        converted: list[dict] = []
        images: list[Image] = []
        for result in message.results:
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.text or "(no output)",
                }
            )
            images.extend(p for p in result.content if isinstance(p, Image))
        if images:
            parts = [{"type": "text", "text": "Images returned by the tools above:"}]
            parts.extend(_image_part(i) for i in images)
            converted.append({"role": "user", "content": parts})
        return converted

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream one completion (retrying transient failures before output)."""
        async for event in with_retries(
            lambda: guard_stream(self.name, self._once(request)), self.retry
        ):
            yield event

    async def _once(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        headers = {"Accept": "text/event-stream", **self.headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        started: set[int] = set()
        stop = "end"
        url = f"{self.base_url}/chat/completions"
        async for _event, data in post_sse(
            self.client, url, headers=headers, json=self.build_payload(request)
        ):
            if data.strip() == "[DONE]":
                break
            chunk = json.loads(data)
            if "error" in chunk:
                err = chunk["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                raise ProviderError(f"stream error: {message}", "server")
            if chunk.get("usage"):
                yield UsageReport(parse_usage(chunk["usage"]))
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    yield ThinkingDelta(reasoning)
                if delta.get("content"):
                    yield TextDelta(delta["content"])
                for call in delta.get("tool_calls") or []:
                    index = int(call.get("index", 0))
                    fn = call.get("function") or {}
                    if index not in started:
                        started.add(index)
                        yield ToolCallStart(
                            index, call.get("id") or f"call_{index}", fn.get("name", "")
                        )
                    if fn.get("arguments"):
                        yield ToolCallDelta(index, fn["arguments"])
                if choice.get("finish_reason"):
                    stop = _FINISH.get(choice["finish_reason"], "end")
        yield StopEvent(stop)
