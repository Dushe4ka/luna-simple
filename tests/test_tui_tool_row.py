from textual.app import App, ComposeResult
from textual.widgets import Static

from luna.tui.tool_row import ToolRow
from luna.tui.widgets import PulseGlyph


class _Host(App):
    def __init__(self, row: ToolRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        yield self.row


def _text(row: ToolRow) -> str:
    return "\n".join(str(s.render()) for s in row.query(Static) if not isinstance(s, PulseGlyph))


async def test_running_row_pulses_then_shows_result_and_duration():
    row = ToolRow("web_search", "погода Орёл")
    async with _Host(row).run_test():
        assert len(row.query(PulseGlyph)) == 1
        await row.finish(True, "8 результатов")
        assert row.is_finished
        assert len(row.query(PulseGlyph)) == 0
        text = _text(row)
        assert "● web_search" in text and "«погода Орёл»" in text
        assert "└ 8 результатов · " in text and text.rstrip().endswith("s")


async def test_quiet_success_shows_only_duration_live():
    row = ToolRow("read_file", "/a.py")
    async with _Host(row).run_test():
        await row.finish(True, "")
        assert "└ " in _text(row)


async def test_replayed_row_has_no_duration_and_no_empty_detail_line():
    row = ToolRow("read_file", "/a.py", result=(True, ""))
    async with _Host(row).run_test():
        assert row.is_finished
        assert "└" not in _text(row)


async def test_replayed_failure_shows_detail():
    row = ToolRow("execute", "false", result=(False, "exit 1"))
    async with _Host(row).run_test():
        assert "└ exit 1" in _text(row)
