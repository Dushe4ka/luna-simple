import io

from rich.console import Console

from luna.ui.splash import render_splash


def _console(width):
    return Console(file=io.StringIO(), width=width, force_terminal=True, color_system="truecolor")


def test_renders_at_various_widths():
    for w in (40, 80, 200):
        c = _console(w)
        render_splash(c, steps=["loading modules", "connecting to tools"], animate=False)
        out = c.file.getvalue()
        assert "LUNA" in out
        assert "loading modules" in out


def test_contains_companion_line_and_slogan():
    c = _console(100)
    render_splash(c, animate=False)
    out = c.file.getvalue()
    assert "YOUR AI AGENT COMPANION" in out
    assert "SAME MOON" in out.upper()
