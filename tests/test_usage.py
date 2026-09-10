"""Tests for :mod:`luna.usage` — best-effort token accounting."""

from luna.usage import SessionUsage, TurnUsage, context_window, indicator_line


def test_turn_merges_metadata():
    t = TurnUsage()
    t.merge({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    t.merge({"input_tokens": 300, "output_tokens": 40, "total_tokens": 340})
    # input_tokens is the *last* prompt size (assign), output/total accumulate.
    assert (t.input_tokens, t.output_tokens, t.total_tokens) == (300, 60, 460)


def test_turn_merge_none_is_safe():
    t = TurnUsage()
    t.merge(None)
    assert t.total_tokens == 0


def test_session_totals_and_last_prompt():
    s = SessionUsage()
    a = TurnUsage()
    a.merge({"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
    b = TurnUsage()
    b.merge({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    b.merge({"input_tokens": 300, "output_tokens": 40, "total_tokens": 340})
    s.add_turn(a)
    s.add_turn(b)
    # input column of totals sums each turn's (last) prompt size: 100 + 300
    assert s.totals == (400, 70, 570)
    assert s.last_prompt_tokens == 300  # last turn's current prompt size


def test_context_window_lookup_and_fallback():
    assert context_window("anthropic", "claude-sonnet-4-5") >= 200_000
    assert context_window("ollama", "who-knows") == 200_000


def test_indicator_line_mentions_ctx_and_session():
    s = SessionUsage()
    t = TurnUsage()
    t.merge({"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200})
    s.add_turn(t)
    line = indicator_line(s, "anthropic", "claude-sonnet-4-5")
    assert "ctx" in line and "session" in line


def test_registry_window_and_price():
    from luna.usage import context_window, price

    assert context_window("anthropic", "claude-sonnet-4-5") == 200_000
    p = price("anthropic", "claude-sonnet-4-5")
    assert p is not None and p[0] > 0 and p[1] > 0


def test_registry_overrides_win_over_the_packaged_file():
    from luna.usage import context_window, price

    overrides = {"my-model": {"window": 128_000, "input": 2.0, "output": 6.0}}
    assert context_window("x", "my-model-v1", overrides) == 128_000
    assert price("x", "my-model-v1", overrides) == (2.0, 6.0)


def test_unknown_model_has_no_price():
    from luna.usage import price

    assert price("x", "totally-unknown-model-id") is None


def test_session_cost_uses_totals():
    from luna.usage import SessionUsage, TurnUsage

    s = SessionUsage()
    t = TurnUsage()
    t.merge({"input_tokens": 1_000_000, "output_tokens": 1_000_000, "total_tokens": 2_000_000})
    s.add_turn(t)
    cost = s.cost("anthropic", "claude-sonnet-4-5")
    assert cost is not None and cost > 0


def test_indicator_line_shows_cost_when_known():
    from luna.usage import SessionUsage, TurnUsage, indicator_line

    s = SessionUsage()
    t = TurnUsage()
    t.merge({"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200})
    s.add_turn(t)
    line = indicator_line(s, "anthropic", "claude-sonnet-4-5")
    assert "$" in line
