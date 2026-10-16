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

import asyncio
import sys
import threading
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


async def in_thread(fn: Callable, /, *args, **kwargs):
    """Run a blocking prompt (or handler) in a daemon thread and await its result.

    Unlike ``asyncio.to_thread`` the thread is a daemon outside the default
    executor, so a prompt still blocked on stdin when the loop stops (Ctrl-C,
    exit) never holds up interpreter shutdown. A ``StopIteration`` (a scripted
    ``input_fn`` that ran out of answers) arrives as ``EOFError``.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(result, exc) -> None:
        if future.done():
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def post(result, exc) -> None:
        try:
            loop.call_soon_threadsafe(deliver, result, exc)
        except RuntimeError:  # the loop is gone: nobody is waiting any more
            pass

    def work() -> None:
        try:
            result = fn(*args, **kwargs)
        except StopIteration:
            post(None, EOFError("input exhausted"))
        except BaseException as exc:  # noqa: BLE001 - handed to the awaiting coroutine
            post(None, exc)
        else:
            post(result, None)

    threading.Thread(target=work, daemon=True, name="luna-prompt").start()
    return await future
