"""Render a unified diff as ``rich`` text styled with the theme's ``luna.diff.*`` roles."""

from __future__ import annotations

from collections.abc import Mapping

from rich.text import Text

#: Line role -> ``rich`` theme style, used when no explicit palette is given.
_THEME_STYLES = {
    "header": "luna.muted",
    "hunk": "luna.diff.hunk",
    "add": "luna.diff.add",
    "del": "luna.diff.del",
    "ctx": "luna.diff.ctx",
}


def _palette_styles(palette: Mapping[str, str]) -> dict[str, str]:
    """Explicit styles from theme tokens (for surfaces without the ``luna.*`` rich theme)."""
    return {
        "header": palette["muted"],
        "hunk": palette["primary"],
        "add": f"{palette['add']} on {palette['add_bg']}",
        "del": f"{palette['del']} on {palette['del_bg']}",
        "ctx": palette["muted"],
    }


def _line_role(line: str, in_hunk: bool) -> str:
    # ``---``/``+++`` are file headers only before the first hunk: inside one
    # they are a removed ``-- x`` / an added ``++ x`` content line.
    if not in_hunk and line.startswith(("+++", "---")):
        return "header"
    if line.startswith("@@"):
        return "hunk"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "del"
    return "ctx"


def render_diff(text: str, palette: Mapping[str, str] | None = None) -> Text:
    """Style each line of ``text`` by its diff role (added, removed, hunk, context).

    ``palette`` maps theme tokens (``muted``, ``primary``, ``add``, ``add_bg``,
    ``del``, ``del_bg``) to ``rich`` colors; ``None`` uses the ``luna.diff.*``
    styles of the active ``rich`` theme.
    """
    styles = _THEME_STYLES if palette is None else _palette_styles(palette)
    out = Text()
    in_hunk = False
    for i, line in enumerate(text.split("\n")):
        if i:
            out.append("\n")
        role = _line_role(line, in_hunk)
        in_hunk = in_hunk or role == "hunk"
        out.append(line, style=styles[role])
    return out
