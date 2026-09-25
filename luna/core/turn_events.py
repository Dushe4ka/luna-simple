"""Pure event generator over one pass of an agent turn.

Mirrors the chunk-parsing logic that used to live inline in
``luna.core.session._stream_turn`` — extracted so both the REPL (which
drives ``rich`` objects from these events) and the server (which turns
them into SSE) share exactly one implementation of "what does a turn's
raw ``agent.stream()`` output mean."
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

#: Read-only navigation tools whose own progress-line label already says
#: what happened — a successful call yields no extra detail, only a
#: failure does. Matches session.py's existing convention exactly.
_QUIET_ON_SUCCESS = {"read_file", "ls", "glob", "grep"}

_RELOAD_MARKER = "Run /reload"


@dataclass
class TextDelta:
    """A chunk of streamed assistant text."""

    text: str


@dataclass
class UsageDelta:
    """Token-usage metadata attached to a model chunk."""

    usage_metadata: dict


@dataclass
class ToolStarted:
    """A tool call has been requested and is now in flight."""

    call_id: str
    name: str
    args: dict


@dataclass
class ToolFinished:
    """A tool call has completed (successfully or not)."""

    call_id: str
    name: str
    ok: bool
    detail: str


@dataclass
class ReloadRequested:
    """A tool (manage_mcp/manage_skills) asked for a /reload."""


@dataclass
class Interrupted:
    """The graph paused for human approval. iter_turn stops after this."""

    value: dict


TurnEvent = TextDelta | UsageDelta | ToolStarted | ToolFinished | ReloadRequested | Interrupted


def iter_turn(agent, payload, config: dict) -> Iterator[TurnEvent]:
    """Run one pass of ``agent.stream(...)``, yielding typed events.

    Stops after yielding an ``Interrupted`` event — the caller decides
    whether/how to resume by calling this again with
    ``payload=Command(resume=...)``.
    """
    # Seed both dedup sets from the persisted graph state rather than
    # starting empty: on a resume call (``payload=Command(resume=...)``)
    # after an ``Interrupted`` event, this same turn's already-recorded
    # AIMessage/ToolMessage history would otherwise be re-scanned from
    # scratch by the loop below, re-yielding a ``ToolStarted`` for a call
    # that started (and was already reported) before the pause. As of the
    # pause, state contains the AIMessage with the tool_calls that
    # triggered the interrupt but NOT yet a ToolMessage for it (the tool
    # hasn't run), so seeding only suppresses the stale ToolStarted —
    # ToolFinished still fires once the tool actually completes.
    state_messages = agent.get_state(config).values.get("messages", [])
    requested_tools: set[str] = set()
    seen_tools: set[str] = set()
    for m in state_messages:
        if isinstance(m, AIMessage):
            for call in m.tool_calls or []:
                call_id = call.get("id")
                if call_id:
                    requested_tools.add(call_id)
        elif isinstance(m, ToolMessage):
            seen_tools.add(m.tool_call_id)

    for mode, chunk in agent.stream(payload, config=config, stream_mode=["messages", "updates"]):
        if mode == "messages":
            msg, meta = chunk
            if meta.get("langgraph_node") == "model" and isinstance(
                msg, (AIMessage, AIMessageChunk)
            ):
                usage = getattr(msg, "usage_metadata", None)
                if usage:
                    yield UsageDelta(usage)
                text = msg.content if isinstance(msg.content, str) else ""
                if text:
                    yield TextDelta(text)
        elif mode == "updates":
            for update in chunk.values():
                if not isinstance(update, dict):
                    continue
                for m in update.get("messages", []) or []:
                    if isinstance(m, AIMessage):
                        for call in m.tool_calls or []:
                            call_id = call.get("id")
                            if not call_id or call_id in requested_tools:
                                continue
                            requested_tools.add(call_id)
                            yield ToolStarted(call_id, call["name"], call.get("args") or {})
                    elif isinstance(m, ToolMessage) and m.tool_call_id not in seen_tools:
                        seen_tools.add(m.tool_call_id)
                        body = str(m.content) if m.content else ""
                        detail = body.splitlines()[0][:120] if body else ""
                        ok = getattr(m, "status", "success") != "error"
                        if ok and m.name in _QUIET_ON_SUCCESS:
                            detail = ""
                        yield ToolFinished(m.tool_call_id, m.name or "", ok, detail)
                        if m.name in ("manage_mcp", "manage_skills") and _RELOAD_MARKER in body:
                            yield ReloadRequested()
            if "__interrupt__" in chunk:
                for interrupt in chunk["__interrupt__"]:
                    yield Interrupted(interrupt.value)
                return

    state = agent.get_state(config)
    for interrupt in getattr(state, "interrupts", ()) or ():
        yield Interrupted(interrupt.value)
