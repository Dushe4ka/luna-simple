"""Agent Client Protocol (ACP) agent: Luna inside editors (needs the ``acp`` extra)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Callable
from typing import Any

from luna_core import __version__
from luna_core.events import (
    ApprovalRequest,
    Cancelled,
    NeedsApproval,
    Notice,
    TextDelta,
    ThinkingDelta,
    TodosUpdated,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnError,
    UsageUpdate,
)
from luna_core.messages import Image
from luna_core.policy import Decision
from luna_core.session import EventLog, Session

logger = logging.getLogger(__name__)

_KINDS = {
    "read_file": "read",
    "ls": "read",
    "write_file": "edit",
    "edit_file": "edit",
    "delete": "delete",
    "glob": "search",
    "grep": "search",
    "execute": "execute",
    "task": "think",
}
_STOPS = {
    "end": "end_turn",
    "tool_use": "end_turn",
    "max_steps": "max_turn_requests",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def prompt_to_input(blocks: list[dict]) -> tuple[str, list[Image]]:
    """ACP prompt content blocks → user text and images."""
    parts: list[str] = []
    images: list[Image] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "image":
            images.append(Image(block.get("mimeType", "image/png"), block.get("data", "")))
        elif kind == "resource":
            res = block.get("resource", {})
            parts.append(f"\n<attached: {res.get('uri', '')}>\n{res.get('text', '')}\n</attached>")
        elif kind == "resource_link":
            parts.append(f"\n(see {block.get('uri', '')})")
    return "".join(parts), images


class AcpBridge:
    """Pure mapping between Luna turn events and ACP payloads."""

    def updates(self, event: object, *, window: int | None = None) -> list[dict]:
        """``session/update`` payloads for one turn event.

        ``window`` is the model's context window, needed for ``UsageUpdate``
        (ACP's ``usage_update.size`` is required); without it a ``UsageUpdate``
        is dropped rather than sent with a made-up size.
        """
        if isinstance(event, TextDelta):
            return [{"sessionUpdate": "agent_message_chunk", "content": _text(event.text)}]
        if isinstance(event, ThinkingDelta):
            return [{"sessionUpdate": "agent_thought_chunk", "content": _text(event.text)}]
        if isinstance(event, ToolStarted):
            title = f"{event.name} {event.preview}".strip()
            return [
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": event.call_id,
                    "title": title,
                    "kind": _KINDS.get(event.name, "other"),
                    "status": "in_progress",
                    "rawInput": event.args,
                }
            ]
        if isinstance(event, ToolFinished):
            update = {
                "sessionUpdate": "tool_call_update",
                "toolCallId": event.call_id,
                "status": "completed" if event.ok else "failed",
            }
            if event.detail:
                update["content"] = [{"type": "content", "content": _text(event.detail)}]
            return [update]
        if isinstance(event, TodosUpdated):
            return [
                {
                    "sessionUpdate": "plan",
                    "entries": [
                        {"content": t["content"], "priority": "medium", "status": t["status"]}
                        for t in event.todos
                    ],
                }
            ]
        if isinstance(event, Notice) and event.level in ("warn", "error"):
            return [
                {"sessionUpdate": "agent_message_chunk", "content": _text(f"\n_{event.text}_\n")}
            ]
        if isinstance(event, TurnError):
            return [
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": _text(f"\n**Error:** {event.message}\n"),
                }
            ]
        if isinstance(event, UsageUpdate):
            if window is None:
                return []
            return [
                {
                    "sessionUpdate": "usage_update",
                    "used": event.usage.input + event.usage.output,
                    "size": window,
                }
            ]
        return []

    def permission(self, request: ApprovalRequest) -> tuple[dict, list[dict]]:
        """Build the ``toolCall`` and ``options`` of a permission request."""
        tool_call = {
            "toolCallId": request.call_id,
            "title": f"{request.name} {request.description}".strip(),
            "kind": _KINDS.get(request.name, "other"),
            "status": "pending",
            "rawInput": request.args,
        }
        options = [
            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
            {
                "optionId": "allow_always",
                "name": "Always allow ({})".format(
                    ", ".join(request.suggested_rules or (request.suggested_rule,))
                ),
                "kind": "allow_always",
            },
            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
        ]
        return tool_call, options

    def decision(self, outcome: dict, request: ApprovalRequest) -> Decision:
        """Translate the client's choice."""
        if outcome.get("outcome") != "selected":
            return Decision.reject("cancelled in the editor")
        option = outcome.get("optionId")
        if option == "allow_once":
            return Decision.approve()
        if option == "allow_always":
            return Decision.approve(always=request.suggested_rule)
        return Decision.reject("rejected in the editor")

    def stop_reason(self, events: list[object]) -> str:
        """ACP stop reason for a finished turn."""
        for event in reversed(events):
            if isinstance(event, Cancelled):
                return "cancelled"
            if isinstance(event, TurnDone):
                return _STOPS.get(event.stop_reason, "end_turn")
        return "end_turn"


def _dump(model: Any) -> dict:
    return (
        model.model_dump(mode="json", by_alias=True, exclude_none=True)
        if hasattr(model, "model_dump")
        else dict(model)
    )


class LunaAcpAgent:
    """The ACP agent: one Luna session per ACP session."""

    def __init__(self, make_agent: Callable[[str], Any], log: EventLog) -> None:
        self.make_agent = make_agent
        self.log = log
        self.bridge = AcpBridge()
        self.conn: Any = None
        self._sessions: dict[str, tuple[Any, Session]] = {}
        self._ask_tasks: dict[str, set[asyncio.Task]] = {}

    def on_connect(self, conn: Any) -> None:
        """Keep the client connection (SDK callback)."""
        self.conn = conn

    async def _agent_for(self, cwd: str):
        # ``make_agent`` may be sync or async (the Luna app builds agents asynchronously).
        agent = self.make_agent(cwd)
        return await agent if inspect.isawaitable(agent) else agent

    async def initialize(
        self, protocol_version: int, client_capabilities: Any = None, **kwargs: Any
    ):
        """Handshake."""
        from acp import schema

        return schema.InitializeResponse.model_validate(
            {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {"image": True, "embeddedContext": True},
                },
                "agentInfo": {"name": "luna", "title": "Luna", "version": __version__},
            }
        )

    async def new_session(self, cwd: str, mcp_servers: list | None = None, **kwargs: Any):
        """Create a Luna session in ``cwd``."""
        from acp import schema

        session = Session.create(self.log, cwd, meta={"kind": "acp"})
        self._sessions[session.id] = (await self._agent_for(cwd), session)
        return schema.NewSessionResponse.model_validate({"sessionId": session.id})

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers: list | None = None, **kwargs: Any
    ):
        """Reopen a session and replay its transcript as updates."""
        from acp import schema

        session = Session.open(self.log, session_id)
        # Build the agent for the directory the session's log says it lives in, not
        # whatever ``cwd`` the client happens to pass (e.g. reopening the same
        # session from a different editor window/workspace).
        self._sessions[session_id] = (await self._agent_for(session.workdir), session)
        for message in session.messages():
            if message.role in ("user", "assistant") and message.text:
                kind = "user_message_chunk" if message.role == "user" else "agent_message_chunk"
                await self._update(
                    session_id, {"sessionUpdate": kind, "content": _text(message.text)}
                )
        return schema.LoadSessionResponse.model_validate({})

    async def _update(self, session_id: str, update: dict) -> None:
        from acp import schema

        note = schema.SessionNotification.model_validate(
            {"sessionId": session_id, "update": update}
        )
        await self.conn.session_update(session_id=session_id, update=note.update)

    async def prompt(self, prompt: list, session_id: str, **kwargs: Any):
        """Run one turn and stream it to the editor."""
        from acp import schema

        agent, session = self._sessions[session_id]
        text, images = prompt_to_input([_dump(b) for b in prompt])
        window = agent.model_info.context_window
        seen: list[object] = []
        run = agent.run(session, text, images=images)
        async with contextlib.aclosing(run) as events:
            async for event in events:
                seen.append(event)
                for update in self.bridge.updates(event, window=window):
                    await self._update(session_id, update)
                if isinstance(event, NeedsApproval):
                    self._spawn_ask(session_id, session, event.request)
        return schema.PromptResponse.model_validate({"stopReason": self.bridge.stop_reason(seen)})

    def _spawn_ask(
        self, session_id: str, session: Session, request: ApprovalRequest
    ) -> asyncio.Task:
        """Start ``_ask`` as a tracked task so it can be cancelled and never leaks."""
        task = asyncio.create_task(self._ask(session_id, session, request))
        tasks = self._ask_tasks.setdefault(session_id, set())
        tasks.add(task)
        task.add_done_callback(lambda t: self._ask_done(session_id, t))
        return task

    def _ask_done(self, session_id: str, task: asyncio.Task) -> None:
        self._ask_tasks.get(session_id, set()).discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover - _ask already catches everything itself
            logger.exception("ACP permission task failed unexpectedly", exc_info=exc)

    async def _ask(self, session_id: str, session: Session, request: ApprovalRequest) -> None:
        """Ask the editor to approve ``request``; always resolves the pending decision.

        Everything here — building the request, the round trip, reading the
        answer — can fail (a bad client, a dropped connection, a bug in
        ``bridge.permission``). Whatever happens, the call must not be left
        pending forever: on any failure the call is rejected instead.
        """
        try:
            tool_call, options = self.bridge.permission(request)
            from acp import schema

            params = schema.RequestPermissionRequest.model_validate(
                {"sessionId": session_id, "toolCall": tool_call, "options": options}
            )
            try:
                response = await self.conn.request_permission(
                    session_id=session_id, tool_call=params.tool_call, options=params.options
                )
                outcome = _dump(response).get("outcome", {})
            except Exception:  # noqa: BLE001 - a broken client answer rejects the call
                outcome = {"outcome": "cancelled"}
            decision = self.bridge.decision(outcome, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leave the call pending forever
            logger.exception("ACP permission request failed for call %s", request.call_id)
            decision = Decision.reject(f"permission request failed: {exc}")
        if request.call_id in session.pending_approvals:
            session.decide(request.call_id, decision)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Cancel the running turn and any outstanding permission requests."""
        entry = self._sessions.get(session_id)
        if entry:
            entry[1].cancel_turn()
        for task in list(self._ask_tasks.get(session_id, ())):
            task.cancel()


async def run_acp(make_agent: Callable[[str], Any], log: EventLog) -> None:
    """Serve ACP over stdio until the editor closes the pipe."""
    from acp import run_agent
    from acp.stdio import stdio_streams

    # Own the streams so the stdout transport is closed while the loop is still alive; left to
    # StreamWriter.__del__ it closes after the loop and prints "Event loop is closed".
    reader, writer = await stdio_streams()
    try:
        await run_agent(LunaAcpAgent(make_agent, log), input_stream=writer, output_stream=reader)
    finally:
        try:
            writer.close()  # the library's write protocol has no wait_closed(); one tick suffices
            await asyncio.sleep(0)
        except (OSError, RuntimeError):
            pass  # the editor already closed its end
