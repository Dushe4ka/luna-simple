import io

from luna_core.providers.fake import FakeProvider
from rich.console import Console

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.session import run_once
from luna.core.store import open_log


def _console():
    return Console(file=io.StringIO(), force_terminal=True)


async def test_agent_writes_a_file_after_approval(tmp_path):
    cfg = LunaConfig(workdir=str(tmp_path), model="m", format_command="")
    script = [
        FakeProvider.call("write_file", {"file_path": "/hello.txt", "content": "hi\n"}),
        FakeProvider.text("created hello.txt"),
    ]
    bundle = await build_agent(cfg, provider=FakeProvider(script))
    text = await run_once(
        bundle, "make hello.txt", console=_console(), log=open_log(), cfg=cfg, input_fn=lambda _: ""
    )
    await bundle.aclose()
    assert (tmp_path / "hello.txt").read_text() == "hi\n"
    assert "hello.txt" in text


async def test_agent_respects_rejection(tmp_path):
    cfg = LunaConfig(workdir=str(tmp_path), model="m", format_command="")
    script = [
        FakeProvider.call("write_file", {"file_path": "/nope.txt", "content": "x"}),
        FakeProvider.text("ok, skipped it"),
    ]
    bundle = await build_agent(cfg, provider=FakeProvider(script))
    answers = iter(["n", "not needed"])
    await run_once(
        bundle,
        "make nope.txt",
        console=_console(),
        log=open_log(),
        cfg=cfg,
        input_fn=lambda _: next(answers),
    )
    await bundle.aclose()
    assert not (tmp_path / "nope.txt").exists()
