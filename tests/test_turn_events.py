from langchain_core.messages import AIMessage

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.turn_events import (
    Interrupted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    iter_turn,
)


def test_iter_turn_yields_text_deltas_for_a_plain_answer(tmp_path, fake_model):
    calls = [AIMessage(content="hello there")]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    config = {"configurable": {"thread_id": "t"}}
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    events = list(iter_turn(agent, payload, config))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "hello there"


def test_iter_turn_yields_tool_started_and_finished(tmp_path, fake_model):
    (tmp_path / "a.py").write_text("x = 1\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "id": "1", "args": {"file_path": "/a.py"}}],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    config = {"configurable": {"thread_id": "t"}}
    payload = {"messages": [{"role": "user", "content": "read it"}]}
    events = list(iter_turn(agent, payload, config))
    started = [e for e in events if isinstance(e, ToolStarted)]
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert len(started) == 1
    assert started[0].name == "read_file"
    assert len(finished) == 1
    assert finished[0].name == "read_file"
    assert finished[0].ok is True


def test_iter_turn_yields_interrupted_and_stops(tmp_path, fake_model):
    """A mutating call under approval (yolo=False) surfaces as Interrupted,
    and iter_turn does not loop back in on its own — no further events
    after it in this same call."""
    (tmp_path / "a.py").write_text("original\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "id": "1",
                    "args": {
                        "file_path": "/a.py",
                        "old_string": "original",
                        "new_string": "changed",
                    },
                }
            ],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=False)
    agent = build_agent(cfg, model=fake_model(*calls))
    config = {"configurable": {"thread_id": "t"}}
    payload = {"messages": [{"role": "user", "content": "edit it"}]}
    events = list(iter_turn(agent, payload, config))
    assert isinstance(events[-1], Interrupted)
    assert "action_requests" in events[-1].value or "action_request" in events[-1].value
    # the edit must NOT have happened yet — nothing resumed the interrupt
    assert (tmp_path / "a.py").read_text() == "original\n"


def test_iter_turn_resumes_after_interrupted(tmp_path, fake_model):
    """Calling iter_turn again with Command(resume=...) continues the same turn."""
    from langgraph.types import Command

    (tmp_path / "a.py").write_text("original\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "id": "1",
                    "args": {
                        "file_path": "/a.py",
                        "old_string": "original",
                        "new_string": "changed",
                    },
                }
            ],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=False)
    agent = build_agent(cfg, model=fake_model(*calls))
    config = {"configurable": {"thread_id": "t"}}
    payload = {"messages": [{"role": "user", "content": "edit it"}]}
    list(iter_turn(agent, payload, config))  # first pass: stops at Interrupted
    resume_events = list(
        iter_turn(agent, Command(resume={"decisions": [{"type": "approve"}]}), config)
    )
    assert (tmp_path / "a.py").read_text() == "changed\n"
    assert any(isinstance(e, ToolFinished) and e.name == "edit_file" for e in resume_events)
