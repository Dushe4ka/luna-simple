import pytest

from luna.ui import console as console_mod
from luna.ui.themes import active, load_theme, rich_color, set_active


@pytest.fixture
def restore_theme():
    before = active()
    yield
    console_mod.use_theme(before)
    set_active(before)


def test_console_uses_active_theme_styles():
    con = console_mod.get_console(force_terminal=False)
    want = rich_color(active().tokens["primary"])
    assert con.get_style("luna.primary").color.name == want


def test_use_theme_swaps_console_theme(restore_theme):
    con = console_mod.get_console(force_terminal=False)
    day = load_theme("daymoon")
    console_mod.use_theme(day)
    assert active() is day
    assert con.get_style("luna.primary").color.name == day.tokens["primary"]
    depth = len(con._theme_stack._entries)
    console_mod.use_theme(load_theme("moonlight"))
    # the previous Luna theme is popped, not stacked forever
    assert len(con._theme_stack._entries) == depth
    assert con.get_style("luna.primary").color.name == load_theme("moonlight").tokens["primary"]
