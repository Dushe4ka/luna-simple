"""Human-in-the-loop approval prompt for mutating tool calls."""

from __future__ import annotations

import difflib
import os
from collections.abc import Callable

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from luna.permissions import suggest_rule
from luna.ui.theme import PALETTE

_MAX_PREVIEW_LINES = 40


def _truncate(text: str, limit: int = _MAX_PREVIEW_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join([*lines[:limit], f"… (+{len(lines) - limit} more lines)"])


def describe_action(action_request: dict) -> str:
    """Return a human-readable summary of a pending tool call."""
    action = action_request.get("action") or action_request.get("name") or "tool"
    args = action_request.get("args", {}) or {}

    if action == "execute":
        return f"$ {args.get('command', '')}"
    if action == "delete":
        return f"delete {args.get('file_path', args.get('path', '?'))}"
    if action == "write_file":
        path = args.get("file_path", "?")
        return f"write {path}\n\n{_truncate(args.get('content', ''))}"
    if action == "edit_file":
        path = args.get("file_path", "?")
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        diff = "\n".join(
            difflib.unified_diff(
                old.splitlines(), new.splitlines(), fromfile=path, tofile=path, lineterm=""
            )
        )
        return f"edit {path}\n\n{_truncate(diff)}"
    rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{action}({rendered})"


def _panel(action_request: dict) -> Panel:
    action = action_request.get("action") or action_request.get("name") or "tool"
    body = describe_action(action_request)
    lexer = "diff" if action == "edit_file" else "bash" if action == "execute" else "text"
    return Panel(
        Syntax(body, lexer, theme="ansi_dark", word_wrap=True),
        title=f"[bold {PALETTE['accent']}]{action}[/] wants to run",
        border_style=PALETTE["mauve"],
    )


def prompt_decision(
    console: Console,
    action_request: dict,
    *,
    input_fn: Callable[[str], str] = input,
) -> dict:
    """Show the pending action and collect the user's decision.

    Returns a decision dict for ``Command(resume={"decisions": [...]})``.
    """
    console.print(_panel(action_request))
    choice = input_fn("[Enter] approve · [e] edit · [a] always · [n] reject > ").strip().lower()

    if choice in ("", "y", "yes"):
        return {"type": "approve"}

    if choice in ("n", "no"):
        reason = input_fn("reason > ").strip()
        return {"type": "reject", "message": reason or "rejected by user"}

    if choice in ("a", "always"):
        action = action_request.get("action") or action_request.get("name")
        args = action_request.get("args", {}) or {}
        rule = suggest_rule(action, args, ".")
        edited = input_fn(f"rule [{rule}] > ").strip()
        return {"type": "approve", "always": edited or rule}

    if choice in ("e", "edit"):
        action = action_request.get("action") or action_request.get("name")
        args = dict(action_request.get("args", {}) or {})
        if action == "execute":
            replacement = input_fn("new command > ").strip()
            if replacement:
                args["command"] = replacement
                return {"type": "edit", "args": args}
        elif os.environ.get("EDITOR"):
            console.print(
                Text(
                    "inline editing is not wired yet; approving as-is",
                    style=PALETTE["mauve"],
                )
            )
        return {"type": "approve"}

    return {"type": "approve"}
