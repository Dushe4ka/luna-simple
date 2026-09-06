import io

from langchain_core.messages import AIMessage
from rich.console import Console

from luna.agent import build_agent
from luna.config import LunaConfig
from luna.session import SLASH_COMMANDS, collect_decisions, run_once


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
