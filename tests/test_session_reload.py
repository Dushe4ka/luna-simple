import io
from pathlib import Path

from luna_core.errors import ProviderError
from luna_core.providers.fake import FakeProvider
from luna_core.session import Session
from rich.console import Console

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.session import TurnSummary, run_repl
from luna.core.store import open_log


class _Bundle:
    """A stand-in bundle: ``agent`` is a real core agent, ``aclose`` is recorded."""

    def __init__(self, agent, tag):
        self.agent, self.tag, self.closed = agent, tag, False

    async def aclose(self):
        self.closed = True


async def _run(tmp_path, lines, *, script=(), rebuild=None, bundle=None):
    cfg = LunaConfig(workdir=str(tmp_path), model="m", format_command="")
    real = await build_agent(cfg, provider=FakeProvider(list(script) or [FakeProvider.text("ok")]))
    bundle = bundle or _Bundle(real.agent, 0)
    log = open_log()
    out = io.StringIO()
    answers = iter(lines)
    code = await run_repl(
        bundle,
        console=Console(file=out, force_terminal=True, no_color=True),
        input_fn=lambda _: next(answers),
        rebuild=rebuild,
        log=log,
        session=Session.create(log, str(Path(tmp_path).resolve())),
        config=cfg,
    )
    await real.aclose()
    return code, out.getvalue(), bundle


async def test_reload_swaps_the_bundle_and_closes_the_old_one(tmp_path):
    built = []

    async def rebuild():
        new = _Bundle(None, len(built) + 1)
        built.append(new)
        return new

    code, out, first = await _run(tmp_path, ["/reload", "/reload", "/exit"], rebuild=rebuild)
    assert code == 0 and len(built) == 2
    assert first.closed and built[0].closed and not built[1].closed
    assert "reloaded" in out.lower()


async def test_reload_failure_keeps_the_old_bundle(tmp_path):
    async def rebuild():
        raise RuntimeError("bad toml")

    code, out, first = await _run(tmp_path, ["/reload", "/exit"], rebuild=rebuild)
    assert code == 0 and "/reload failed: bad toml" in out and not first.closed


async def test_turn_failure_does_not_kill_the_session(tmp_path):
    """A provider error ends that turn with "turn failed"; the next line still runs.

    Transient errors are retried inside the core providers (``with_retries``)
    before a turn ever sees them; what reaches the REPL is final.
    """
    script = [[ProviderError("boom", kind="server")], FakeProvider.text("recovered")]
    code, out, _ = await _run(tmp_path, ["do a thing", "again", "/exit"], script=script)
    assert code == 0
    assert "turn failed:" in out and "boom" in out
    assert "recovered" in out


async def test_auto_reload_failure_keeps_old_agent(tmp_path, monkeypatch):
    from luna.core import session

    async def fake_render(*a, **k):
        return TurnSummary(reload=True)

    async def rebuild():
        raise RuntimeError("bad toml")

    monkeypatch.setattr(session, "render_turn", fake_render)
    code, out, first = await _run(tmp_path, ["trigger reload", "/exit"], rebuild=rebuild)
    assert code == 0 and "auto-reload failed" in out and not first.closed


async def test_auto_reload_after_a_reload_requested_turn(tmp_path, monkeypatch):
    from luna.core import session

    async def fake_render(*a, **k):
        return TurnSummary(reload=True)

    async def rebuild():
        return _Bundle(None, 1)

    monkeypatch.setattr(session, "render_turn", fake_render)
    code, out, first = await _run(tmp_path, ["trigger reload", "/exit"], rebuild=rebuild)
    assert code == 0 and "auto-reloaded" in out and first.closed


async def test_init_rebuilds_after_its_turn(tmp_path):
    built = []

    async def rebuild():
        built.append(1)
        return _Bundle(None, 1)

    code, out, _ = await _run(
        tmp_path, ["/init", "/exit"], script=[FakeProvider.text("wrote AGENTS.md")], rebuild=rebuild
    )
    assert code == 0 and built == [1] and "wrote AGENTS.md" in out


async def test_reload_unavailable_without_rebuild(tmp_path):
    code, out, _ = await _run(tmp_path, ["/reload", "/exit"])
    assert code == 0
    assert "not available" in out.lower()


async def test_input_closing_at_an_approval_cancels_the_turn_not_the_repl(tmp_path):
    script = [FakeProvider.call("write_file", {"file_path": "a.txt", "content": "x"})]
    code, out, _ = await _run(tmp_path, ["write it"], script=script)  # no answer, then EOF
    assert code == 0 and "turn cancelled" in out and not (tmp_path / "a.txt").exists()


async def test_an_unexpected_turn_failure_does_not_kill_the_repl(tmp_path, monkeypatch):
    from luna.core import session

    async def boom(*a, **k):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(session, "render_turn", boom)
    code, out, _ = await _run(tmp_path, ["one", "two", "/exit"])
    assert code == 0 and out.count("turn failed: db is gone") == 2


async def test_auto_reload_rereads_custom_commands(tmp_path, monkeypatch):
    from luna.core import session

    async def fake_render(*a, **k):
        (tmp_path / ".luna" / "commands").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".luna" / "commands" / "fresh.md").write_text("fresh $ARGUMENTS\n")
        return TurnSummary(reload=True)

    async def rebuild():
        return _Bundle(None, 1)

    monkeypatch.setattr(session, "render_turn", fake_render)
    code, out, _ = await _run(tmp_path, ["install it", "/commands", "/exit"], rebuild=rebuild)
    assert code == 0 and "/fresh" in out
