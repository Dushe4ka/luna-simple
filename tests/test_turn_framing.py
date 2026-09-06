import io

from rich.console import Console

from luna.ui.turn import close_turn, open_turn, tool_line


def _c():
    return Console(file=io.StringIO(), force_terminal=True, no_color=True, width=80)


def test_open_and_close_emit_markers():
    c = _c()
    open_turn(c)
    tool_line(c, "read_file", "pyproject.toml")
    close_turn(c)
    out = c.file.getvalue()
    assert "luna" in out
    assert "read_file" in out and "pyproject.toml" in out
    assert "⚙" in out
