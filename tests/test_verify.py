import io
import sys

from rich.console import Console

from luna.config.config import LunaConfig
from luna.config.usage import TurnUsage
from luna.turn.verify import run_verify


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=True)


def test_disabled_command_is_ok(tmp_path):
    assert run_verify("", str(tmp_path)) == (True, "")


def test_passing_command(tmp_path):
    ok, tail = run_verify(f"{sys.executable} -c \"print('hi')\"", str(tmp_path))
    assert ok is True


def test_failing_command_tail(tmp_path):
    ok, tail = run_verify(
        f"{sys.executable} -c \"import sys; print('boom'); sys.exit(1)\"", str(tmp_path)
    )
    assert ok is False and "boom" in tail


def _flaky_command(sentinel) -> str:
    """A command that fails the first time and passes once ``sentinel`` exists."""
    return (
        f'{sys.executable} -c "import os,sys; '
        f"sys.exit(0) if os.path.exists(r'{sentinel}') "
        f"else (open(r'{sentinel}','w').close() or sys.exit(1))\""
    )


def test_run_verification_one_retry_then_ok(tmp_path, monkeypatch):
    from luna.core import session

    calls: list = []

    def fake_stream(*args, **kwargs):
        calls.append(args)
        return ("", False, TurnUsage(), set())

    monkeypatch.setattr(session, "_stream_turn", fake_stream)

    cmd = _flaky_command(tmp_path / "ok")
    cfg = LunaConfig(workdir=str(tmp_path), verify_command=cmd)
    console = _console()

    session._run_verification(
        object(), {"configurable": {"thread_id": "t"}}, console, cfg, lambda _: ""
    )

    assert len(calls) == 1  # exactly one fix-up turn
    assert "verify ok" in console.file.getvalue()


def test_run_verification_noop_when_disabled(tmp_path, monkeypatch):
    from luna.core import session

    calls: list = []
    monkeypatch.setattr(session, "_stream_turn", lambda *a, **k: calls.append(1))

    session._run_verification(
        object(), {}, _console(), LunaConfig(workdir=str(tmp_path)), lambda _: ""
    )

    assert calls == []


def test_run_verification_gives_up_after_one_retry(tmp_path, monkeypatch):
    from luna.core import session
    from luna.turn.verify import run_verify as real_run_verify

    verify_calls: list = []

    def counting(command, workdir):
        verify_calls.append(command)
        return real_run_verify(command, workdir)

    monkeypatch.setattr(session, "run_verify", counting)

    stream_calls: list = []
    monkeypatch.setattr(
        session,
        "_stream_turn",
        lambda *a, **k: stream_calls.append(a) or ("", False, TurnUsage(), set()),
    )

    cfg = LunaConfig(
        workdir=str(tmp_path),
        verify_command=f'{sys.executable} -c "import sys; sys.exit(1)"',
    )
    console = Console(file=io.StringIO(), width=200)

    session._run_verification(
        object(), {"configurable": {"thread_id": "t"}}, console, cfg, lambda _: ""
    )

    assert len(stream_calls) == 1  # exactly one fix-up turn, no second
    assert len(verify_calls) == 2  # initial check + one re-check, no third
    assert "still failing after 1 retry" in console.file.getvalue()


def test_verification_gated_on_mutating_tool(tmp_path, monkeypatch):
    from luna.core import session

    verify_calls: list = []
    monkeypatch.setattr(
        session,
        "run_verify",
        lambda *a, **k: verify_calls.append(a) or (True, ""),
    )

    def scripted(names):
        return lambda *a, **k: ("", False, TurnUsage(), names)

    cfg = LunaConfig(workdir=str(tmp_path), verify_command="x")

    monkeypatch.setattr(session, "_stream_turn", scripted({"write_file"}))
    session.run_once(
        object(),
        "go",
        thread_id="t",
        console=_console(),
        workdir=str(tmp_path),
        cfg=cfg,
    )
    assert len(verify_calls) == 1  # mutating turn -> verification ran

    verify_calls.clear()
    monkeypatch.setattr(session, "_stream_turn", scripted({"read_file"}))
    session.run_once(
        object(),
        "go",
        thread_id="t",
        console=_console(),
        workdir=str(tmp_path),
        cfg=cfg,
    )
    assert verify_calls == []  # read-only turn -> verification skipped
