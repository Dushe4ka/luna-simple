import io

from rich.console import Console

from luna.commands import CommandContext, dispatch
from luna.config import LunaConfig


def _ctx(**kw):
    base = dict(
        console=Console(file=io.StringIO(), force_terminal=True),
        config=LunaConfig(),
        agent=object(),
        rebuild=lambda: "rebuilt",
        thread_id="t",
        workdir=".",
        index=None,
    )
    base.update(kw)
    return CommandContext(**base)


def test_unknown_command_handled_but_noop():
    res = dispatch("/nope", _ctx())
    assert res.handled is True and res.exit is False


def test_exit_command():
    assert dispatch("/exit", _ctx()).exit is True


def test_non_command_not_handled():
    assert dispatch("hello world", _ctx()).handled is False


def test_reload_swaps_agent():
    res = dispatch("/reload", _ctx())
    assert res.agent == "rebuilt"


def test_new_rotates_thread():
    res = dispatch("/new", _ctx())
    assert res.thread_id and res.thread_id != "t"


def test_compact_rotates_thread_with_summary(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent

    ctx = _ctx(
        agent=build_agent(
            LunaConfig(workdir=str(tmp_path)),
            model=fake_model(AIMessage(content="SUMMARY: did X")),
        ),
        workdir=str(tmp_path),
        thread_id="old",
    )
    res = dispatch("/compact", ctx)
    assert res.thread_id and res.thread_id != "old"
    state = ctx.agent.get_state({"configurable": {"thread_id": res.thread_id}})
    assert any("did X" in getattr(m, "content", "") for m in state.values["messages"])
