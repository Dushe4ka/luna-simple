"""Tests for ``luna.cli._resolve_resume`` — ``-c`` / ``--resume`` handling."""

import argparse
import io

from rich.console import Console

from luna.cli import _resolve_resume
from luna.core.persistence import SessionIndex


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=True)


def _args(cont: bool = False, resume: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(cont=cont, resume=resume)


def test_continue_returns_recorded_thread(tmp_path):
    idx = SessionIndex()
    idx.record("thread-a", str(tmp_path), "a task")
    got = _resolve_resume(_args(cont=True), idx, str(tmp_path), _console(), interactive=False)
    assert got == "thread-a"


def test_resume_numeric_is_a_list_index(tmp_path):
    idx = SessionIndex()
    idx.record("thread-old", str(tmp_path), "older")
    idx.record("thread-new", str(tmp_path), "newer")
    got = _resolve_resume(_args(resume="1"), idx, str(tmp_path), _console(), interactive=False)
    assert got == "thread-new"  # newest first → [1]


def test_resume_non_digit_is_treated_as_raw_thread_id(tmp_path):
    idx = SessionIndex()
    got = _resolve_resume(_args(resume="nope"), idx, str(tmp_path), _console(), interactive=False)
    assert got == "nope"


def test_resume_list_non_interactive_returns_none(tmp_path):
    idx = SessionIndex()
    idx.record("thread-a", str(tmp_path), "a task")
    got = _resolve_resume(
        _args(resume="__list__"), idx, str(tmp_path), _console(), interactive=False
    )
    assert got is None


def test_session_id_follows_resumed_thread(tmp_path, monkeypatch):
    import luna.cli as cli
    from luna.core.persistence import SessionIndex

    idx = SessionIndex()
    idx.record("thread-abc", str(tmp_path), "earlier work")

    seen: dict = {}

    def _capture(agent, **kw):
        seen.update(kw)
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(cli, "run_repl", _capture)
    monkeypatch.setattr(cli, "build_agent", lambda *a, **k: object())

    cli.main(["-c"])
    assert seen["thread_id"] == "thread-abc"
    assert seen["session_id"] == "thread-abc"


def test_resume_list_interactive_uses_arrow_pick_when_available(tmp_path, monkeypatch):
    """The interactive `--resume` (no id) branch has a real bug today: it
    calls the bare `input()` builtin directly (not an injectable
    input_fn), so this is the only way to exercise it in a test — patch
    the real builtin. Confirms arrow_pick is consulted first, and that a
    stubbed arrow_pick's return value is used directly."""
    import luna.cli as cli
    from luna.core.persistence import SessionIndex

    idx = SessionIndex()
    idx.record("thread-a", str(tmp_path), "a task")

    monkeypatch.setattr(
        cli, "arrow_pick", lambda console, input_fn, options, default=None: "thread-a"
    )
    got = _resolve_resume(
        _args(resume="__list__"), idx, str(tmp_path), _console(), interactive=True
    )
    assert got == "thread-a"


def test_resume_list_interactive_falls_back_to_input_when_arrow_pick_declines(
    tmp_path, monkeypatch
):
    import luna.cli as cli
    from luna.core.persistence import SessionIndex

    idx = SessionIndex()
    idx.record("thread-a", str(tmp_path), "a task")

    monkeypatch.setattr(cli, "arrow_pick", lambda console, input_fn, options, default=None: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    got = _resolve_resume(
        _args(resume="__list__"), idx, str(tmp_path), _console(), interactive=True
    )
    assert got == "thread-a"


def test_main_handles_keyboard_interrupt_during_resume_list(tmp_path, monkeypatch):
    """Regression test: Ctrl-C during `luna --resume` (interactive list) must return 130,
    not propagate as raw KeyboardInterrupt traceback. The try/except around
    _resolve_resume in main() should catch this."""
    import sys

    import luna.cli as cli
    from luna.core.persistence import SessionIndex
    from luna.ui.console import get_console

    idx = SessionIndex()
    idx.record("thread-a", str(tmp_path), "a task")

    # Make arrow_pick raise KeyboardInterrupt to simulate Ctrl-C
    def _raise_keyboard_interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "arrow_pick", _raise_keyboard_interrupt)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    # Ensure interactive detection works: console must be terminal, stdin must be tty
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "get_console", lambda: get_console(force_terminal=True))
    # Dummy agent so we don't hit build errors
    monkeypatch.setattr(cli, "build_agent", lambda *a, **k: object())

    # Call main() with --resume (no id) to trigger interactive list branch
    result = cli.main(["--resume"])
    assert result == 130
