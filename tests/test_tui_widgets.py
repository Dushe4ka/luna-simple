from textual.app import App, ComposeResult

from luna.tui.widgets import PulseGlyph


class _HarnessApp(App):
    def compose(self) -> ComposeResult:
        yield PulseGlyph("Luna думает", id="glyph")


async def test_pulse_glyph_shows_a_twinkling_glyph_and_its_label_by_default():
    app = _HarnessApp()
    async with app.run_test():
        glyph = app.query_one("#glyph", PulseGlyph)
        text = str(glyph.render())
        assert "Luna думает" in text
        assert any(g in text for g in ("✳", "✦", "✧"))


async def test_pulse_glyph_falls_back_to_a_static_line_when_animations_are_disabled():
    """Regression: a hand-rolled animation that ignores
    ``App.animation_level`` (the ``TEXTUAL_ANIMATIONS=none`` setting)
    keeps blinking regardless of the user's reduced-motion preference —
    Textual's own LoadingIndicator falls back to plain static text under
    this same setting, and PulseGlyph must match that.
    """
    app = _HarnessApp()
    app.animation_level = "none"
    async with app.run_test():
        glyph = app.query_one("#glyph", PulseGlyph)
        text = str(glyph.render())
        assert text == "⏺ Luna думает"
        assert glyph.auto_refresh is None


async def test_pulse_glyph_appends_a_live_elapsed_counter_when_started_at_is_given():
    import time

    class _TimedHarness(App):
        def compose(self) -> ComposeResult:
            yield PulseGlyph("read_file", started_at=time.monotonic(), id="glyph")

    app = _TimedHarness()
    async with app.run_test():
        glyph = app.query_one("#glyph", PulseGlyph)
        text = str(glyph.render())
        assert "read_file" in text
        assert "0s" in text
