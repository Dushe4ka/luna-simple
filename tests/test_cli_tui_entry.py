"""``luna`` with no one-shot prompt in an interactive session launches the TUI."""

import pytest


def test_main_launches_tui_when_interactive_and_no_prompt(monkeypatch, tmp_path):
    import luna.cli as cli_mod

    launched = {}

    def fake_run_tui(config, **kwargs):
        launched["called"] = True
        return 0

    def fail_run_repl(*a, **k):
        pytest.fail("run_repl should not be called when interactive")

    monkeypatch.setattr(cli_mod, "run_tui", fake_run_tui)
    monkeypatch.setattr(cli_mod, "_has_api_key", lambda *a, **k: True)
    monkeypatch.setattr(cli_mod, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(cli_mod, "run_repl", fail_run_repl)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    # Console.is_terminal is read from the real console; force it via the
    # same pattern tests/test_cli.py already uses (see
    # test_missing_key_interactive_runs_wizard) to simulate an interactive
    # terminal for main()'s branching.
    monkeypatch.setattr("luna.ui.console.Console.is_terminal", property(lambda self: True))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    code = cli_mod.main(["--no-splash", "--workdir", str(tmp_path)])

    assert code == 0
    assert launched == {"called": True}


def test_main_still_dispatches_repl_when_not_interactive(monkeypatch, tmp_path):
    """Non-interactive sessions (piped stdin, --no-input, etc.) keep using
    the old REPL as a fallback path — run_tui must not be reachable there."""
    import luna.cli as cli_mod

    calls = {}

    def fail_run_tui(*a, **k):
        pytest.fail("run_tui should not be called when not interactive")

    monkeypatch.setattr(cli_mod, "run_tui", fail_run_tui)
    monkeypatch.setattr(cli_mod, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(cli_mod, "run_repl", lambda *a, **k: calls.setdefault("repl", 0))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    code = cli_mod.main(["--no-splash", "--no-input", "--workdir", str(tmp_path)])

    assert code == 0
    assert calls == {"repl": 0}
