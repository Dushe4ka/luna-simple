from langchain_core.messages import AIMessage, ToolMessage

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.turn_events import (
    Interrupted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    args_preview,
    iter_turn,
    tool_outcome,
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


def test_iter_turn_does_not_repeat_tool_started_across_a_resume(tmp_path, fake_model):
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
    first_pass = list(iter_turn(agent, payload, config))
    from langgraph.types import Command

    resume_pass = list(
        iter_turn(agent, Command(resume={"decisions": [{"type": "approve"}]}), config)
    )
    all_events = first_pass + resume_pass
    started_ids = [e.call_id for e in all_events if isinstance(e, ToolStarted)]
    assert started_ids == ["1"]  # exactly once, not twice


def test_tool_outcome_first_line_of_body():
    msg = ToolMessage(content="8 results\nmore", tool_call_id="c1", name="web_search")
    assert tool_outcome(msg) == (True, "8 results")


def test_tool_outcome_quiet_tools_have_no_detail_on_success():
    msg = ToolMessage(content="line 1\nline 2", tool_call_id="c1", name="read_file")
    assert tool_outcome(msg) == (True, "")


def test_tool_outcome_error_keeps_detail_even_for_quiet_tools():
    msg = ToolMessage(content="No such file", tool_call_id="c1", name="read_file", status="error")
    assert tool_outcome(msg) == (False, "No such file")


def test_args_preview_first_non_empty_string_collapsed_and_truncated():
    assert args_preview({"n": 3, "query": "  погода\n Орёл  "}) == "погода Орёл"
    long = args_preview({"q": "x" * 100})
    assert len(long) == 40 and long.endswith("…")
    assert args_preview({"n": 3}) == ""
