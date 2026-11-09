"""Shared Rich console and small rendering helpers."""

from __future__ import annotations

from contextlib import contextmanager

from rich.console import Console
from rich.markdown import Markdown
from rich.style import Style

from luna.ui.themes import ThemeSpec, active, rich_theme, set_active

_console: Console | None = None
#: Whether a theme pushed by :func:`use_theme` sits on top of ``_console``'s stack.
_pushed = False


def get_console(*, force_terminal: bool | None = None) -> Console:
    """Return the process-wide Luna console (created on first use)."""
    global _console, _pushed
    if _console is None or force_terminal is not None:
        _console = Console(
            theme=rich_theme(active()), force_terminal=force_terminal, highlight=False
        )
        _pushed = False
    return _console


def use_theme(spec: ThemeSpec) -> None:
    """Make ``spec`` the active theme and restyle the shared console with it."""
    global _pushed
    set_active(spec)
    console = get_console()
    if _pushed:
        console.pop_theme()
    console.push_theme(rich_theme(spec))
    _pushed = True


def theme_style(name: str) -> Style:
    """Resolve a ``luna.*`` style of the active theme to a concrete ``Style``.

    For renderables whose style rich looks up without a fallback (a panel
    border), so they also render on a console without the Luna theme.
    """
    return rich_theme(active()).styles.get(name, Style.null())


def render_markdown(console: Console, text: str) -> None:
    """Render assistant output as Markdown."""
    if text.strip():
        console.print(Markdown(text))


@contextmanager
def spinner(console: Console, label: str):
    """Context manager showing a status spinner while work runs."""
    with console.status(f"[luna.step]{label}[/]", spinner="dots"):
        yield
