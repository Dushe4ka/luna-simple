from luna.tui.status_bar import format_status_line


def test_format_status_line_includes_all_fields():
    line = format_status_line(
        model="sonnet-5", cost_usd=0.14, context_file="toolguard.py", plan_mode=False, undo_depth=3
    )
    assert "sonnet-5" in line
    assert "0.14" in line
    assert "toolguard.py" in line
    assert "plan: off" in line
    assert "undo: 3" in line


def test_format_status_line_shows_plan_on():
    line = format_status_line(
        model="sonnet-5", cost_usd=0.0, context_file=None, plan_mode=True, undo_depth=0
    )
    assert "plan: on" in line
