"""The TUI runs on registered Textual themes built from Luna's theme tokens."""

import pytest
from textual.color import Color

from luna.ui import themes
from tests.tui_fakes import make_app


@pytest.fixture(autouse=True)
def _reset_active_theme(monkeypatch):
    """``apply_theme_choice`` sets the process-wide active theme: keep it per test."""
    monkeypatch.setattr(themes, "_active", None)


async def test_theme_switch_changes_background():
    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        pane = app.query_one("ChatPane")
        assert pane.styles.background == Color.parse("#0e1117")
        app.apply_theme_choice("daymoon")
        await pilot.pause()
        assert pane.styles.background == Color.parse("#f8f7f3")
        assert app.theme_choice == "daymoon"
        assert themes.active().id == "daymoon"


async def test_auto_uses_terminal_background():
    app = make_app(theme_choice="auto", dark_terminal=False)
    async with app.run_test():
        assert app.theme == "luna-daymoon"
        assert app.theme_choice == "auto"
        assert app.dark_terminal is False


async def test_every_available_theme_is_registered():
    app = make_app()
    for spec in themes.available_themes():
        assert themes.textual_name(spec) in app.available_themes


def _mark_color(head) -> str:
    segs = [s for s in head.render_line(0) if s.text.strip() in ("✓", "✗")]
    assert segs, "status mark not rendered"
    return segs[0].style.color.triplet.hex


async def test_tool_mark_follows_theme():
    from luna.tui.tool_row import ToolRow

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        row = ToolRow("read_file", "a.py", result=(True, "ok"))
        await app.query_one("#transcript").mount(row)
        await pilot.pause()
        assert row.has_class("-ok")
        head = row.query_one(".tool-head")
        assert "✓" in str(head.render())
        assert _mark_color(head) == "#7fd3a0"
        app.apply_theme_choice("moonlight-cb")
        await pilot.pause()
        assert _mark_color(head) == "#6cb8ee"


async def test_failed_tool_row_has_cross_and_err_class():
    from luna.tui.tool_row import ToolRow

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        row = ToolRow("execute", "false", result=(False, "exit 1"))
        await app.query_one("#transcript").mount(row)
        await pilot.pause()
        assert row.has_class("-err") and not row.has_class("-ok")
        head = row.query_one(".tool-head")
        assert "✗" in str(head.render())
        assert _mark_color(head) == "#f0817b"


async def test_running_tool_row_has_running_class():
    from luna.tui.tool_row import ToolRow

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        row = ToolRow("read_file", "a.py")
        await app.query_one("#transcript").mount(row)
        await pilot.pause()
        assert row.has_class("-running")
        await row.finish(True, "")
        assert row.has_class("-ok") and not row.has_class("-running")


async def test_notice_row_level_is_a_css_class():
    from luna.tui.chat import NoticeRow

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        row = NoticeRow("warn", "verify failed")
        await app.query_one("#transcript").mount(row)
        await pilot.pause()
        assert row.has_class("-warn")
        assert row.styles.color == Color.parse("#e6b866")


async def test_single_pulse_timer():
    from luna.tui.widgets import PulseGlyph

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        await app.query_one("#transcript").mount(PulseGlyph("a"), PulseGlyph("b"))
        await pilot.pause()
        assert all(g.auto_refresh is None for g in app.query(PulseGlyph))
        assert app._pulse_timer is not None


async def test_pulse_refreshes_every_glyph(monkeypatch):
    from luna.tui.widgets import PulseGlyph

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        glyphs = [PulseGlyph("a"), PulseGlyph("b")]
        await app.query_one("#transcript").mount(*glyphs)
        await pilot.pause()
        refreshed = []
        for g in glyphs:
            monkeypatch.setattr(g, "refresh", lambda *a, g=g, **k: refreshed.append(g))
        app._pulse()
        assert set(map(id, refreshed)) >= set(map(id, glyphs))


async def test_pulse_color_breathes_between_primary_and_accent():
    from luna.tui.widgets import PulseGlyph

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        glyph = PulseGlyph("a")
        await app.query_one("#transcript").mount(glyph)
        await pilot.pause()
        # a quarter of the 3.2 s breath is the crest (exactly accent), 3/4 the trough
        assert glyph._color(0.8).lower() == "#e3c58f"
        assert glyph._color(2.4).lower() == "#a3b5ff"


async def test_pulse_color_is_static_primary_on_ansi_theme():
    from luna.tui.widgets import PulseGlyph

    app = make_app(theme_choice="ansi-dark")
    async with app.run_test() as pilot:
        glyph = PulseGlyph("a")
        await app.query_one("#transcript").mount(glyph)
        await pilot.pause()
        primary = app.theme_variables["primary"]
        assert primary.startswith("ansi_")
        assert {glyph._color(t) for t in (0.0, 0.8, 1.6, 2.4)} == {primary}
        assert primary in repr(glyph.render())


