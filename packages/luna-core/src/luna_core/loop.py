"""The agent loop: model ↔ tools, approvals, hooks, cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Protocol

from luna_core import catalog
from luna_core.errors import ProviderError, ToolError
from luna_core.events import (
    ApprovalRequest,
    Cancelled,
    NeedsApproval,
    Notice,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnError,
    TurnStarted,
    UsageUpdate,
)
from luna_core.hooks import Hooks
from luna_core.messages import Image, Message, Text, ToolCall, ToolResult, message_to_dict
from luna_core.policy import MUTATING_FILE_TOOLS, Decision, Policy, UnsafeRule
from luna_core.providers.base import ModelRequest, Provider, StreamAccumulator
from luna_core.roles import ModelRoles
from luna_core.sandbox import LocalSandbox, Sandbox
from luna_core.session import Session
from luna_core.shellsplit import split_command
from luna_core.tools import builtin_tools
from luna_core.tools.base import Tool, ToolContext, ToolOutput, args_preview
from luna_core.usage import Usage, cost
from luna_core.workspace import Workspace

_DONE = object()
_GRACE = 2.0


class Compactor(Protocol):
    """Shrinks the context when it gets too large (see ``luna_core.compaction``)."""

    def maybe_compact(
        self, agent: Agent, session: Session, *, window: int | None = None
    ) -> AsyncIterator[object]:
        """Yield turn events (``Compacted``, notices) if it compacted."""
        ...


class _Cancelled(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class _Outcome:
    """How preparing one tool call ended: ``("run", args)``, ``("refuse", text)``, ``("stop",)``."""

    value: tuple


class PathLocks:
    """Named asyncio locks; mutating tools hold one per path.

    An entry exists only while somebody holds or awaits its key, so the table
    does not grow with every path ever touched.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    def locked(self, key: str) -> bool:
        """Return True while ``key`` is held."""
        lock = self._locks.get(key)
        return bool(lock and lock.locked())

    def _release_user(self, key: str) -> None:
        self._users[key] -= 1
        if self._users[key] <= 0:
            del self._users[key]
            del self._locks[key]

    @contextlib.asynccontextmanager
    async def hold(self, keys: list[str]):
        """Hold every key (sorted, so two holders never deadlock)."""
        acquired: list[str] = []
        try:
            for key in sorted(set(keys)):
                lock = self._locks.setdefault(key, asyncio.Lock())
                self._users[key] = self._users.get(key, 0) + 1
                try:
                    await lock.acquire()
                except BaseException:
                    self._release_user(key)
                    raise
                acquired.append(key)
            yield
        finally:
            for key in reversed(acquired):
                self._locks[key].release()
                self._release_user(key)


def readable_error(exc: ProviderError, provider_name: str) -> str:
    """Describe ``exc`` for the user, with the fix where there is one."""
    if exc.kind == "auth":
        return (
            f"{provider_name} rejected the API key ({exc}). Set it with "
            f"`luna config set-key {provider_name}` or the provider's env var."
        )
    if exc.kind == "context_length":
        return (
            f"the conversation no longer fits the model's context window ({exc}). "
            "Run /compact or start a new session."
        )
    if exc.kind == "rate_limit":
        return f"{provider_name} is rate limiting requests ({exc}). Wait a moment and retry."
    if exc.kind == "network":
        return f"network problem talking to {provider_name}: {exc}"
    return f"{provider_name}: {exc}"


@dataclasses.dataclass(frozen=True)
class SideAnswer:
    """The answer to a ``/btw`` side question."""

    text: str
    usage: Usage
    cost: float | None
    #: Set when images went to a model with no vision and no vision role rescued it.
    notice: str = ""


