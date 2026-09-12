import io

from rich.console import Console

from luna.config.usage import TurnUsage
from luna.session import run_repl


class _FakeAgent:
    def __init__(self, tag):
        self.tag = tag


def test_reload_swaps_agent():
    built = []

    def rebuild():
        agent = _FakeAgent(len(built))
        built.append(agent)
        return agent

    answers = iter(["/reload", "/exit"])
    out = io.StringIO()
    code = run_repl(
        rebuild(),  # initial agent
        console=Console(file=out, force_terminal=True, no_color=True),
        input_fn=lambda _: next(answers),
        rebuild=rebuild,
    )
    assert code == 0
    assert len(built) == 2  # initial + one /reload
    assert "reload" in out.getvalue().lower()


def test_reload_refreshes_allow_rules(monkeypatch):
    from luna import session

    calls = []
    monkeypatch.setattr(session, "load_rules", lambda wd: calls.append(wd) or f"rules-{len(calls)}")

    answers = iter(["/reload", "/exit"])
    run_repl(
        _FakeAgent(0),
        console=Console(file=io.StringIO(), force_terminal=True, no_color=True),
        input_fn=lambda _: next(answers),
        rebuild=lambda: _FakeAgent(1),
    )
    assert len(calls) >= 2  # once before the loop, again after /reload swapped the agent


def test_turn_failure_does_not_kill_the_session(monkeypatch):
    from luna import session

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(session, "_stream_turn", boom)
    answers = iter(["do a thing", "/exit"])
    out = io.StringIO()
    code = run_repl(
        _FakeAgent(0),
        console=Console(file=out, force_terminal=True, no_color=True),
        input_fn=lambda _: next(answers),
    )
    assert code == 0
    assert "turn failed" in out.getvalue()
    assert calls == [1]


def test_auto_reload_failure_keeps_old_agent(monkeypatch):
    from luna import session

    def fake_stream(*a, **k):
        return "", True, TurnUsage(), set()

    monkeypatch.setattr(session, "_stream_turn", fake_stream)
    answers = iter(["trigger reload", "/exit"])
    out = io.StringIO()
    code = run_repl(
        _FakeAgent(0),
        console=Console(file=out, force_terminal=True, no_color=True),
        input_fn=lambda _: next(answers),
        rebuild=lambda: (_ for _ in ()).throw(RuntimeError("bad toml")),
    )
    assert code == 0
    assert "auto-reload failed" in out.getvalue()


def test_reload_unavailable_without_rebuild():
    answers = iter(["/reload", "/exit"])
    out = io.StringIO()
    code = run_repl(
        _FakeAgent(0),
        console=Console(file=out, force_terminal=True, no_color=True),
        input_fn=lambda _: next(answers),
    )
    assert code == 0
    assert "not available" in out.getvalue().lower()
