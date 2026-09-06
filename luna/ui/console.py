"""Shared Rich console and small rendering helpers."""

from __future__ import annotations

from contextlib import contextmanager

from rich.console import Console
from rich.markdown import Markdown

from luna.ui.theme import LUNA_THEME

_console: Console | None = None


def get_console(*, force_terminal: bool | None = None) -> Console:
    """Return the process-wide Luna console (created on first use)."""
    global _console
    if _console is None or force_terminal is not None:
        _console = Console(theme=LUNA_THEME, force_terminal=force_terminal, highlight=False)
    return _console


def render_markdown(console: Console, text: str) -> None:
    """Render assistant output as Markdown."""
    if text.strip():
        console.print(Markdown(text))


@contextmanager
def spinner(console: Console, label: str):
    """Context manager showing a status spinner while work runs."""
    with console.status(f"[luna.step]{label}[/]", spinner="dots"):
        yield
