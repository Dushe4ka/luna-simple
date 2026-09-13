import io

from langchain_core.messages import AIMessage
from rich.console import Console

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.session import SLASH_COMMANDS, collect_decisions, run_once


def _console():
    return Console(file=io.StringIO(), force_terminal=True)


def test_collect_decisions_maps_multiple():
    iv = {
        "action_requests": [
            {"name": "write_file", "args": {"file_path": "/a", "content": "x"}},
            {"name": "execute", "args": {"command": "ls"}},
        ]
    }
    answers = iter(["", "n", "nope"])
    out = collect_decisions(_console(), iv, input_fn=lambda _: next(answers))
    assert out["decisions"][0] == {"type": "approve"}
    assert out["decisions"][1]["type"] == "reject"
    assert out["decisions"][1]["message"] == "nope"


def test_collect_decisions_handles_singular_key():
    iv = {"action_request": {"name": "delete", "args": {"file_path": "/x"}}}
    out = collect_decisions(_console(), iv, input_fn=lambda _: "")
    assert out == {"decisions": [{"type": "approve"}]}


def test_run_once_returns_final_text(tmp_path, fake_model):
    agent = build_agent(
        LunaConfig(workdir=str(tmp_path)), model=fake_model(AIMessage(content="done"))
    )
    text = run_once(agent, "hi", thread_id="t", console=_console())
    assert text == "done"


def test_slash_help_registered():
    assert "/help" in SLASH_COMMANDS
    assert "/exit" in SLASH_COMMANDS
    assert "/tools" in SLASH_COMMANDS


def test_at_agent_mention_is_rewritten_to_a_delegation_instruction(tmp_path, fake_model):
    from luna.core.session import run_repl

    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(AIMessage(content="ok"))
    )
    seen: list[str] = []
    real_stream = agent.stream

    def _spy(payload, *a, **kw):
        if isinstance(payload, dict):
            msgs = payload.get("messages", [])
            if msgs:
                seen.append(msgs[-1]["content"])
        yield from real_stream(payload, *a, **kw)

    agent.stream = _spy
    console = _console()
    lines = iter(["@researcher find the entry point", "/exit"])
    run_repl(
        agent,
        console=console,
        input_fn=lambda _: next(lines),
        rebuild=lambda: agent,
        workdir=str(tmp_path),
        config=LunaConfig(workdir=str(tmp_path), yolo=True),
        thread_id="t",
    )
    assert seen and "researcher" in seen[0] and "task tool" in seen[0]
    assert "find the entry point" in seen[0]


def test_stream_turn_pauses_and_resumes_progress_around_an_interrupt(monkeypatch):
    from types import SimpleNamespace

    from luna.core import session

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

    monkeypatch.setattr(session, "ToolProgress", _SpyProgress)

    tool_call = AIMessage(
        content="", tool_calls=[{"name": "write_file", "id": "1", "args": {"file_path": "/a"}}]
    )
    interrupt = SimpleNamespace(
        value={"action_requests": [{"name": "write_file", "args": {"file_path": "/a"}}]}
    )

    class _FakeAgent:
        def __init__(self):
            self._resumed = False

        def stream(self, payload, config, stream_mode):
            if not self._resumed:
                yield "updates", {"model": {"messages": [tool_call]}}
            else:
                yield "messages", (AIMessage(content="done"), {"langgraph_node": "model"})

        def get_state(self, config):
            if not self._resumed:
                self._resumed = True
                return SimpleNamespace(values={}, interrupts=[interrupt])
            return SimpleNamespace(values={}, interrupts=[])

    console = Console(file=io.StringIO(), force_terminal=True, no_color=True)
    session._stream_turn(
        _FakeAgent(),
        {"messages": [{"role": "user", "content": "hi"}]},
        {"configurable": {"thread_id": "t"}},
        console,
        input_fn=lambda _: "",
    )
    assert events == ["created", "start", "pause", "resume", "close"]
