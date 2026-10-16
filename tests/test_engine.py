import subprocess

from luna_core.events import Notice, TurnDone
from luna_core.loop import Agent
from luna_core.policy import Policy
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session
from luna_core.workspace import Workspace

from luna.config.config import LunaConfig
from luna.core.session_state import SessionState
from luna.turn import engine


def git(root, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_prepare_turn_builds_text_and_images(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    state = SessionState("s", str(tmp_path))
    state.pending_diagnostics = "E1 bad"
    state.pinned.add("a.py")
    prepared = engine.prepare_turn(
        state,
        "@researcher look at @a.py",
        workdir=str(tmp_path),
        subagent_names={"researcher"},
    )
    assert prepared.text.startswith("<diagnostics>\nE1 bad") and "<attached: a.py>" in prepared.text
    assert "Delegate this to the 'researcher' subagent" in prepared.text
    assert state.pending_diagnostics == ""
    assert prepared.images == [] and prepared.title_line.startswith("@researcher")


def test_prepare_turn_turns_an_image_mention_into_an_image_block(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    state = SessionState("s", str(tmp_path))
    prepared = engine.prepare_turn(
        state, "what is in @shot.png", workdir=str(tmp_path), subagent_names=set()
    )
    assert len(prepared.images) == 1 and "<image: shot.png>" in prepared.text


async def test_verify_failure_gets_exactly_one_fixup_turn(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "ok.flag").write_text("")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")
    cfg = LunaConfig(
        workdir=str(tmp_path),
        verify_command="test -f fixed.flag",
        format_command="",
        diagnose_command="",
    )
    state = SessionState("s", str(tmp_path))
    hooks = engine.make_hooks(lambda: state, lambda: cfg)
    script = [
        FakeProvider.call("write_file", {"file_path": "x.txt", "content": "1"}),
        FakeProvider.text("done"),
        FakeProvider.call("write_file", {"file_path": "fixed.flag", "content": ""}),
        FakeProvider.text("fixed"),
    ]
    ws = Workspace(tmp_path)
    agent = Agent(
        FakeProvider(script), "m", workspace=ws, policy=Policy(workspace=ws, yolo=True), hooks=hooks
    )
    log = EventLog(tmp_path / ".luna" / "s.db")
    session = Session.create(log, str(tmp_path), undo=False)
    engine.before_turn(state)
    events = [e async for e in agent.run(session, "make x")]
    texts = [m.text for m in session.messages() if m.role == "user"]
    assert sum("`test -f fixed.flag` failed" in t for t in texts) == 1
    assert any(isinstance(e, Notice) and "verify ok" in e.text for e in events)
    assert isinstance(events[-1], TurnDone)


async def test_verify_still_failing_is_a_notice_not_a_loop(tmp_path):
    cfg = LunaConfig(
        workdir=str(tmp_path), verify_command="false", format_command="", diagnose_command=""
    )
    state = SessionState("s", str(tmp_path))
    script = [
        FakeProvider.call("write_file", {"file_path": "x.txt", "content": "1"}),
        FakeProvider.text("done"),
        FakeProvider.text("tried"),
    ]
    ws = Workspace(tmp_path)
    agent = Agent(
        FakeProvider(script),
        "m",
        workspace=ws,
        policy=Policy(workspace=ws, yolo=True),
        hooks=engine.make_hooks(lambda: state, lambda: cfg),
    )
    log = EventLog(tmp_path / "s.db")
    engine.before_turn(state)
    session = Session.create(log, str(tmp_path), undo=False)
    events = [e async for e in agent.run(session, "go")]
    assert any(isinstance(e, Notice) and "still failing" in e.text for e in events)
    assert isinstance(events[-1], TurnDone)
    fixups = [m for m in session.messages() if m.role == "user" and "Fix it." in m.text]
    assert len(fixups) == 1


async def test_read_only_turn_skips_the_pipeline(tmp_path):
    cfg = LunaConfig(workdir=str(tmp_path), verify_command="false")
    state = SessionState("s", str(tmp_path))
    ws = Workspace(tmp_path)
    agent = Agent(
        FakeProvider([FakeProvider.call("ls", {}), FakeProvider.text("ok")]),
        "m",
        workspace=ws,
        hooks=engine.make_hooks(lambda: state, lambda: cfg),
    )
    log = EventLog(tmp_path / "s.db")
    engine.before_turn(state)
    events = [e async for e in agent.run(Session.create(log, str(tmp_path), undo=False), "look")]
    assert not any(isinstance(e, Notice) and "verify" in e.text for e in events)


async def test_mutating_turn_formats_and_carries_diagnostics(tmp_path):
    cfg = LunaConfig(
        workdir=str(tmp_path),
        verify_command="",
        format_command="",
        diagnose_command="echo a.py:1: fake finding",
    )
    state = SessionState("s", str(tmp_path))
    ws = Workspace(tmp_path)
    agent = Agent(
        FakeProvider(
            [
                FakeProvider.call("write_file", {"file_path": "a.py", "content": "x = 1\n"}),
                FakeProvider.text("wrote"),
            ]
        ),
        "m",
        workspace=ws,
        policy=Policy(workspace=ws, yolo=True),
        hooks=engine.make_hooks(lambda: state, lambda: cfg),
    )
    engine.before_turn(state)
    session = Session.create(EventLog(tmp_path / "s.db"), str(tmp_path), undo=False)
    events = [e async for e in agent.run(session, "write a.py")]
    assert any(isinstance(e, Notice) and "fake finding" in e.text for e in events)
    assert "fake finding" in state.pending_diagnostics


async def test_a_missing_state_disables_the_pipeline(tmp_path):
    cfg = LunaConfig(workdir=str(tmp_path), verify_command="false")
    ws = Workspace(tmp_path)
    agent = Agent(
        FakeProvider(
            [
                FakeProvider.call("write_file", {"file_path": "a", "content": ""}),
                FakeProvider.text("k"),
            ]
        ),
        "m",
        workspace=ws,
        policy=Policy(workspace=ws, yolo=True),
        hooks=engine.make_hooks(lambda: None, lambda: cfg),
    )
    session = Session.create(EventLog(tmp_path / "s.db"), str(tmp_path), undo=False)
    events = [e async for e in agent.run(session, "go")]
    assert not any(isinstance(e, Notice) for e in events) and isinstance(events[-1], TurnDone)


def test_before_turn_ignores_lunas_own_state(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "j.json").write_text("{}")
    state = SessionState("s", str(tmp_path))
    state.mutated, state.fixup_turn = True, "t"
    engine.before_turn(state)
    assert state.dirty_before == ["a.txt"]
    assert state.mutated is False and state.fixup_turn is None


def test_notice_level():
    assert engine.notice_level("✓ verify ok") == "ok"
    assert engine.notice_level("⚠ verify still failing after 1 retry\nx") == "warn"
    assert engine.notice_level("verify failed\nboom") == "warn"
    assert engine.notice_level("⌁ formatted 2 file(s)") == "dim"
    assert engine.notice_level("a.py:1: E1\na.py:2: E2") == "dim"
    assert engine.notice_level("hook says hi") == "info"
