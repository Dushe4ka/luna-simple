"""Conversation content: blocks, messages and their JSON form.

The system prompt is not a message: it is passed separately to providers.
Messages are what the event log stores and what adapters translate into each
provider's wire format.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

#: Bumped when the JSON shape of messages changes incompatibly.
SCHEMA_VERSION = 1

_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class Text:
    """Plain text."""

    text: str


@dataclass(frozen=True)
class Thinking:
    """Model reasoning.

    ``signature`` carries the provider's opaque data that must be sent back
    unchanged (Anthropic thinking signatures, OpenAI encrypted reasoning).
    ``provider`` records which adapter produced it, so another provider is
    never sent a signature it cannot verify.
    """

    text: str
    signature: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class Image:
    """An inline image, base64-encoded."""

    media_type: str
    data: str


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model.

    ``extra`` keeps provider-specific data that has to round-trip (for
    example Gemini's thought signature on a function call).
    """

    id: str
    name: str
    args: dict
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one tool call, returned to the model."""

    call_id: str
    content: tuple[Text | Image, ...]
    is_error: bool = False
    name: str = ""

    @property
    def text(self) -> str:
        """Concatenated text parts of the result."""
        return "".join(part.text for part in self.content if isinstance(part, Text))


Block = Text | Thinking | Image | ToolCall | ToolResult
Role = Literal["user", "assistant", "tool"]


@dataclass
class Message:
    """One conversation message."""

    role: Role
    content: list[Block]

    @classmethod
    def user(cls, text: str = "", images: list[Image] | tuple[Image, ...] = ()) -> Message:
        """Build a user message from text and optional images."""
        blocks: list[Block] = [Text(text)] if text else []
        blocks.extend(images)
        return cls("user", blocks)

    @classmethod
    def assistant(cls, blocks: list[Block]) -> Message:
        """Build an assistant message."""
        return cls("assistant", list(blocks))

    @classmethod
    def tool_results(cls, results: list[ToolResult]) -> Message:
        """Build the message carrying tool results back to the model."""
        return cls("tool", list(results))

    @property
    def text(self) -> str:
        """Concatenated text blocks (thinking excluded)."""
        return "".join(b.text for b in self.content if isinstance(b, Text))

    @property
    def tool_calls(self) -> list[ToolCall]:
        """Tool calls in this message, in order."""
        return [b for b in self.content if isinstance(b, ToolCall)]

    @property
    def results(self) -> list[ToolResult]:
        """Tool results in this message, in order."""
        return [b for b in self.content if isinstance(b, ToolResult)]


def block_to_dict(block: Block) -> dict:
    """Serialise one block to a JSON-compatible dict."""
    if isinstance(block, Text):
        return {"type": "text", "text": block.text}
    if isinstance(block, Thinking):
        return {
            "type": "thinking",
            "text": block.text,
            "signature": block.signature,
            "provider": block.provider,
        }
    if isinstance(block, Image):
        return {"type": "image", "media_type": block.media_type, "data": block.data}
    if isinstance(block, ToolCall):
        return {
            "type": "tool_call",
            "id": block.id,
            "name": block.name,
            "args": block.args,
            "extra": block.extra,
        }
    if isinstance(block, ToolResult):
        return {
            "type": "tool_result",
            "call_id": block.call_id,
            "content": [block_to_dict(p) for p in block.content],
            "is_error": block.is_error,
            "name": block.name,
        }
    raise TypeError(f"not a block: {block!r}")


def block_from_dict(data: dict) -> Block:
    """Rebuild a block from :func:`block_to_dict` output."""
    kind = data.get("type")
    if kind == "text":
        return Text(data["text"])
    if kind == "thinking":
        return Thinking(data["text"], data.get("signature"), data.get("provider"))
    if kind == "image":
        return Image(data["media_type"], data["data"])
    if kind == "tool_call":
        return ToolCall(data["id"], data["name"], dict(data["args"]), dict(data.get("extra") or {}))
    if kind == "tool_result":
        parts = tuple(block_from_dict(p) for p in data["content"])
        return ToolResult(data["call_id"], parts, bool(data.get("is_error")), data.get("name", ""))
    raise ValueError(f"unknown block type {kind!r}")


def message_to_dict(message: Message) -> dict:
    """Serialise a message (versioned)."""
    return {
        "v": SCHEMA_VERSION,
        "role": message.role,
        "content": [block_to_dict(b) for b in message.content],
    }


def message_from_dict(data: dict) -> Message:
    """Rebuild a message from :func:`message_to_dict` output."""
    return Message(data["role"], [block_from_dict(b) for b in data["content"]])


def image_from_file(path: str | Path) -> Image:
    """Load an image file as an :class:`Image` block."""
    path = Path(path)
    media_type = _IMAGE_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise ValueError(f"{path.name} is not a supported image (png, jpg, gif, webp)")
    return Image(media_type, base64.b64encode(path.read_bytes()).decode("ascii"))