async def _cancellable(stream: AsyncIterator, cancel: asyncio.Event) -> AsyncIterator:
    iterator = stream.__aiter__()
    waiter = asyncio.ensure_future(cancel.wait())
    step: asyncio.Future | None = None
    try:
        while True:
            step = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({step, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if step not in done:
                step.cancel()
                with contextlib.suppress(BaseException):
                    await step
                raise _Cancelled
            exc = step.exception()
            if isinstance(exc, StopAsyncIteration):
                return
            if exc is not None:
                raise exc
            yield step.result()
    finally:
        waiter.cancel()
        if step is not None and not step.done():
            # The consumer was cancelled mid-read: do not leave the read running.
            step.cancel()
            with contextlib.suppress(BaseException):
                await step
        with contextlib.suppress(Exception):
            await iterator.aclose()


class Agent:
    """A configured agent; ``run`` executes one turn in a session."""

    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        tools: list[Tool] | None = None,
        system: str | Callable[[Session], str] = "",
        policy: Policy | None = None,
        workspace: Workspace | None = None,
        sandbox: Sandbox | None = None,
        hooks: Hooks | None = None,
        max_steps: int = 50,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning: str | None = None,
        max_cost: float | None = None,
        compactor: Compactor | None = None,
        services: dict | None = None,
        tool_output_budget: int = 30_000,
        locks: PathLocks | None = None,
        model_overrides: dict | None = None,
        roles: ModelRoles | None = None,
        name: str = "luna",
    ) -> None:
        self.provider = provider
        self.model = model
        self.workspace = workspace or Workspace(os.getcwd())
        self.tools: dict[str, Tool] = {
            t.name: t for t in (tools if tools is not None else builtin_tools())
        }
        self.system = system
        self.policy = policy or Policy(workspace=self.workspace)
        self.sandbox = sandbox or LocalSandbox()
        self.hooks = hooks or Hooks()
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.reasoning = reasoning
        self.max_cost = max_cost
        self.compactor = compactor
        self.services = dict(services or {})
        self.tool_output_budget = tool_output_budget
        self.locks = locks or PathLocks()
        self.model_overrides = model_overrides
        self.roles = roles
        self.name = name

    # -- configuration -----------------------------------------------------
    def add_tool(self, tool: Tool) -> None:
        """Add or replace a tool."""
        self.tools[tool.name] = tool

    def remove_tool(self, name: str) -> None:
        """Remove a tool if present."""
        self.tools.pop(name, None)

    @property
    def model_info(self) -> catalog.ModelInfo:
        """Catalogue entry for the model."""
        return catalog.lookup(self.model, self.model_overrides)

    def system_prompt(self, session: Session) -> str:
        """Return the system prompt for ``session``."""
        return self.system(session) if callable(self.system) else self.system

    def request(
        self, session: Session, messages: list[Message] | None = None, *, model: str | None = None
    ) -> ModelRequest:
        """Build the model request for the session's current context."""
        return ModelRequest(
            model=model or self.model,
            system=self.system_prompt(session),
            messages=session.messages() if messages is None else messages,
            tools=[t.spec for t in self.tools.values()],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            reasoning=self.reasoning,
        )

    async def side_question(
        self, session: Session, text: str, *, images: tuple[Image, ...] | list[Image] = ()
    ) -> SideAnswer:
        """Answer a question over the current context without tools and without logging it."""
        provider, model, role = self.provider, self.model, None
        if self.roles is not None:
            provider, model, role = self.roles.for_turn(
                self.provider,
                self.model,
                images=bool(images),
                plan=self.policy.planning,
                overrides=self.model_overrides,
            )
        info = catalog.lookup(model, self.model_overrides)
        notice = (
            f"{model} may not accept images; set a vision model in [roles] if it fails"
            if images and role != "vision" and not info.vision
            else ""
        )
        messages = [*session.messages(), Message("user", [Text(text), *images])]
        request = dataclasses.replace(self.request(session, messages, model=model), tools=[])
        acc = StreamAccumulator(provider.name)
        async for event in provider.stream(request):
            acc.feed(event)
        return SideAnswer(acc.message().text, acc.usage, cost(acc.usage, info), notice)

    # -- running -----------------------------------------------------------
    async def run(
        self, session: Session, text: str = "", *, images: tuple[Image, ...] | list[Image] = ()
    ) -> AsyncIterator[object]:
        """Run one turn; yields turn events."""
        cancel = session._begin_turn()
        turn_id = uuid.uuid4().hex[:12]
        ended = False
        try:
            # ``aclosing`` so that closing this generator (consumer ``break`` or
            # ``aclose()``) also closes the inner ones and stops running tools.
            turn = self._turn(session, turn_id, text, list(images), cancel)
            async with contextlib.aclosing(turn) as events:
                async for event in events:
                    ended = isinstance(event, (TurnDone, TurnError, Cancelled))
                    if ended:
                        # Before the last event: a consumer may close us right after it.
                        await asyncio.to_thread(self._end_journal, session, turn_id)
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            # A consumer closing us right after the final event is not a cancel.
            if not ended and not any(
                r.type == "cancelled" and r.payload.get("turn_id") == turn_id
                for r in session.records()[-3:]
            ):
                session.append("cancelled", {"turn_id": turn_id})
            raise
        finally:
            try:
                if not ended:  # cancelled, closed or crashed: still record the end
                    await self._end_journal_off_loop(session, turn_id)
            finally:
                session._end_turn()

    async def _end_journal_off_loop(self, session: Session, turn_id: str) -> None:
        """``_end_journal`` in a worker thread (the git snapshot must not block the loop)."""
        try:
            await asyncio.to_thread(self._end_journal, session, turn_id)
        except asyncio.CancelledError:
            pass  # cancelled again: the worker thread still finishes the snapshot
        except RuntimeError:  # no running loop to await on (closed from a finaliser)
            self._end_journal(session, turn_id)

    @staticmethod
    def _end_journal(session: Session, turn_id: str) -> None:
        """Record the turn's end state for /undo (a failure only weakens /undo's checks)."""
        if session.undo_journal is not None:
            with contextlib.suppress(Exception):
                session.undo_journal.end_turn(turn_id)

    def run_sync(
        self,
        session: Session,
        text: str = "",
        *,
        images: tuple[Image, ...] = (),
        approve: Callable[[ApprovalRequest], Decision] | None = None,
    ) -> list[object]:
        """Blocking wrapper around :meth:`run` for non-async callers."""

        async def main() -> list[object]:
            events = []
            async for event in self.run(session, text, images=images):
                events.append(event)
                if isinstance(event, NeedsApproval):
                    decision = (
                        approve(event.request)
                        if approve
                        else Decision.reject("no approval handler")
                    )
                    session.decide(event.request.call_id, decision)
            return events

        return asyncio.run(main())

    async def _turn(
        self, session: Session, turn_id: str, text: str, images: list[Image], cancel: asyncio.Event
    ) -> AsyncIterator[object]:
        first = not session.has_user_messages
        session.append("turn_started", {"turn_id": turn_id})
        yield TurnStarted(turn_id)
        if session.undo_journal is not None:
            await asyncio.to_thread(session.undo_journal.begin_turn, turn_id)
        blocks: list = [Text(text)] if text else []
        blocks += images
        if first:
            outcome = await self.hooks.run(
                "session_start", {"session_id": session.id, "workdir": session.workdir}
            )
            for note in outcome.notices:
                yield Notice("info", note)
            if outcome.context:
                blocks.append(Text(f"\n\n{outcome.context}"))
        provider, model, role = self.provider, self.model, None
        if self.roles is not None:
            provider, model, role = self.roles.for_turn(
                self.provider,
                self.model,
                images=bool(images),
                plan=self.policy.planning,
                overrides=self.model_overrides,
            )
            if role:
                yield Notice("dim", f"{role} model for this turn: {model}")
        info = catalog.lookup(model, self.model_overrides)
        if images and role != "vision" and not info.vision:
            yield Notice(
                "warn", f"{model} may not accept images; set a vision model in [roles] if it fails"
            )
        session.append("user", {"message": message_to_dict(Message("user", blocks))})
        total = Usage()
        steps = 0
        stop_reason = "end"
        while True:
            if cancel.is_set():
                yield self._record_cancel(session, turn_id)
                return
            for queued in session.drain_queue():
                session.append("user", {"message": message_to_dict(Message.user(queued))})
            if self.compactor is not None:
                compaction = _cancellable(
                    self.compactor.maybe_compact(self, session, window=info.context_window), cancel
                )
                try:
                    async with contextlib.aclosing(compaction) as compaction_events:
                        async for event in compaction_events:
                            yield event
                except _Cancelled:
                    yield self._record_cancel(session, turn_id)
                    return
            if steps >= self.max_steps:
                yield Notice("warn", f"stopped after max_steps={self.max_steps} model calls")
                stop_reason = "max_steps"
                break
            if (
                self.max_cost is not None
                and session.cost() + (cost(total, info) or 0) >= self.max_cost
            ):
                message = f"budget reached: max_cost=${self.max_cost:g}"
                session.append(
                    "turn_error", {"turn_id": turn_id, "message": message, "kind": "budget"}
                )
                yield TurnError(message, "budget")
                return
            steps += 1
            acc = StreamAccumulator(provider.name)
            try:
                stream = _cancellable(provider.stream(self.request(session, model=model)), cancel)
                async with contextlib.aclosing(stream) as model_events:
                    async for event in model_events:
                        acc.feed(event)
                        if isinstance(event, TextDelta):
                            yield event
                        elif isinstance(event, ThinkingDelta) and event.text:
                            yield ThinkingDelta(event.text)
            except _Cancelled:
                self._record_partial(session, acc)
                yield self._record_cancel(session, turn_id)
                return
            except ProviderError as exc:
                self._record_partial(session, acc)
                message = readable_error(exc, provider.name)
                session.append(
                    "turn_error", {"turn_id": turn_id, "message": message, "kind": exc.kind}
                )
                yield TurnError(message, exc.kind)
                return
            except (asyncio.CancelledError, GeneratorExit):
                self._record_partial(session, acc)
                raise
            message = acc.message()
            session.append(
                "assistant", {"message": message_to_dict(message), "usage": acc.usage.to_dict()}
            )
            total = total + acc.usage
            yield UsageUpdate(acc.usage)
            if not message.tool_calls:
                stop_reason = acc.stop_reason
                if stop_reason == "max_tokens":
                    yield Notice("warn", "the answer was cut off at max_tokens")
                outcome = await self.hooks.run(
                    "turn_end", {"session_id": session.id, "turn_id": turn_id, "text": message.text}
                )
                for note in outcome.notices:
                    yield Notice("info", note)
                if outcome.block and outcome.message and steps < self.max_steps:
                    session.append(
                        "user", {"message": message_to_dict(Message.user(outcome.message))}
                    )
                    continue
                break
            results: list[ToolResult] = []
            tools_run = self._run_tools(session, turn_id, message.tool_calls, cancel, results)
            async with contextlib.aclosing(tools_run) as tool_events:
                async for event in tool_events:
                    yield event
            session.append(
                "tool_results", {"message": message_to_dict(Message.tool_results(results))}
            )
            if cancel.is_set():
                yield self._record_cancel(session, turn_id)
                return
        price = cost(total, info)
        session.append(
            "turn_done",
            {
                "turn_id": turn_id,
                "usage": total.to_dict(),
                "cost": price,
                "stop_reason": stop_reason,
            },
        )
        yield TurnDone(total, price, stop_reason)

    def _record_partial(self, session: Session, acc: StreamAccumulator) -> None:
        partial = acc.message()
        kept = [b for b in partial.content if not isinstance(b, ToolCall)]
        if any(isinstance(b, Text) and b.text for b in kept):
            session.append(
                "assistant",
                {"message": message_to_dict(Message.assistant(kept)), "usage": acc.usage.to_dict()},
            )

    def _record_cancel(self, session: Session, turn_id: str) -> Cancelled:
        session.append("cancelled", {"turn_id": turn_id})
        return Cancelled()

    # -- tools -------------------------------------------------------------
    async def _run_tools(
        self,
        session: Session,
        turn_id: str,
        calls: list[ToolCall],
        cancel: asyncio.Event,
        out: list[ToolResult],
    ) -> AsyncIterator[object]:
        results: dict[str, ToolResult] = {}
        approved: list[tuple[ToolCall, Tool, dict]] = []

        def refuse(call: ToolCall, text: str) -> ToolFinished:
            results[call.id] = ToolResult(call.id, (Text(text),), True, call.name)
            return ToolFinished(call.id, call.name, False, text.splitlines()[0][:120], 0.0)

        tasks: list[asyncio.Task] = []
        watchdog: asyncio.Task | None = None
        try:
            for call in calls:
                if cancel.is_set():
                    break
                tool = self.tools.get(call.name)
                if tool is None:
                    yield refuse(
                        call,
                        f"unknown tool {call.name!r}; available: {', '.join(sorted(self.tools))}",
                    )
                    continue
                args = dict(call.args)
                if "__invalid_json__" in args:
                    yield refuse(
                        call,
                        f"the arguments for {call.name} were not valid JSON: "
                        f"{args['__invalid_json__'][:200]}",
                    )
                    continue
                outcome: tuple = ("stop",)
                try:
                    prepare = self._prepare(session, call, tool, args, cancel)
                    async with contextlib.aclosing(prepare) as prepared:
                        async for item in prepared:
                            if isinstance(item, _Outcome):
                                outcome = item.value
                            else:
                                yield item
                except Exception as exc:  # noqa: BLE001 - bad args/policy/hook → error result
                    outcome = ("refuse", f"{call.name} failed: {type(exc).__name__}: {exc}")
                if outcome[0] == "stop":
                    break
                if outcome[0] == "refuse":
                    yield refuse(call, outcome[1])
                    continue
                approved.append((call, tool, outcome[1]))

            if cancel.is_set():
                # Esc during an approval (or a hook): nothing of this batch runs.
                approved.clear()
            queue: asyncio.Queue = asyncio.Queue()
            tasks = [
                asyncio.create_task(
                    self._execute(session, turn_id, call, tool, args, queue, cancel, results)
                )
                for call, tool, args in approved
            ]
            watchdog = asyncio.create_task(self._watchdog(cancel, tasks)) if tasks else None
            finished = 0
            while finished < len(tasks):
                event = await queue.get()
                if event is _DONE:
                    finished += 1
                else:
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            # The consumer task was cancelled or the generator closed: stop the
            # tools, then record a result for every call so the log stays valid.
            cancel.set()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._fill_cancelled(calls, results, out)
            session.append("tool_results", {"message": message_to_dict(Message.tool_results(out))})
            raise
        finally:
            if watchdog:
                watchdog.cancel()
        self._fill_cancelled(calls, results, out)

    async def _prepare(
        self, session: Session, call: ToolCall, tool: Tool, args: dict, cancel: asyncio.Event
    ) -> AsyncIterator[object]:
        """Policy, approval and pre_tool hooks for one call; ends with an ``_Outcome``."""
        verdict = self.policy.check(tool, args)
        if verdict.action == "deny":
            yield _Outcome(("refuse", verdict.message))
            return
        if verdict.action == "ask":
            request = dataclasses.replace(verdict.request, call_id=call.id)
            future = session.expect_decision(call.id)
            yield NeedsApproval(request)
            decision = await self._await_decision(future, cancel)
            if decision is None:
                yield _Outcome(("stop",))
                return
            session.append("approval", {"call_id": call.id, "decision": decision.to_dict()})
            if decision.always:
                # echoing ``suggested_rule`` back means "always allow this call":
                # save every rule it takes (one per program of a compound command)
                if decision.always == request.suggested_rule and request.suggested_rules:
                    to_save = list(request.suggested_rules)
                else:
                    to_save = [decision.always]
                for rule in to_save:
                    try:
                        self.policy.remember_always(rule)
                    except UnsafeRule as exc:
                        yield Notice("warn", f"rule not saved: {exc}")
                    except Exception as exc:  # noqa: BLE001 - the approval itself still stands
                        yield Notice(
                            "warn",
                            f"could not save the rule {rule!r} ({exc}); "
                            "it applies for this session only",
                        )
            if decision.always and call.name == "execute":
                parts = split_command(str(args.get("command", "")))
                if parts is not None and parts.elevated:
                    yield Notice(
                        "info",
                        "команды с sudo/env-префиксом или такими переменными "
                        "всегда спрашиваются, даже с правилом «всегда»",
                    )
            if decision.type == "reject":
                suffix = f": {decision.message}" if decision.message else ""
                yield _Outcome(("refuse", f"rejected by the user{suffix}"))
                return
            if decision.type == "edit" and decision.args is not None:
                args = dict(decision.args)
        pre = await self.hooks.run(
            "pre_tool",
            {"session_id": session.id, "tool": call.name, "args": args},
            tool=call.name,
        )
        for note in pre.notices:
            yield Notice("info", note)
        if pre.block:
            yield _Outcome(("refuse", pre.message or f"blocked by a hook ({call.name})"))
            return
        yield _Outcome(("run", dict(pre.args) if pre.args is not None else args))

    @staticmethod
    def _fill_cancelled(
        calls: list[ToolCall], results: dict[str, ToolResult], out: list[ToolResult]
    ) -> None:
        out.clear()
        for call in calls:
            out.append(
                results.get(call.id)
                or ToolResult(call.id, (Text("cancelled by the user"),), True, call.name)
            )

    @staticmethod
    async def _await_decision(future: asyncio.Future, cancel: asyncio.Event) -> Decision | None:
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            done, _ = await asyncio.wait({future, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if future in done and not future.cancelled():
            return future.result()
        future.cancel()
        return None

    @staticmethod
    async def _watchdog(cancel: asyncio.Event, tasks: list[asyncio.Task]) -> None:
        await cancel.wait()
        await asyncio.sleep(_GRACE)
        for task in tasks:
            task.cancel()

    def _lock_keys(self, tool: Tool, args: dict) -> list[str]:
        root = str(self.workspace.root)
        if tool.name == "execute":
            return [f"{root}:shell"]
        if not tool.mutating:
            return []
        raw = args.get("file_path") or args.get("path") or ""
        try:
            rel = self.workspace.rel(self.workspace.resolve(raw)) if raw else "*"
        except (ToolError, OSError, ValueError):
            rel = str(raw)
        return [f"{root}:{rel}"]

    async def _execute(
        self,
        session: Session,
        turn_id: str,
        call: ToolCall,
        tool: Tool,
        args: dict,
        queue: asyncio.Queue,
        cancel: asyncio.Event,
        results: dict[str, ToolResult],
    ) -> None:
        started = time.monotonic()
        output = ToolOutput.error("cancelled by the user")
        try:
            queue.put_nowait(ToolStarted(call.id, call.name, args, args_preview(args)))
            output = await self._invoke(session, turn_id, call, tool, args, queue, cancel)
            output = await self._post_process(session, call, args, output, queue)
        finally:
            # Always record a result and release ``_run_tools``, whatever happened above.
            results[call.id] = ToolResult(call.id, output.content, output.is_error, call.name)
            detail = output.detail or (
                output.content[0].text.splitlines()[0][:120]
                if output.content and isinstance(output.content[0], Text) and output.content[0].text
                else ""
            )
            queue.put_nowait(
                ToolFinished(
                    call.id, call.name, not output.is_error, detail, time.monotonic() - started
                )
            )
            queue.put_nowait(_DONE)

    async def _invoke(
        self,
        session: Session,
        turn_id: str,
        call: ToolCall,
        tool: Tool,
        args: dict,
        queue: asyncio.Queue,
        cancel: asyncio.Event,
    ) -> ToolOutput:
        ctx = ToolContext(
            workspace=self.workspace,
            sandbox=self.sandbox,
            session_id=session.id,
            call_id=call.id,
            emit=queue.put_nowait,
            state=session.state,
            cancel=cancel,
            services={**self.services, "agent": self, "session": session, "turn_id": turn_id},
        )
        try:
            async with self.locks.hold(self._lock_keys(tool, args)):
                # A subagent working in its parent's workspace records into the
                # parent's journal and turn (services set by the subagent runner).
                journal = self.services.get("undo_journal") or session.undo_journal
                journal_turn = self.services.get("undo_turn_id") or turn_id
                if tool.name in MUTATING_FILE_TOOLS and journal is not None:
                    raw = args.get("file_path") or args.get("path") or ""
                    rel = None
                    with contextlib.suppress(ToolError, OSError, ValueError):
                        rel = self.workspace.rel(self.workspace.resolve(raw))
                    if rel is not None:
                        await asyncio.to_thread(journal.before_write, journal_turn, rel)
                elif journal is not None and (tool.name == "execute" or tool.mutating):
                    # a command (or a mutating extension tool) may change any file
                    await asyncio.to_thread(journal.note_command, journal_turn)
                return await tool.invoke(ctx, args)
        except asyncio.CancelledError:
            return ToolOutput.error("cancelled by the user")
        except Exception as exc:  # noqa: BLE001 - a broken journal/lock must not hang the turn
            return ToolOutput.error(f"{tool.name} failed: {type(exc).__name__}: {exc}")

    async def _post_process(
        self, session: Session, call: ToolCall, args: dict, output: ToolOutput, queue: asyncio.Queue
    ) -> ToolOutput:
        """Run post_tool hooks, clip, update anchors; a failure here becomes a notice."""
        try:
            post = await self.hooks.run(
                "post_tool",
                {
                    "session_id": session.id,
                    "tool": call.name,
                    "args": args,
                    "ok": not output.is_error,
                    "output": "".join(p.text for p in output.content if isinstance(p, Text))[
                        :20_000
                    ],
                },
                tool=call.name,
            )
            for note in post.notices:
                queue.put_nowait(Notice("info", note))
            if post.context or post.block:
                extra = Text(f"\n\n{post.message or post.context}")
                output = ToolOutput(
                    (*output.content, extra), output.is_error or post.block, output.detail
                )
        except Exception as exc:  # noqa: BLE001 - the tool already ran; keep its output
            queue.put_nowait(Notice("warn", f"post_tool hooks for {call.name} failed: {exc}"))
        output = await self._clip(output, call.id)
        try:
            self.policy.after(call.name, args, not output.is_error)
            if call.name == "write_todos" and not output.is_error:
                session.append("todos", {"todos": session.state.get("todos", [])})
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not lose the result
            queue.put_nowait(Notice("warn", f"bookkeeping after {call.name} failed: {exc}"))
        return output

    @staticmethod
    def _save_overflow(target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    async def _clip(self, output: ToolOutput, call_id: str) -> ToolOutput:
        text = "".join(p.text for p in output.content if isinstance(p, Text))
        if len(text) <= self.tool_output_budget:
            return output
        rel = Path(".luna", "tool-output", f"{call_id}.txt").as_posix()
        try:
            await asyncio.to_thread(self._save_overflow, self.workspace.root / rel, text)
            where = f"full output saved to {rel} — read it with read_file"
        except OSError as exc:
            where = f"the full output could not be saved ({exc})"
        half = self.tool_output_budget // 2 - 200
        omitted = len(text) - 2 * half
        clipped = f"{text[:half]}\n\n… [{omitted} characters omitted; {where}] …\n\n{text[-half:]}"
        images = tuple(p for p in output.content if isinstance(p, Image))
        return ToolOutput((Text(clipped), *images), output.is_error, output.detail)


__all__ = ["Agent", "Compactor", "PathLocks", "SideAnswer", "readable_error"]
