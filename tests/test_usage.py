"""Tests for :mod:`luna.usage` — best-effort token accounting."""

from luna.usage import SessionUsage, TurnUsage, context_window, indicator_line


def test_turn_merges_metadata():
    t = TurnUsage()
    t.merge({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    t.merge({"input_tokens": 5, "output_tokens": 3, "total_tokens": 8})
    assert (t.input_tokens, t.output_tokens, t.total_tokens) == (105, 23, 128)


def test_turn_merge_none_is_safe():
    t = TurnUsage()
    t.merge(None)
    assert t.total_tokens == 0


def test_session_totals_and_last_prompt():
    s = SessionUsage()
    a = TurnUsage()
    a.merge({"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
    b = TurnUsage()
    b.merge({"input_tokens": 300, "output_tokens": 40, "total_tokens": 340})
    s.add_turn(a)
    s.add_turn(b)
    assert s.totals == (400, 50, 450)
    assert s.last_prompt_tokens == 300


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
