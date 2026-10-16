"""The app's turn pipeline: message preparation and the format/diagnose/verify hooks.

Streaming lives in the surfaces (REPL renderer, server SSE); everything
around a turn is here once and runs as luna-core hooks, so the REPL, the
server and background runs behave the same.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from luna_core.context import expand_mentions, render_pinned
from luna_core.hooks import HookResult, Hooks
from luna_core.messages import Image
from luna_core.workspace import Workspace

from luna.turn import diagnose, fmt, gitinfo
from luna.turn.verify import run_verify

__all__ = [
    "AT_AGENT_RE",
    "MUTATING",
    "Notice",
    "PreparedTurn",
    "before_turn",
    "delegate_line",
    "fixup_prompt",
    "format_and_diagnose",
    "make_hooks",
    "notice_level",
    "prepare_turn",
    "verify_step",
]

#: Tools whose successful use marks a turn as mutating (format/diagnose/verify run after it).
MUTATING = frozenset({"write_file", "edit_file", "delete", "execute"})
#: Matches a line invoking a subagent by name, e.g. ``@researcher do X``.
AT_AGENT_RE = re.compile(r"^@([\w-]+)\s+(.+)$", re.DOTALL)


@dataclass(frozen=True)
class Notice:
    """One short status line: ``level`` is dim/info/ok/warn/error."""

    level: str
    text: str


@dataclass
class PreparedTurn:
    """What :func:`prepare_turn` hands to the surface."""

    text: str
    images: list[Image] = field(default_factory=list)
    title_line: str = ""


def delegate_line(line: str, subagent_names: set[str]) -> str:
    """Rewrite ``@name text`` into a delegation request when ``name`` is a subagent."""
    match = AT_AGENT_RE.match(line)
    if match and match.group(1) in subagent_names:
        return (
            f"Delegate this to the '{match.group(1)}' subagent using the "
            f"task tool: {match.group(2)}"
        )
    return line


def prepare_turn(state, line: str, *, workdir: str, subagent_names: set[str]) -> PreparedTurn:
    """Build the user message: carried diagnostics, pinned files, ``@mentions``."""
    title = line
    line = delegate_line(line, subagent_names)
    diag = (
        f"<diagnostics>\n{state.pending_diagnostics}\n</diagnostics>\n\n"
        if state.pending_diagnostics
        else ""
    )
    state.pending_diagnostics = ""
    pinned = render_pinned(state.pinned, workdir)
    text, images = expand_mentions(line, Workspace(workdir))
    return PreparedTurn(diag + (pinned + "\n\n" if pinned else "") + text, images, title)


def _own_path(path: str) -> bool:
    """Luna's own state (``.luna/``: undo journal, tool output) is never formatted."""
    return path == ".luna" or path.startswith(".luna/")


def _dirty(workdir: str) -> list[str]:
    return [p for p in gitinfo.dirty_paths(workdir) if not _own_path(p)]


def before_turn(state) -> None:
    """Reset per-turn pipeline state and capture which files were already dirty."""
    state.mutated = False
    state.fixup_turn = None
    state.dirty_before = _dirty(state.workdir) if gitinfo.is_git_repo(state.workdir) else []


def format_and_diagnose(cfg, before: list[str] | None) -> tuple[list[Notice], str]:
    """Format, then diagnose, the files this turn changed. Returns (notices, diagnostics).

    ``before`` is the dirty-path snapshot taken by :func:`before_turn`: only
    paths that became dirty during the turn are formatted/diagnosed, so a
    file the user had already changed is left alone. ``None`` means the
    whole project.
    """
    notices: list[Notice] = []
    before_set = set(before) if before is not None else None
    is_repo = gitinfo.is_git_repo(cfg.workdir)

    def touched() -> list[str]:
        if not is_repo:
            return []
        current = _dirty(cfg.workdir)
        return current if before_set is None else [p for p in current if p not in before_set]

    changed = touched()
    if is_repo and before_set is not None and not changed:
        return notices, ""
    fmt_cmd = fmt.detect(cfg.workdir) if cfg.format_command == "auto" else cfg.format_command
    if fmt_cmd:
        formatted = fmt.run(fmt_cmd, cfg.workdir, changed)
        if formatted:
            notices.append(Notice("dim", f"⌁ formatted {len(formatted)} file(s)"))
    diag_cmd = (
        diagnose.detect(cfg.workdir) if cfg.diagnose_command == "auto" else cfg.diagnose_command
    )
    if not diag_cmd:
        return notices, ""
    changed = touched()
    if is_repo and before_set is not None and not changed:
        # formatting can normalise a turn's edit back to the committed content
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
    """Build the message for the single automatic fix-up after a failed verify."""
    return f"The verify command `{cfg.verify_command}` failed. Output:\n{tail}\nFix it."


def notice_level(text: str) -> str:
    """Level for a hook notice by its leading marker (the old pipeline's colours)."""
    if text.startswith("✓"):
        return "ok"
    if text.startswith(("⚠", "verify failed")):
        return "warn"
    if text.startswith("⌁") or "\n" in text:
        return "dim"
    return "info"


def make_hooks(state_fn: Callable[[], object], cfg_fn: Callable[[], object]) -> Hooks:
    """Build the format/diagnose/verify pipeline as luna-core hooks for one session.

    ``state_fn`` returns the current :class:`~luna.core.session_state.SessionState`
    (``None`` disables the pipeline), ``cfg_fn`` the current config. A failed
    verify on a turn's first ``turn_end`` blocks with the fix-up prompt (the
    core loop feeds it back); the next ``turn_end`` of that turn re-checks
    once and never asks again.
    """
    hooks = Hooks()

    async def post_tool(payload: dict) -> HookResult | None:
        state = state_fn()
        if state is not None and payload.get("ok") and payload.get("tool") in MUTATING:
            state.mutated = True
        return None

    async def turn_end(payload: dict) -> HookResult | None:
        state, cfg = state_fn(), cfg_fn()
        if state is None:
            return None
        turn_id = payload.get("turn_id")
        in_fixup = state.fixup_turn is not None and state.fixup_turn == turn_id
        if not state.mutated and not in_fixup:
            return None
        mutated, state.mutated = state.mutated, False
        texts: list[str] = []
        if mutated:
            notices, diagnostics = await asyncio.to_thread(
                format_and_diagnose, cfg, state.dirty_before
            )
            state.pending_diagnostics = diagnostics
            texts = [n.text for n in notices]
        if not cfg.verify_command:
            return HookResult(notices=tuple(texts))
        ok, tail, verify_notices = await asyncio.to_thread(verify_step, cfg)
        if ok:
            return HookResult(notices=(*texts, *(n.text for n in verify_notices)))
        if in_fixup:
            return HookResult(notices=(*texts, f"⚠ verify still failing after 1 retry\n{tail}"))
        state.fixup_turn = turn_id
        return HookResult(
            block=True,
            message=fixup_prompt(cfg, tail),
            notices=(*texts, *(n.text for n in verify_notices)),
        )

    hooks.add("post_tool", post_tool, timeout=10)
    hooks.add("turn_end", turn_end, timeout=900)
    return hooks
