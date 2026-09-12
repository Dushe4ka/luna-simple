import io

from rich.console import Console

from luna.ui.splash import (
    _gradient_stops,
    _hex_to_rgb,
    _lerp_rgb,
    _rgb_to_hex,
    render_splash,
)


def test_hex_to_rgb_and_back_roundtrip():
    assert _hex_to_rgb("#8a9cff") == (0x8A, 0x9C, 0xFF)
    assert _rgb_to_hex((0x8A, 0x9C, 0xFF)) == "#8a9cff"
    for hexcolor in ("#000000", "#ffffff", "#5566a8"):
        assert _rgb_to_hex(_hex_to_rgb(hexcolor)) == hexcolor


def test_lerp_rgb_endpoints_return_exact_colors():
    a, b = (10, 20, 30), (200, 100, 0)
    assert _lerp_rgb(a, b, 0.0) == a
    assert _lerp_rgb(a, b, 1.0) == b


def test_lerp_rgb_midpoint_is_the_average():
    a, b = (0, 0, 0), (100, 200, 50)
    assert _lerp_rgb(a, b, 0.5) == (50, 100, 25)


def test_gradient_stops_produces_requested_length():
    stops = _gradient_stops(["#000000", "#ffffff"], 5)
    assert len(stops) == 5
    assert stops[0] == "#000000"
    assert stops[-1] == "#ffffff"


def test_gradient_stops_passes_through_middle_colors():
    # a 3-color gradient sampled at exactly 3 steps must hit each stop exactly
    stops = _gradient_stops(["#ff0000", "#00ff00", "#0000ff"], 3)
    assert stops == ["#ff0000", "#00ff00", "#0000ff"]


def test_gradient_stops_single_step_returns_first_color():
    assert _gradient_stops(["#111111", "#222222"], 1) == ["#111111"]


def _console(width):
    # force_terminal so the wordmark path runs; no_color so assertions see plain text
    return Console(file=io.StringIO(), width=width, force_terminal=True, no_color=True)


def test_renders_at_various_widths():
    for w in (40, 80, 200):
        c = _console(w)
        render_splash(c, steps=["loading modules", "connecting to tools"], animate=False)
        out = c.file.getvalue()
        assert "loading modules" in out


def test_full_splash_has_wordmark_version_and_tagline():
    c = _console(118)
    render_splash(c, animate=False)
    out = c.file.getvalue()
    assert "YOUR AI AGENT COMPANION" in out
    assert "INITIALIZING ..." in out
    assert "AI AGENT HARNESS" in out
    # the block-letter wordmark itself: no literal "LUNA" text, just glyphs
    assert "█" in out


def test_compact_fallback_is_used_when_narrow():
    c = _console(70)
    render_splash(c, animate=False)
    out = c.file.getvalue()
    assert "LUNA" in out
    assert "YOUR AI AGENT COMPANION" in out
    assert "> loading modules ..." in out


def test_wordmark_emits_truecolor_ansi_codes():
    """Guards against a silent regression to flat/no color: the whole point
    of the gradient wordmark is a real RGB gradient, not a flat color."""
    c = Console(file=io.StringIO(), width=118, force_terminal=True, color_system="truecolor")
    render_splash(c, animate=False)
    out = c.file.getvalue()
    assert "\x1b[38;2;" in out  # a 24-bit truecolor foreground escape
