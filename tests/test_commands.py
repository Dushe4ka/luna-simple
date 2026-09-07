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
