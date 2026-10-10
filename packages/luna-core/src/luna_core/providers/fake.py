"""A scripted provider for tests and SDK examples."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from luna_core.events import (
    ModelEvent,
    StopEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallStart,
    UsageReport,
)
from luna_core.messages import Message, Text, Thinking, ToolCall
from luna_core.providers.base import ModelRequest
from luna_core.usage import Usage


class FakeProvider:
    """Replays a script: each ``stream`` call consumes the next item.

    An item is either an assistant :class:`Message` (converted to events) or
    an explicit list of :data:`ModelEvent`. Requests are recorded.
    """

    name = "fake"

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[ModelRequest] = []

    @staticmethod
    def text(text: str) -> Message:
        """Build an assistant reply with plain text."""
        return Message.assistant([Text(text)])

    @staticmethod
    def call(name: str, args: dict, call_id: str | None = None) -> Message:
        """Build an assistant reply calling one tool."""
        return Message.assistant([ToolCall(call_id or f"call_{name}", name, args)])

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Yield the next scripted reply."""
        self.requests.append(request)
        assert self.script, "FakeProvider script exhausted"
        item = self.script.pop(0)
        events = item if isinstance(item, list) else _events_for(item)
        for event in events:
            if isinstance(event, BaseException):
                raise event
            yield event


def _events_for(message: Message) -> list[ModelEvent]:
    events: list[ModelEvent] = []
    index = 0
    for block in message.content:
        if isinstance(block, Text):
            events.append(TextDelta(block.text))
        elif isinstance(block, Thinking):
            events.append(ThinkingDelta(block.text, block.signature))
        elif isinstance(block, ToolCall):
            events.append(ToolCallStart(index, block.id, block.name, dict(block.extra)))
            events.append(ToolCallDelta(index, json.dumps(block.args)))
            index += 1
    events.append(UsageReport(Usage(input=10, output=5)))
    events.append(StopEvent("tool_use" if message.tool_calls else "end"))
    return events
