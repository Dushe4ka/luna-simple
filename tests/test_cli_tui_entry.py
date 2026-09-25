"""``luna`` with no one-shot prompt in an interactive session launches the TUI."""

import pytest


def test_main_launches_tui_when_interactive_and_no_prompt(monkeypatch, tmp_path):
    import luna.cli as cli_mod

    launched = {}

    def fake_run_tui(config, *, workdir, thread_id):
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


def _force_interactive(monkeypatch, cli_mod):
    monkeypatch.setattr(cli_mod, "_has_api_key", lambda *a, **k: True)
    monkeypatch.setattr(cli_mod, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_mod, "run_repl", lambda *a, **k: pytest.fail("run_repl should not be called")
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("luna.ui.console.Console.is_terminal", property(lambda self: True))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")


def test_run_tui_receives_the_freshly_generated_start_thread(monkeypatch, tmp_path):
    """Regression (C1+C6): ``main()`` already resolves ``start_thread`` — a
    fresh uuid4 hex for a new session — but used to drop it on the floor
    when dispatching to the TUI, so the TUI opened on no thread at all.
    """
    import luna.cli as cli_mod

    seen = {}

    def fake_run_tui(config, *, workdir, thread_id):
        seen["workdir"] = workdir
        seen["thread_id"] = thread_id
        return 0

    monkeypatch.setattr(cli_mod, "run_tui", fake_run_tui)
    _force_interactive(monkeypatch, cli_mod)

    assert cli_mod.main(["--no-splash", "--workdir", str(tmp_path)]) == 0
    assert seen["workdir"] == str(tmp_path)
    # a real uuid4().hex, never None and never the string "None"
    assert isinstance(seen["thread_id"], str)
    assert len(seen["thread_id"]) == 32
    int(seen["thread_id"], 16)


def test_run_tui_receives_the_thread_resolved_by_resume(monkeypatch, tmp_path):
    """Regression (C1+C6): ``--resume <thread-id>`` must reach the TUI."""
    import luna.cli as cli_mod

    seen = {}

    def fake_run_tui(config, *, workdir, thread_id):
        seen["thread_id"] = thread_id
        return 0

    monkeypatch.setattr(cli_mod, "run_tui", fake_run_tui)
    _force_interactive(monkeypatch, cli_mod)

    code = cli_mod.main(
        ["--no-splash", "--workdir", str(tmp_path), "--resume", "deadbeefcafe"],
    )

    assert code == 0
    assert seen["thread_id"] == "deadbeefcafe"


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
