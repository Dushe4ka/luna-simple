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
from dataclasses import dataclass, field

from luna.config.usage import TurnUsage
from luna.core.persistence import make_title
from luna.core.session_state import SessionState
from luna.turn import diagnose, fmt, gitinfo, undo
from luna.turn.context import expand_mentions, render_pinned
from luna.turn.verify import run_verify

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


@dataclass
class TurnOutcome:
    """What the streaming layer observed during a turn."""

    usage: TurnUsage = field(default_factory=TurnUsage)
    tool_names: set[str] = field(default_factory=set)
    reload_requested: bool = False


@dataclass
class FinishResult:
    """What happens after a turn: notices, an optional fix-up turn, a reload."""

    notices: list[Notice]
    fixup_prompt: str | None
    reload: bool


def format_and_diagnose(cfg, before: list[str] | None) -> tuple[list[Notice], str]:
    """Format then diagnose the files this turn changed. Returns (notices, diagnose text).

    ``before`` is the ``dirty_paths`` snapshot captured before the turn ran; only
    paths that became newly dirty during the turn are passed to the format/
    diagnose commands, so a file the user had already changed before this turn
    started is left alone. ``None`` falls back to the whole-repo behavior.
    """
    notices: list[Notice] = []
    before_set = set(before) if before is not None else None
    is_repo = gitinfo.is_git_repo(cfg.workdir)

    def _touched_now() -> list[str]:
        if not is_repo:
            return []
        current = gitinfo.dirty_paths(cfg.workdir)
        return current if before_set is None else [p for p in current if p not in before_set]

    changed = _touched_now()
    if is_repo and before_set is not None and not changed:
        return notices, ""
    fmt_cmd = cfg.format_command
    if fmt_cmd == "auto":
        fmt_cmd = fmt.detect(cfg.workdir)
    if fmt_cmd:
        touched = fmt.run(fmt_cmd, cfg.workdir, changed)
        if touched:
            notices.append(Notice("dim", f"⌁ formatted {len(touched)} file(s)"))
    diag_cmd = cfg.diagnose_command
    if diag_cmd == "auto":
        diag_cmd = diagnose.detect(cfg.workdir)
    if not diag_cmd:
        return notices, ""
    changed = _touched_now()
    if is_repo and before_set is not None and not changed:
        # formatting can normalize a turn's edit back to the committed content
        return notices, ""
    text = diagnose.run(diag_cmd, cfg.workdir, changed)
    if text:
        notices.append(Notice("dim", text))
    return notices, text


def verify_step(cfg) -> tuple[bool, str, list[Notice]]:
    """Run the verify command once: (ok, output tail, notices)."""
    ok, tail = run_verify(cfg.verify_command, cfg.workdir)
    if ok:
        return True, "", [Notice("ok", "✓ verify ok")]
    return False, tail, [Notice("warn", f"verify failed\n{tail}")]


def fixup_prompt(cfg, tail: str) -> str:
    """Build the message for the single automatic fix-up turn after a failed verify."""
    return f"The verify command `{cfg.verify_command}` failed. Output:\n{tail}\nFix it."


def finish_turn(
    state: SessionState,
    prepared: PreparedTurn,
    outcome: TurnOutcome,
    *,
    cfg,
    index,
    thread_id: str,
    workdir: str,
) -> FinishResult:
    """Everything after a turn completed without a pending approval."""
    state.usage.add_turn(outcome.usage)
    notices: list[Notice] = []
    fix = None
    if outcome.tool_names & MUTATING:
        fmt_notices, diagnostics = format_and_diagnose(cfg, prepared.dirty_before)
        notices += fmt_notices
        state.pending_diagnostics = diagnostics
        if cfg.verify_command:
            ok, tail, verify_notices = verify_step(cfg)
            notices += verify_notices
            if not ok:
                fix = fixup_prompt(cfg, tail)
    if index is not None:
        index.record(thread_id, workdir, make_title(prepared.title_line))
        index.touch(thread_id)
    return FinishResult(notices=notices, fixup_prompt=fix, reload=outcome.reload_requested)


def finish_fixup(cfg) -> list[Notice]:
    """Re-run verify once after the fix-up turn."""
    ok, tail = run_verify(cfg.verify_command, cfg.workdir)
    if ok:
        return [Notice("ok", "✓ verify ok")]
    return [Notice("warn", f"⚠ verify still failing after 1 retry\n{tail}")]
