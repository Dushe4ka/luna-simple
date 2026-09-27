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


def test_format_status_line_shows_provider_alongside_model():
    line = format_status_line(
        model="sonnet-5",
        provider="anthropic",
        cost_usd=0.0,
        context_file=None,
        plan_mode=False,
        undo_depth=0,
    )
    assert "sonnet-5 (anthropic)" in line


def test_format_status_line_prefers_usage_summary_over_bare_cost():
    """Regression: StatusBar existed but nothing ever fed it real data, so
    it only ever showed a static "$0.00" — `usage_summary` (the old REPL's
    "ctx ~X/Y · ..." indicator line, computed once a turn completes) is
    what actually made the model/context/cost info visible again.
    """
    line = format_status_line(
        model="sonnet-5",
        provider="anthropic",
        cost_usd=0.0,
        context_file=None,
        plan_mode=False,
        undo_depth=0,
        usage_summary="ctx ~1.2k/200.0k · turn 100 in / 20 out · session 220",
    )
    assert "ctx ~1.2k/200.0k" in line
    assert "$0.00" not in line
