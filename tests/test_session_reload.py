import io

from rich.console import Console

from luna.config.usage import TurnUsage
from luna.core.session import run_repl


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
    from luna.core import session

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
    from luna.core import session

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(session, "_stream_turn", boom)
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    answers = iter(["do a thing", "/exit"])
    out = io.StringIO()
    code = run_repl(
        _FakeAgent(0),
        console=Console(file=out, force_terminal=True, no_color=True),
        input_fn=lambda _: next(answers),
    )
    assert code == 0
    assert "turn failed:" in out.getvalue()  # the final give-up message, after the cap
    # a persistently-failing turn is retried up to _MAX_TURN_RETRIES (2) times
    # — 3 total attempts — before the caller finally sees the exception.
    assert calls == [1, 1, 1]
    assert "retrying in" in out.getvalue()


def test_turn_retries_and_recovers_from_a_transient_failure(monkeypatch):
    """A turn that fails once and then succeeds must not surface any error —
    the retry is transparent to the user beyond the one "retrying" notice."""
    from luna.core import session

    calls = []
    real_result = ("ok", False, TurnUsage(), set())

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("simulated network timeout")
        return real_result

    monkeypatch.setattr(session, "_stream_turn", flaky)
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    answers = iter(["do a thing", "/exit"])
    out = io.StringIO()
    code = run_repl(
        _FakeAgent(0),
        console=Console(file=out, force_terminal=True, no_color=True),
        input_fn=lambda _: next(answers),
    )
    assert code == 0
    assert calls == [1, 1]
    assert "retrying in" in out.getvalue()
    # the final give-up message ("turn failed: <exc>") from run_repl's own
    # except-block must never fire — the retry recovered before the cap
    assert "turn failed:" not in out.getvalue()


def test_auto_reload_failure_keeps_old_agent(monkeypatch):
    from luna.core import session

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
