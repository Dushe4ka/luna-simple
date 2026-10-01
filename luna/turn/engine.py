"""The before/after-turn pipeline shared by the REPL and the local server.

Streaming stays transport-specific (the REPL renders to a console and asks
for approvals inline; the server streams SSE and resumes via ``/approve``),
but everything around it lives here once: ``@agent`` delegation, ``@file``
expansion, pinned files, carried-over diagnostics and the undo checkpoint
before a turn; usage, the session index, format + diagnose, verify with one
fix-up turn and auto-reload after it. Functions return :class:`Notice`s
instead of printing, so each surface renders them its own way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from luna.core.session_state import SessionState
from luna.turn import gitinfo, undo
from luna.turn.context import expand_mentions, render_pinned

#: Tool names whose use marks a turn as mutating and triggers format/verify.
MUTATING = frozenset({"write_file", "edit_file", "delete", "execute"})

#: Matches a line invoking a subagent by name, e.g. ``@researcher do X``.
AT_AGENT_RE = re.compile(r"^@([\w-]+)\s+(.+)$", re.DOTALL)


@dataclass(frozen=True)
class Notice:
    """One short status line for the user: ``level`` is dim/info/ok/warn/error."""

    level: str
    text: str


@dataclass
class PreparedTurn:
    """What :func:`prepare_turn` hands to the streaming layer."""

    content: str
    title_line: str
    dirty_before: list[str]


def delegate_line(line: str, subagent_names: set[str]) -> str:
    """Rewrite ``@name text`` into a delegation request when ``name`` is a subagent."""
    match = AT_AGENT_RE.match(line)
    if match and match.group(1) in subagent_names:
        return (
            f"Delegate this to the '{match.group(1)}' subagent using the "
            f"task tool: {match.group(2)}"
        )
    return line


def prepare_turn(
    state: SessionState,
    line: str,
    *,
    agent,
    thread_id: str,
    workdir: str,
    session_id: str,
    subagent_names: set[str],
) -> PreparedTurn:
    """Build the message content for a turn and checkpoint files for undo."""
    line = delegate_line(line, subagent_names)
    config = {"configurable": {"thread_id": thread_id}}
    try:
        current_messages = agent.get_state(config).values.get("messages", [])
    except Exception:  # noqa: BLE001 - a stub/broken agent must not block the turn
        current_messages = []
    undo.begin_turn(workdir, session_id, len(current_messages))
    # captured *after* begin_turn so its own journal writes don't register as
    # "newly dirty" when .luna/ isn't gitignored
    dirty_before = gitinfo.dirty_paths(workdir) if gitinfo.is_git_repo(workdir) else []
    diag_block = (
        f"<diagnostics>\n{state.pending_diagnostics}\n</diagnostics>\n\n"
        if state.pending_diagnostics
        else ""
    )
    state.pending_diagnostics = ""
    pinned_block = render_pinned(state.pinned, workdir)
    content = (
        diag_block
        + (pinned_block + "\n\n" if pinned_block else "")
        + expand_mentions(line, workdir)
    )
    return PreparedTurn(content=content, title_line=line, dirty_before=dirty_before)
