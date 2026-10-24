"""Summarise old history when the context window fills up."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

from luna_core import catalog
from luna_core.errors import ProviderError
from luna_core.events import Compacted, Notice
from luna_core.messages import Image, Message, Text, ToolCall, ToolResult
from luna_core.providers.base import ModelRequest, Provider, StreamAccumulator

SUMMARY_PROMPT = """\
You compress a coding session so the work can continue without the full transcript.
Write a dense summary in the language of the conversation with these sections:
Goal; Decisions and constraints; Files touched (paths and what changed); Commands run
and their results; Open problems and next steps. Keep exact names, paths, error
messages and numbers. Do not invent anything."""
_IMAGE_TOKENS = 1500
#: An automatic attempt that failed or found nothing to compact waits for this many
#: new entries before trying again (avoids retrying every single step).
_BACKOFF_ENTRIES = 4
#: After a ``pre_compact`` hook blocked an automatic attempt, wait for this many new
#: entries before asking the hook (and showing its notice) again.
_BLOCKED_BACKOFF_ENTRIES = 30
#: The summariser input is trimmed to this fraction of the summary model's window.
_TRIM_RATIO = 0.7
_OMITTED_MARKER = "[earlier part omitted]\n\n"


def estimate_tokens(request: ModelRequest) -> int:
    """Rough prompt size: characters ÷ 4, plus a flat cost per image."""
    chars = len(request.system)
    images = 0
    for message in request.messages:
        for block in message.content:
            if isinstance(block, Text):
                chars += len(block.text)
            elif isinstance(block, Image):
                images += 1
            elif isinstance(block, ToolCall):
                chars += len(json.dumps(block.args)) + len(block.name)
            elif isinstance(block, ToolResult):
                chars += len(block.text)
                images += sum(1 for p in block.content if isinstance(p, Image))
    chars += sum(len(json.dumps(t.parameters)) + len(t.description) for t in request.tools)
    return chars // 4 + images * _IMAGE_TOKENS


def _render(messages: list[Message], previous: str | None) -> str:
    lines = [f"<previous summary>\n{previous}\n</previous summary>"] if previous else []
    for m in messages:
        if m.role == "user":
            lines.append(f"USER: {m.text}")
        elif m.role == "assistant":
            if m.text:
                lines.append(f"ASSISTANT: {m.text}")
            lines += [
                f"TOOL CALL {c.name}: {json.dumps(c.args, ensure_ascii=False)[:500]}"
                for c in m.tool_calls
            ]
        else:
            lines += [
                f"TOOL RESULT {r.name}{' (error)' if r.is_error else ''}: {r.text[:2000]}"
                for r in m.results
            ]
    return "\n\n".join(lines)


def _trim_transcript(transcript: str, max_tokens: int) -> str:
    """Keep the most recent part of ``transcript`` within ``max_tokens`` (chars ÷ 4)."""
    limit_chars = max_tokens * 4
    if len(transcript) <= limit_chars:
        return transcript
    budget = max(limit_chars - len(_OMITTED_MARKER), 0)
    return _OMITTED_MARKER + transcript[-budget:]


def _distinct_turn_keys(entries: list) -> tuple[list, list]:
    """Per-entry turn identity and the distinct identities, oldest first.

    Entries sharing a ``turn_id`` (steering messages drained mid-turn, a
    ``turn_end`` hook continuation) are one turn. An entry whose ``turn_id``
    is ``None`` counts as its own, singleton turn.
    """
    keys = [e.turn_id if e.turn_id is not None else object() for e in entries]
    seen: set = set()
    order: list = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            order.append(key)
    return keys, order


@dataclass
class Compactor:
    """Compaction policy; plug into ``Agent(compactor=...)``."""

    threshold: float = 0.8
    keep_turns: int = 4
    provider: Provider | None = None
    model: str | None = None
    max_summary_tokens: int = 4000

    async def maybe_compact(
        self, agent, session, *, window: int | None = None
    ) -> AsyncIterator[object]:
        """Compact when the next request would exceed the threshold.

        ``window`` overrides the context window to check against (the
        turn's resolved model, when roles switched it); without it, the
        agent's default model window is used.

        An automatic attempt that just failed, or found nothing to compact,
        is not retried until the session has grown by ``_BACKOFF_ENTRIES``
        entries (recorded in ``session.state["compaction_backoff"]``). A
        ``pre_compact`` hook that blocked an automatic attempt is not asked again
        (and its notice not repeated) for ``_BLOCKED_BACKOFF_ENTRIES`` entries.
        """
        window = window if window is not None else agent.model_info.context_window
        if estimate_tokens(agent.request(session)) < self.threshold * window:
            return
        backoff_at = session.state.get("compaction_backoff")
        if backoff_at is not None and len(session.entries()) - backoff_at < _BACKOFF_ENTRIES:
            return
        blocked_at = session.state.get("compaction_blocked")
        if blocked_at is not None:
            if len(session.entries()) - blocked_at < _BLOCKED_BACKOFF_ENTRIES:
                return
            del session.state["compaction_blocked"]
        async for event in self._run(agent, session, instructions="", automatic=True):
            yield event

    async def compact(self, agent, session, *, instructions: str = "") -> AsyncIterator[object]:
        """Summarise everything before the last ``keep_turns`` turns (manual; ignores backoff)."""
        async for event in self._run(agent, session, instructions=instructions, automatic=False):
            yield event

    async def _run(
        self, agent, session, *, instructions: str, automatic: bool
    ) -> AsyncIterator[object]:
        entries = session.entries()
        keys, distinct_turns = _distinct_turn_keys(entries)
        if len(distinct_turns) <= self.keep_turns:
            if automatic:
                session.state["compaction_backoff"] = len(entries)
            else:
                yield Notice("info", "nothing to compact yet")
            return
        if self.keep_turns > 0:
            oldest_kept = distinct_turns[-self.keep_turns]
            first_kept = next(i for i, key in enumerate(keys) if key == oldest_kept)
        else:
            first_kept = len(entries)
        old = entries[:first_kept]
        if not old:
            if automatic:
                session.state["compaction_backoff"] = len(entries)
            else:
                yield Notice("info", "nothing to compact yet")
            return
        outcome = await agent.hooks.run(
            "pre_compact", {"session_id": session.id, "messages": len(old)}
        )
        for note in outcome.notices:
            yield Notice("info", note)
        if outcome.block:
            if automatic:
                session.state["compaction_blocked"] = len(session.entries())
            yield Notice("info", f"compaction skipped: {outcome.message or 'blocked by a hook'}")
            return
        previous = next(
            (r.payload["summary"] for r in reversed(session.records()) if r.type == "compacted"),
            None,
        )
        transcript = _render([e.message for e in old], previous)
        summarize_model = self.model or agent.model
        window = catalog.lookup(summarize_model, agent.model_overrides).context_window
        transcript = _trim_transcript(transcript, int(window * _TRIM_RATIO))
        if instructions:
            transcript += f"\n\nFocus of the summary: {instructions}"
        provider = self.provider or agent.provider
        request = ModelRequest(
            model=summarize_model,
            system=SUMMARY_PROMPT,
            messages=[Message.user(transcript)],
            max_tokens=self.max_summary_tokens,
        )
        acc = StreamAccumulator(provider.name)
        try:
            async for event in provider.stream(request):
                acc.feed(event)
        except ProviderError as exc:
            if automatic:
                session.state["compaction_backoff"] = len(session.entries())
            yield Notice("warn", f"compaction failed: {exc}")
            return
        summary = acc.message().text.strip()
        if not summary:
            if automatic:
                session.state["compaction_backoff"] = len(session.entries())
            yield Notice("warn", "compaction failed: the model returned an empty summary")
            return
        session.state.pop("compaction_backoff", None)
        session.append(
            "compacted", {"summary": summary, "upto_seq": old[-1].seq, "removed": len(old)}
        )
        yield Compacted(summary, len(old))


__all__ = ["Compactor", "SUMMARY_PROMPT", "estimate_tokens"]
