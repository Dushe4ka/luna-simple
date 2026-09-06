import io

from rich.console import Console

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