async def test_motion_off():
    app = make_app(theme_choice="moonlight", motion=False)
    async with app.run_test():
        assert app.animation_level == "none"
        assert app._pulse_timer is None


def _banner_colors(group) -> set[str]:
    colors = set()
    for align in group.renderables:
        text = align.renderable
        colors.add(str(text.style).lower())
        colors.update(str(span.style).lower() for span in text.spans)
    return colors


async def test_banner_reads_theme_colors_at_render():
    from luna.tui.banner import LunaBanner

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        banner = app.query_one(LunaBanner)
        before = _banner_colors(banner.render())
        # primary (version line), accent (tagline), strong (lit moon), border (shadow)
        assert {"#a3b5ff", "#e3c58f", "#f2f4f8", "#323b4b"} <= before
        app.apply_theme_choice("daymoon")
        await pilot.pause()
        after = _banner_colors(banner.render())
        assert "#a3b5ff" not in after and "#e3c58f" not in after


async def test_approval_modal_diff_uses_theme_colors():
    from textual.widgets import Static

    from luna.tui.approval_modal import ApprovalModal

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        modal = ApprovalModal(
            {
                "name": "edit_file",
                "args": {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
            }
        )
        app.push_screen(modal)
        await pilot.pause()
        body = modal.query(Static).first()
        rendered = body.render()
        spans = {str(s.style) for s in getattr(rendered, "spans", [])}
        # moonlight add on add_bg: #86d6a8 on #15281f
        assert "rgb(134,214,168) on rgb(21,40,31)" in spans, spans


async def test_failed_theme_switch_keeps_the_previous_choice():
    from textual.app import InvalidThemeError

    app = make_app(theme_choice="moonlight")
    async with app.run_test():
        app.unregister_theme("luna-daymoon")
        with pytest.raises(InvalidThemeError):
            app.apply_theme_choice("daymoon")
        assert app.theme_choice == "moonlight"
        assert app.theme == "luna-moonlight"
        assert themes.active().id == "moonlight"


async def test_session_title_and_pickers_are_literal_not_markup():
    from textual.widgets import Label

    from luna.tui.pickers import ChoiceModal, ConfirmModal
    from luna.tui.sidebar_sessions import _SessionRow

    title = "fix [/] and [$ok] ${arr[$i]}"
    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        row = _SessionRow(title, "[b]now[/b]")
        await app.query_one("#transcript").mount(row)
        await pilot.pause()
        assert [str(lbl.render()) for lbl in row.query(Label)] == [title, "[b]now[/b]"]
        app.push_screen(ChoiceModal(title, [("a", "[/] option [$x]")]))
        await pilot.pause()
        assert title in [str(w.render()) for w in app.screen.query(".picker-title")]
        assert "[/] option [$x]" in [str(lbl.render()) for lbl in app.screen.query(Label)]
        app.pop_screen()
        app.push_screen(ConfirmModal(title))
        await pilot.pause()
        assert title in [str(w.render()) for w in app.screen.query(".picker-title")]


@pytest.mark.parametrize("theme_id", ["moonlight", "daymoon", "moonlight-cb", "daymoon-cb"])
async def test_approval_buttons_use_theme_tokens(theme_id):
    from luna.tui.approval_modal import ApprovalModal

    tokens = themes.load_theme(theme_id).tokens
    app = make_app(theme_choice=theme_id)
    async with app.run_test(size=(130, 36)) as pilot:
        modal = ApprovalModal({"name": "execute", "args": {"command": "ls"}})
        app.push_screen(modal)
        await pilot.pause()
        focused = modal.query_one("#approve-button")
        resting = modal.query_one("#always-button")
        assert app.focused is focused
        assert focused.styles.background == Color.parse(tokens["primary"])
        assert focused.styles.color == Color.parse(tokens["on_primary"])
        assert focused.styles.text_style.bold
        assert not focused.styles.text_style.reverse
        assert resting.styles.background == Color.parse(tokens["panel"])
        assert resting.styles.color == Color.parse(tokens["text"])
        assert not resting.styles.border.top[0]  # no 3-row tall border
        ok_bar = modal.query_one("#approve-button").styles.border_left
        err_bar = modal.query_one("#reject-button").styles.border_left
        assert ok_bar[1] == Color.parse(tokens["ok"])
        assert err_bar[1] == Color.parse(tokens["err"])
