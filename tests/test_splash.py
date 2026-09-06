import io

from rich.console import Console

from luna.ui.splash import render_splash


def _console(width):
    # force_terminal so the scene path runs; no_color so assertions see plain text
    return Console(file=io.StringIO(), width=width, force_terminal=True, no_color=True)


def test_renders_at_various_widths():
    for w in (40, 80, 200):
        c = _console(w)
        render_splash(c, steps=["loading modules", "connecting to tools"], animate=False)
        out = c.file.getvalue()
        assert "LUNA" in out
        assert "loading modules" in out


def test_scene_has_wordmark_and_taglines():
    c = _console(118)
    render_splash(c, animate=False)
    out = c.file.getvalue()
    assert "YOUR AI AGENT COMPANION" in out
    assert "INITIALIZING ..." in out
    assert "SAME MOON" in out
    assert "IDEAS" in out and "HUMAN" in out


def test_compact_fallback_is_used_when_narrow():
    c = _console(70)
    render_splash(c, animate=False)
    out = c.file.getvalue()
    assert "YOUR AI AGENT COMPANION" in out
    assert "> loading modules ..." in out
