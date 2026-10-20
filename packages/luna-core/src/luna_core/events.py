"""Events: what a provider streams, and what a turn emits to clients.

Turn events are the public protocol of the core: the REPL, the TUI, the
server's SSE stream, ACP and SDK users all consume them. ``to_wire`` gives
their JSON form (documented in ``docs/protocol.md``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from luna_core.usage import Usage

NOTICE_LEVELS = ("dim", "info", "ok", "warn", "error")


# --- model stream events ---------------------------------------------------


@dataclass(frozen=True)
class TextDelta:
    """A piece of answer text (also a turn event)."""

    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """A piece of reasoning; ``signature`` arrives on the last piece (also a turn event)."""

    text: str
    signature: str | None = None


@dataclass(frozen=True)
class ToolCallStart:
    """The model started a tool call at position ``index``."""

    index: int
    id: str
    name: str
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallDelta:
    """More JSON argument text for the tool call at ``index``."""

    index: int
    args_json: str


@dataclass(frozen=True)
class UsageReport:
    """Token usage reported by the provider for this request."""

    usage: Usage


@dataclass(frozen=True)
class StopEvent:
    """The request finished: ``end``, ``tool_use``, ``max_tokens`` or ``refusal``."""

    reason: str


ModelEvent = TextDelta | ThinkingDelta | ToolCallStart | ToolCallDelta | UsageReport | StopEvent


# --- turn events -----------------------------------------------------------


@dataclass(frozen=True)
class ApprovalRequest:
    """A tool call waiting for a human decision."""

    call_id: str
    name: str
    args: dict
    description: str = ""
    suggested_rule: str = ""


@dataclass(frozen=True)
class TurnStarted:
    """A turn began."""

    turn_id: str


@dataclass(frozen=True)
class ToolStarted:
    """A tool call started running."""

    call_id: str
    name: str
    args: dict
    preview: str


@dataclass(frozen=True)
class ToolFinished:
    """A tool call finished."""

    call_id: str
    name: str
    ok: bool
    detail: str
    duration: float


@dataclass(frozen=True)
class NeedsApproval:
    """The turn is waiting for ``Session.decide`` on this request."""

    request: ApprovalRequest


@dataclass(frozen=True)
class TodosUpdated:
    """The agent's plan (``write_todos``) changed."""

    todos: list[dict]


@dataclass(frozen=True)
class Notice:
    """A short status line for the user."""

    level: str
    text: str

    def __post_init__(self) -> None:
        """Validate the level."""
        if self.level not in NOTICE_LEVELS:
            raise ValueError(f"notice level must be one of {NOTICE_LEVELS}, got {self.level!r}")


@dataclass(frozen=True)
class UsageUpdate:
    """Usage of the request that just finished."""

    usage: Usage


@dataclass(frozen=True)
class Compacted:
    """Old history was summarised; ``removed`` messages left the model context."""

    summary: str
    removed: int


@dataclass(frozen=True)
class BackgroundDone:
    """A background task finished."""

    handle: str
    ok: bool
    summary: str


@dataclass(frozen=True)
class ReloadRequested:
    """A tool asked for the agent to be rebuilt (new MCP server, skill...)."""


@dataclass(frozen=True)
class TurnDone:
    """The turn finished normally."""

    usage: Usage
    cost: float | None
    stop_reason: str


@dataclass(frozen=True)
class TurnError:
    """The turn failed; the log stays consistent."""

    message: str
    kind: str = "error"


@dataclass(frozen=True)
class Cancelled:
    """The turn was cancelled by the user."""


TurnEvent = (
    TurnStarted
    | TextDelta
    | ThinkingDelta
    | ToolStarted
    | ToolFinished
    | NeedsApproval
    | TodosUpdated
    | Notice
    | UsageUpdate
    | Compacted
    | BackgroundDone
    | ReloadRequested
    | TurnDone
    | TurnError
    | Cancelled
)


def to_wire(event: object) -> dict:
    """JSON form of a turn event (the SSE / SDK protocol)."""
    if isinstance(event, TurnStarted):
        return {"event": "turn_started", "turn_id": event.turn_id}
    if isinstance(event, TextDelta):
        return {"event": "text_delta", "text": event.text}
    if isinstance(event, ThinkingDelta):
        return {"event": "thinking_delta", "text": event.text}
    if isinstance(event, ToolStarted):
        return {
            "event": "tool_started",
            "call_id": event.call_id,
            "name": event.name,
            "args": event.args,
            "args_preview": event.preview,
        }
    if isinstance(event, ToolFinished):
        return {
            "event": "tool_finished",
            "call_id": event.call_id,
            "name": event.name,
            "ok": event.ok,
            "detail": event.detail,
            "duration": event.duration,
        }
    if isinstance(event, NeedsApproval):
        r = event.request
        return {
            "event": "approval_needed",
            "value": {
                "action_requests": [
                    {
                        "call_id": r.call_id,
                        "name": r.name,
                        "args": r.args,
                        "description": r.description,
                        "suggested_rule": r.suggested_rule,
                    }
                ]
            },
        }
    if isinstance(event, TodosUpdated):
        return {"event": "todos", "todos": event.todos}
    if isinstance(event, Notice):
        return {"event": "notice", "level": event.level, "text": event.text}
    if isinstance(event, UsageUpdate):
        return {"event": "usage_delta", "usage": event.usage.to_dict()}
    if isinstance(event, Compacted):
        return {"event": "compacted", "summary": event.summary, "removed": event.removed}
    if isinstance(event, BackgroundDone):
        return {
            "event": "background_done",
            "handle": event.handle,
            "ok": event.ok,
            "summary": event.summary,
        }
    if isinstance(event, ReloadRequested):
        return {"event": "reload_requested"}
    if isinstance(event, TurnDone):
        return {
            "event": "turn_done",
            "usage": event.usage.to_dict(),
            "cost": event.cost,
            "stop_reason": event.stop_reason,
        }
    if isinstance(event, TurnError):
        return {"event": "error", "message": event.message, "kind": event.kind}
    if isinstance(event, Cancelled):
        return {"event": "cancelled"}
    raise TypeError(f"not a turn event: {event!r}")
