import io

from langchain_core.messages import AIMessage
from rich.console import Console

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.session import run_once


def test_agent_writes_a_file_after_approval(tmp_path, fake_model):
    write_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "id": "1",
                "args": {"file_path": "/hello.txt", "content": "hi\n"},
            }
        ],
    )
    done = AIMessage(content="created hello.txt")
    agent = build_agent(LunaConfig(workdir=str(tmp_path)), model=fake_model(write_call, done))
    text = run_once(
        agent,
        "make hello.txt",
        thread_id="e2e",
        console=Console(file=io.StringIO(), force_terminal=True),
        input_fn=lambda _: "",  # approve
    )
    assert (tmp_path / "hello.txt").read_text() == "hi\n"
    assert "hello.txt" in text


def test_agent_respects_rejection(tmp_path, fake_model):
    write_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "id": "1",
                "args": {"file_path": "/nope.txt", "content": "x"},
            }
        ],
    )
    done = AIMessage(content="ok, skipped it")
    agent = build_agent(LunaConfig(workdir=str(tmp_path)), model=fake_model(write_call, done))
    answers = iter(["n", "not needed"])
    run_once(
        agent,
        "make nope.txt",
        thread_id="e2e2",
        console=Console(file=io.StringIO(), force_terminal=True),
        input_fn=lambda _: next(answers),
    )
    assert not (tmp_path / "nope.txt").exists()
