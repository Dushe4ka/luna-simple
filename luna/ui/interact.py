"""Arrow-key interactive prompts (questionary/prompt_toolkit), with a fallback.

Plain-text fallback for non-interactive use.

Every existing prompt in Luna already accepts a plain
``input_fn: Callable[[str], str]`` for testability — tests script it
with a function that returns canned strings, since a real ``input()``
call would block forever with no real stdin attached. This module adds
arrow-key selection ONLY when a real interactive terminal is confirmed
present; otherwise it returns ``None`` so the caller's own existing
``input_fn(...)`` prompt runs completely unchanged. ``None`` is never
used to mean "user cancelled" — a real cancel (Ctrl-C/Ctrl-D during the
questionary prompt) raises ``KeyboardInterrupt`` instead, matching how
every other prompt in Luna already lets Ctrl-C propagate.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from rich.console import Console


def _real_terminal(console: Console, input_fn: Callable[[str], str]) -> bool:
    return input_fn is input and console.is_terminal and sys.stdin.isatty()


def arrow_pick(
    console: Console,
    input_fn: Callable[[str], str],
    options: list[tuple[str, str]],
    *,
    default: str | None = None,
) -> str | None:
    """Arrow-key-select one of ``options`` (``(value, label)`` pairs).

    Returns the chosen ``value``, or ``None`` when no real interactive
    terminal is present — the caller must fall back to its own existing
    prompt in that case, unchanged.
    """
    if not _real_terminal(console, input_fn):
        return None
    import questionary

    result = questionary.select(
        "",
        choices=[questionary.Choice(label, value=value) for value, label in options],
        default=default,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def arrow_confirm(
    console: Console,
    input_fn: Callable[[str], str],
    message: str,
    *,
    default: bool = False,
) -> bool | None:
    """Arrow-key yes/no toggle. ``None`` when not interactive (see :func:`arrow_pick`)."""
    if not _real_terminal(console, input_fn):
        return None
    import questionary

    result = questionary.confirm(message, default=default).ask()
    if result is None:
        raise KeyboardInterrupt
    return result
