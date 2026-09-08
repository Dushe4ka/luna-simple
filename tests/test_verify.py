import io

from rich.console import Console

from luna.config import LunaConfig
from luna.usage import TurnUsage
from luna.verify import run_verify


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=True)


def test_disabled_command_is_ok(tmp_path):
    assert run_verify("", str(tmp_path)) == (True, "")


def test_passing_command(tmp_path):
    ok, tail = run_verify("python -c \"print('hi')\"", str(tmp_path))
    assert ok is True


def test_failing_command_tail(tmp_path):
    ok, tail = run_verify("python -c \"import sys; print('boom'); sys.exit(1)\"", str(tmp_path))
    assert ok is False and "boom" in tail


def _flaky_command(sentinel) -> str:
    """A command that fails the first time and passes once ``sentinel`` exists."""
    return (
        'python -c "import os,sys; '
        f"sys.exit(0) if os.path.exists(r'{sentinel}') "
        f"else (open(r'{sentinel}','w').close() or sys.exit(1))\""
    )


def test_run_verification_one_retry_then_ok(tmp_path, monkeypatch):
    from luna import session

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
    from luna import session

    calls: list = []
    monkeypatch.setattr(session, "_stream_turn", lambda *a, **k: calls.append(1))

    session._run_verification(
        object(), {}, _console(), LunaConfig(workdir=str(tmp_path)), lambda _: ""
    )

    assert calls == []
