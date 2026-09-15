import io

from rich.console import Console

import luna.ui.interact as interact
from luna.ui.interact import arrow_confirm, arrow_pick


def _console(is_terminal=True):
    return Console(file=io.StringIO(), force_terminal=is_terminal)


def test_arrow_pick_returns_none_when_input_fn_is_not_the_real_builtin():
    # every existing test in this project overrides input_fn exactly this
    # way (a real input() would block forever with no real stdin attached)
    # — this must never touch questionary at all when that's true.
    result = arrow_pick(_console(), lambda _: "x", [("a", "A"), ("b", "B")])
    assert result is None


def test_arrow_confirm_returns_none_when_input_fn_is_not_the_real_builtin():
    result = arrow_confirm(_console(), lambda _: "x", "sure?")
    assert result is None


def test_arrow_pick_returns_none_when_console_is_not_a_terminal():
    result = arrow_pick(_console(is_terminal=False), input, [("a", "A")])
    assert result is None


def test_arrow_confirm_returns_none_when_console_is_not_a_terminal():
    result = arrow_confirm(_console(is_terminal=False), input, "sure?")
    assert result is None


def test_arrow_pick_returns_the_selected_value(monkeypatch):
    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)

    class _FakeQuestion:
        def ask(self):
            return "b"

    monkeypatch.setattr("questionary.select", lambda *a, **k: _FakeQuestion())
    result = arrow_pick(_console(), input, [("a", "A"), ("b", "B")], default="a")
    assert result == "b"


def test_arrow_pick_passes_the_default_value_not_its_label(monkeypatch):
    """questionary.select's own `default` matches a Choice's `value`,
    never its display label (verified directly against the real,
    unmocked library in test_arrow_pick_default_is_valid_against_real_questionary
    below) — a mock that captures the wrong one would pass regardless,
    which is exactly how this bug shipped undetected the first time."""
    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)
    captured = {}

    class _FakeQuestion:
        def ask(self):
            return "a"

    def _fake_select(message, choices, default=None):
        captured["default"] = default
        return _FakeQuestion()

    monkeypatch.setattr("questionary.select", _fake_select)
    arrow_pick(_console(), input, [("a", "Alpha"), ("b", "Beta")], default="b")
    assert captured["default"] == "b"


def test_arrow_pick_default_is_valid_against_real_questionary():
    """Regression test for a real crash: questionary.select's own
    InquirerControl raises ValueError when `default` doesn't match a
    Choice's `value` (or the Choice/raw-string itself) — it never
    matches against the display label. A fully mocked questionary.select
    (as every other test in this file uses) cannot catch this class of
    bug, since the mock never runs that validation. This constructs
    exactly what arrow_pick builds internally and calls the real,
    unmocked questionary.select with it."""
    import questionary

    options = [("a", "Alpha  (A_KEY)"), ("b", "Beta  (B_KEY)")]
    for value, _ in options:
        questionary.select(
            "",
            choices=[questionary.Choice(label, value=v) for v, label in options],
            default=value,
        )


def test_arrow_pick_raises_keyboard_interrupt_on_cancel(monkeypatch):
    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)

    class _FakeQuestion:
        def ask(self):
            return None

    monkeypatch.setattr("questionary.select", lambda *a, **k: _FakeQuestion())
    try:
        arrow_pick(_console(), input, [("a", "A")])
        raise AssertionError("expected KeyboardInterrupt")
    except KeyboardInterrupt:
        pass


def test_arrow_confirm_returns_the_selected_value(monkeypatch):
    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)

    class _FakeQuestion:
        def ask(self):
            return True

    monkeypatch.setattr("questionary.confirm", lambda *a, **k: _FakeQuestion())
    result = arrow_confirm(_console(), input, "sure?")
    assert result is True


def test_arrow_confirm_raises_keyboard_interrupt_on_cancel(monkeypatch):
    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)

    class _FakeQuestion:
        def ask(self):
            return None

    monkeypatch.setattr("questionary.confirm", lambda *a, **k: _FakeQuestion())
    try:
        arrow_confirm(_console(), input, "sure?")
        raise AssertionError("expected KeyboardInterrupt")
    except KeyboardInterrupt:
        pass
