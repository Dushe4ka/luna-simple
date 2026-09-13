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


def test_arrow_pick_passes_the_default_labeled_choice(monkeypatch):
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
    assert captured["default"] == "Beta"


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
