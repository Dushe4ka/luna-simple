"""Colour palette and Rich theme, drawn from the Luna splash artwork."""

from rich.theme import Theme

PALETTE: dict[str, str] = {
    "bg": "#0b1026",
    "moon": "#e8ecff",
    "moon_dim": "#aab4e8",
    "peri": "#8a9cff",
    "blue": "#5566a8",
    "mauve": "#b98cc9",
    "accent": "#cdd6ff",
}

LUNA_THEME = Theme(
    {
        "luna.title": f"bold {PALETTE['moon']}",
        "luna.moon": PALETTE["moon"],
        "luna.moon.dim": PALETTE["moon_dim"],
        "luna.tagline": f"italic {PALETTE['peri']}",
        "luna.slogan": PALETTE["blue"],
        "luna.slogan.accent": PALETTE["mauve"],
        "luna.step": PALETTE["peri"],
        "luna.prompt": f"bold {PALETTE['peri']}",
        "luna.tool": PALETTE["blue"],
        "luna.tool.name": f"bold {PALETTE['accent']}",
        "luna.warn": f"bold {PALETTE['mauve']}",
        "luna.error": "bold red",
        "luna.ok": "bold green",
    }
)
