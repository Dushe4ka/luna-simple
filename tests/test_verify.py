import io
import sys

from luna_core.providers.fake import FakeProvider
from rich.console import Console

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.session import run_once
from luna.core.store import open_log
from luna.turn import engine
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


_WRITE = FakeProvider.call("write_file", {"file_path": "a.txt", "content": "x"})


async def _once(tmp_path, cfg, script):
    """``luna -p`` with the engine hooks attached: (printed output, session messages)."""
    hooks_state: dict = {}
    bundle = await build_agent(
        cfg,
        provider=FakeProvider(script),
        hooks=engine.make_hooks(lambda: hooks_state.get("state"), lambda: cfg),
    )
    console = Console(file=io.StringIO(), width=200)
    log = open_log()
    await run_once(bundle, "go", console=console, log=log, cfg=cfg, hooks_state=hooks_state)
    await bundle.aclose()
    info = log.list()[0]  # the newest session: this run's
    from luna_core.session import Session

    messages = Session.open(log, info.id, undo=False).messages()
    return console.file.getvalue(), [m.text for m in messages if m.role == "user"]


def _cfg(tmp_path, verify):
    return LunaConfig(
        workdir=str(tmp_path),
        model="m",
        yolo=True,
        format_command="",
        diagnose_command="",
        verify_command=verify,
    )


async def test_run_verification_one_retry_then_ok(tmp_path):
    cfg = _cfg(tmp_path, _flaky_command(tmp_path / "ok"))
    out, users = await _once(
        tmp_path, cfg, [_WRITE, FakeProvider.text("done"), FakeProvider.text("fixed")]
    )
    assert sum("Fix it." in u for u in users) == 1  # exactly one fix-up
    assert "verify ok" in out


async def test_run_verification_noop_when_disabled(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(engine, "run_verify", lambda *a, **_: calls.append(a) or (True, ""))
    out, users = await _once(tmp_path, _cfg(tmp_path, ""), [_WRITE, FakeProvider.text("done")])
    assert calls == [] and users == ["go"] and "verify" not in out


async def test_run_verification_gives_up_after_one_retry(tmp_path, monkeypatch):
    from luna.turn.verify import run_verify as real_run_verify

    verify_calls: list = []
    monkeypatch.setattr(
        engine, "run_verify", lambda c, w, **k: verify_calls.append(c) or real_run_verify(c, w, **k)
    )
    cfg = _cfg(tmp_path, f'{sys.executable} -c "import sys; sys.exit(1)"')
    out, users = await _once(
        tmp_path, cfg, [_WRITE, FakeProvider.text("done"), FakeProvider.text("tried")]
    )
    assert sum("Fix it." in u for u in users) == 1  # exactly one fix-up, no second
    assert len(verify_calls) == 2  # initial check + one re-check, no third
    assert "still failing after 1 retry" in out


async def test_verification_gated_on_mutating_tool(tmp_path, monkeypatch):
    verify_calls: list = []
    monkeypatch.setattr(engine, "run_verify", lambda *a, **_: verify_calls.append(a) or (True, ""))
    cfg = _cfg(tmp_path, "x")
    await _once(tmp_path, cfg, [_WRITE, FakeProvider.text("done")])
    assert len(verify_calls) == 1  # mutating turn -> verification ran

    verify_calls.clear()
    await _once(tmp_path, cfg, [FakeProvider.call("ls", {}), FakeProvider.text("done")])
    assert verify_calls == []  # read-only turn -> verification skipped
