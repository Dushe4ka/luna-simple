import io

from rich.console import Console

from luna.ui.turn import close_turn, open_turn


def _c():
    return Console(file=io.StringIO(), force_terminal=True, no_color=True, width=80)


def test_open_and_close_emit_markers():
    c = _c()
    open_turn(c)
    close_turn(c)
    out = c.file.getvalue()
    assert "luna" in out
