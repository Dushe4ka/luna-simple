"""Google Gemini adapter (native ``streamGenerateContent``)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import httpx

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

_DROP = {"additionalProperties", "$schema", "default", "title", "examples"}
_FINISH = {
    "STOP": "end",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "BLOCKLIST": "refusal",
}


def sanitize_schema(schema: object) -> object:
    """Drop JSON-Schema keys Gemini's schema subset rejects, recursively."""
    if isinstance(schema, dict):
        return {k: sanitize_schema(v) for k, v in schema.items() if k not in _DROP}
    if isinstance(schema, list):
        return [sanitize_schema(v) for v in schema]
    return schema


class GoogleGenAI:
    """Streams ``POST {base_url}/models/{model}:streamGenerateContent?alt=sse``."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        name: str = "google",
        client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
        thinking_budgets: dict | None = None,
    ) -> None:
        self.name = name
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client = client or default_client()
        self.retry = retry or RetryPolicy()
        self.thinking_budgets = thinking_budgets or {"low": 1024, "medium": 8192, "high": 24576}

    def build_payload(self, request: ModelRequest) -> dict:
        """Build the JSON body for ``request``."""
        contents: list[dict] = []
        for message in request.messages:
            role, parts = self._convert(message)
            if not parts:
                continue
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        body: dict = {"contents": contents}
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        if request.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": sanitize_schema(t.parameters),
                        }
                        for t in request.tools
                    ]
                }
            ]
        config: dict = {}
        if request.max_tokens:
            config["maxOutputTokens"] = request.max_tokens
        if request.temperature is not None:
            config["temperature"] = request.temperature
        budget = self.thinking_budgets.get(request.reasoning or "")
        if budget:
            config["thinkingConfig"] = {"thinkingBudget": budget, "includeThoughts": True}
        if config:
            body["generationConfig"] = config
        return body

    def _convert(self, message: Message) -> tuple[str, list[dict]]:
        if message.role == "user":
            parts: list[dict] = []
            for b in message.content:
                if isinstance(b, Text) and b.text:
                    parts.append({"text": b.text})
                elif isinstance(b, Image):
                    parts.append({"inlineData": {"mimeType": b.media_type, "data": b.data}})
            return "user", parts
        if message.role == "assistant":
            parts = []
            for b in message.content:
                if isinstance(b, Thinking):
                    # Thought summaries are never sent back; a signature-only block
                    # belongs to the text part right before it.
                    if (
                        b.provider == self.name
                        and b.signature
                        and not b.text
                        and parts
                        and "text" in parts[-1]
                    ):
                        parts[-1]["thoughtSignature"] = b.signature
                elif isinstance(b, Text) and b.text:
                    parts.append({"text": b.text})
                elif isinstance(b, ToolCall):
                    part: dict = {"functionCall": {"name": b.name, "args": b.args}}
                    if b.extra.get("thought_signature"):
                        part["thoughtSignature"] = b.extra["thought_signature"]
                    parts.append(part)
            return "model", parts
        parts = []
        for r in message.results:
            parts.append(
                {
                    "functionResponse": {
                        "name": r.name,
                        "response": {"result": r.text or "(no output)"},
                    }
                }
            )
            parts.extend(
                {"inlineData": {"mimeType": p.media_type, "data": p.data}}
                for p in r.content
                if isinstance(p, Image)
            )
        return "user", parts

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream one generation (retrying transient failures before output)."""
        async for event in with_retries(
            lambda: guard_stream(self.name, self._once(request)), self.retry
        ):
            yield event

    async def _once(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        headers = {"x-goog-api-key": self.api_key or ""}
        url = f"{self.base_url}/models/{request.model}:streamGenerateContent?alt=sse"
        index = 0
        stop = "end"
        usage: Usage | None = None
        async for _name, data in post_sse(
            self.client, url, headers=headers, json=self.build_payload(request)
        ):
            chunk = json.loads(data)
            if chunk.get("usageMetadata"):
                u = chunk["usageMetadata"]
                thoughts = int(u.get("thoughtsTokenCount") or 0)
                usage = Usage(
                    input=int(u.get("promptTokenCount") or 0),
                    output=int(u.get("candidatesTokenCount") or 0) + thoughts,
                    cache_read=int(u.get("cachedContentTokenCount") or 0),
                    reasoning=thoughts,
                )
            for candidate in chunk.get("candidates") or []:
                for part in (candidate.get("content") or {}).get("parts") or []:
                    signature = part.get("thoughtSignature")
                    if "functionCall" in part:
                        fc = part["functionCall"]
                        extra = {"thought_signature": signature} if signature else {}
                        call_id = fc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                        yield ToolCallStart(index, call_id, fc.get("name", ""), extra)
                        yield ToolCallDelta(index, json.dumps(fc.get("args") or {}))
                        index += 1
                        stop = "tool_use"
                    elif part.get("thought"):
                        yield ThinkingDelta(part.get("text", ""))
                    elif "text" in part:
                        if part["text"]:
                            yield TextDelta(part["text"])
                        if signature:
                            yield ThinkingDelta("", signature=signature)
                finish = candidate.get("finishReason")
                if finish and stop != "tool_use":
                    stop = _FINISH.get(finish, "end")
        if usage is not None:
            yield UsageReport(usage)
        yield StopEvent(stop)
