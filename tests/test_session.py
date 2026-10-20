import io
from pathlib import Path

from luna_core.providers.fake import FakeProvider
from luna_core.session import Session
from rich.console import Console

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.session import SLASH_COMMANDS, render_turn, run_once, run_repl
from luna.core.store import open_log


def _console():
    return Console(file=io.StringIO(), force_terminal=True)


def _cfg(tmp_path, **kw):
    return LunaConfig(workdir=str(tmp_path), model="m", format_command="", **kw)


async def test_render_turn_approves_and_rejects_in_order(tmp_path):
    """Two approvals in one turn: Enter approves, "n" + reason rejects."""
    from luna_core.messages import Message, ToolCall

    both = Message.assistant(
        [
            ToolCall("1", "write_file", {"file_path": "a.txt", "content": "x"}),
            ToolCall("2", "execute", {"command": "echo hi"}),
        ]
    )
    bundle = await build_agent(
        _cfg(tmp_path), provider=FakeProvider([both, FakeProvider.text("ok")])
    )
    session = Session.create(open_log(), str(Path(tmp_path).resolve()))
    answers = iter(["", "n", "nope"])
    summary = await render_turn(
        bundle.agent, session, "go", console=_console(), input_fn=lambda _: next(answers)
    )
    await bundle.aclose()
    assert (tmp_path / "a.txt").read_text() == "x"
    results = session.messages()[2].results
    assert not results[0].is_error
    assert results[1].is_error and "rejected by the user: nope" in results[1].text
    assert summary.text == "ok" and summary.tools == {"write_file", "execute"}


async def test_run_once_returns_final_text(tmp_path):
    cfg = _cfg(tmp_path)
    bundle = await build_agent(cfg, provider=FakeProvider([FakeProvider.text("done")]))
    assert await run_once(bundle, "hi", console=_console(), log=open_log(), cfg=cfg) == "done"
    await bundle.aclose()


async def test_run_once_continues_a_given_session(tmp_path):
    cfg = _cfg(tmp_path)
    log = open_log()
    bundle = await build_agent(
        cfg, provider=FakeProvider([FakeProvider.text("one"), FakeProvider.text("two")])
    )
    session = Session.create(log, str(Path(tmp_path).resolve()))
    await run_once(bundle, "first", console=_console(), log=log, session=session, cfg=cfg)
    await run_once(bundle, "second", console=_console(), log=log, session=session, cfg=cfg)
    assert [m.text for m in session.messages()] == ["first", "one", "second", "two"]
    await bundle.aclose()


def test_slash_help_registered():
    assert "/help" in SLASH_COMMANDS
    assert "/exit" in SLASH_COMMANDS
    assert "/tools" in SLASH_COMMANDS


async def test_at_agent_mention_is_rewritten_to_a_delegation_instruction(tmp_path):
    cfg = _cfg(tmp_path, yolo=True)
    bundle = await build_agent(cfg, provider=FakeProvider([FakeProvider.text("ok")]))
    log = open_log()
    session = Session.create(log, str(Path(tmp_path).resolve()))
    lines = iter(["@researcher find the entry point", "/exit"])
    await run_repl(
        bundle,
        console=_console(),
        input_fn=lambda _: next(lines),
        log=log,
        session=session,
        config=cfg,
    )
    await bundle.aclose()
    (seen,) = [m.text for m in session.messages() if m.role == "user"]
    assert "researcher" in seen and "task tool" in seen and "find the entry point" in seen
    assert session.title == "@researcher find the entry point"  # the line the user typed


async def test_render_turn_pauses_and_resumes_progress_around_an_approval(tmp_path, monkeypatch):
    from luna.core import session as session_mod

    events: list[str] = []

    class _SpyProgress:
        def __init__(self, console):
            events.append("created")

        def start(self, *a, **k):
            events.append("start")

        def finish(self, *a, **k):
            events.append("finish")

        def pause(self):
            events.append("pause")

        def resume(self):
            events.append("resume")

        def close(self):
            events.append("close")

    monkeypatch.setattr(session_mod, "ToolProgress", _SpyProgress)
    script = [
        FakeProvider.call("write_file", {"file_path": "a", "content": ""}),
        FakeProvider.text("done"),
    ]
    bundle = await build_agent(_cfg(tmp_path), provider=FakeProvider(script))
    session = Session.create(open_log(), str(Path(tmp_path).resolve()))
    await render_turn(bundle.agent, session, "hi", console=_console(), input_fn=lambda _: "")
    await bundle.aclose()
    assert events == ["created", "pause", "resume", "start", "finish", "close"]
