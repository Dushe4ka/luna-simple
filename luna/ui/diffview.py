"""Render a unified diff as ``rich`` text styled with the theme's ``luna.diff.*`` roles."""

from __future__ import annotations

from rich.text import Text


def _line_style(line: str) -> str:
    if line.startswith(("+++", "---")):
        return "luna.muted"
    if line.startswith("@@"):
        return "luna.diff.hunk"
    if line.startswith("+"):
        return "luna.diff.add"
    if line.startswith("-"):
        return "luna.diff.del"
    return "luna.diff.ctx"


def render_diff(text: str) -> Text:
    """Style each line of ``text`` by its diff role (added, removed, hunk, context)."""
    out = Text()
    for i, line in enumerate(text.split("\n")):
        if i:
            out.append("\n")
        out.append(line, style=_line_style(line))
    return out
