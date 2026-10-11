"""Summarise old history when the context window fills up."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

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


@dataclass
class Compactor:
    """Compaction policy; plug into ``Agent(compactor=...)``."""

    threshold: float = 0.8
    keep_turns: int = 4
    provider: Provider | None = None
    model: str | None = None
    max_summary_tokens: int = 4000

    async def maybe_compact(self, agent, session) -> AsyncIterator[object]:
        """Compact when the next request would exceed the threshold."""
        window = agent.model_info.context_window
        if estimate_tokens(agent.request(session)) < self.threshold * window:
            return
        async for event in self.compact(agent, session):
            yield event

    async def compact(self, agent, session, *, instructions: str = "") -> AsyncIterator[object]:
        """Summarise everything before the last ``keep_turns`` user messages."""
        entries = session.entries()
        user_positions = [i for i, e in enumerate(entries) if e.message.role == "user"]
        if len(user_positions) <= self.keep_turns:
            yield Notice("info", "nothing to compact yet")
            return
        first_kept = user_positions[-self.keep_turns] if self.keep_turns > 0 else len(entries)
        old = entries[:first_kept]
        if not old:
            yield Notice("info", "nothing to compact yet")
            return
        outcome = await agent.hooks.run(
            "pre_compact", {"session_id": session.id, "messages": len(old)}
        )
        for note in outcome.notices:
            yield Notice("info", note)
        if outcome.block:
            yield Notice("info", f"compaction skipped: {outcome.message or 'blocked by a hook'}")
            return
        previous = next(
            (r.payload["summary"] for r in reversed(session.records()) if r.type == "compacted"),
            None,
        )
        transcript = _render([e.message for e in old], previous)
        if instructions:
            transcript += f"\n\nFocus of the summary: {instructions}"
        provider = self.provider or agent.provider
        request = ModelRequest(
            model=self.model or agent.model,
            system=SUMMARY_PROMPT,
            messages=[Message.user(transcript)],
            max_tokens=self.max_summary_tokens,
        )
        acc = StreamAccumulator(provider.name)
        try:
            async for event in provider.stream(request):
                acc.feed(event)
        except ProviderError as exc:
            yield Notice("warn", f"compaction failed: {exc}")
            return
        summary = acc.message().text.strip()
        if not summary:
            yield Notice("warn", "compaction failed: the model returned an empty summary")
            return
        session.append(
            "compacted", {"summary": summary, "upto_seq": old[-1].seq, "removed": len(old)}
        )
        yield Compacted(summary, len(old))


__all__ = ["Compactor", "SUMMARY_PROMPT", "estimate_tokens"]
