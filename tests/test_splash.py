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


def test_scene_emits_truecolor_ansi_codes():
    """Guards against a silent regression to flat/no color: the whole point
    of the pixel-scene rewrite is a real RGB gradient, not a small named
    palette — so real truecolor escape codes must show up in the output."""
    c = Console(file=io.StringIO(), width=118, force_terminal=True, color_system="truecolor")
    render_splash(c, animate=False)
    out = c.file.getvalue()
    assert "\x1b[38;2;" in out  # a 24-bit truecolor foreground escape


def test_moon_shading_falls_off_from_center_to_edge():
    """The moon must be a smooth gradient, not the old binary
    "one glyph inside, another outside" disc — brightness should decrease
    monotonically-ish from center to edge, not jump in two flat bands."""
    from luna.ui.splash import _moon, _Scene

    scene = _Scene(60, 30, (0, 0, 0))
    cx, cy, r = 30, 30, 15
    _moon(scene, cx, cy, r)
    center = sum(scene.pixel_at(cx, cy))
    mid = sum(scene.pixel_at(cx + r // 2, cy))
    edge = sum(scene.pixel_at(cx + r - 1, cy))
    assert center >= mid >= edge


def test_sky_gradient_top_and_bottom_differ():
    from luna.ui.splash import _Scene, _sky

    scene = _Scene(40, 20, (0, 0, 0))
    _sky(scene, "#0b1026", "#161c3d")
    assert scene.pixel_at(0, 0) != scene.pixel_at(0, scene.h - 1)
