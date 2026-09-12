import io

from rich.console import Console

from luna.config.config import LunaConfig
from luna.repl.commands import CommandContext, dispatch


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


def test_user_command_falls_through_to_a_prompt(tmp_path):
    from luna.repl.usercmd import UserCommand

    ctx = _ctx(
        workdir=str(tmp_path),
        user_commands={"greet": UserCommand("greet", "", "hi $ARGUMENTS")},
    )
    res = dispatch("/greet world", ctx)
    assert res.handled is True
    assert res.prompt == "hi world"


def test_unknown_command_still_reported_when_no_user_command_matches():
    res = dispatch("/nope", _ctx())
    assert res.prompt is None


def test_reload_swaps_agent():
    res = dispatch("/reload", _ctx())
    assert res.agent == "rebuilt"


def test_reload_failure_does_not_raise():
    ctx = _ctx(
        rebuild=lambda: (_ for _ in ()).throw(RuntimeError("bad toml")),
        console=Console(file=io.StringIO()),
    )
    res = dispatch("/reload", ctx)
    assert res.handled is True
    assert res.agent is None
    assert "/reload failed" in ctx.console.file.getvalue()


def test_agents_with_broken_subagents_toml_does_not_kill_repl(tmp_path):
    d = tmp_path / ".luna"
    d.mkdir()
    (d / "subagents.toml").write_text(
        '[subagent.x]\ndescription = "x"\ntools = ["execute", "read_file"]\n'
    )
    ctx = _ctx(workdir=str(tmp_path), console=Console(file=io.StringIO()))
    res = dispatch("/agents", ctx)
    assert res.handled is True and res.exit is False
    out = ctx.console.file.getvalue()
    assert "unsafe = true" in out


def test_new_rotates_thread():
    res = dispatch("/new", _ctx())
    assert res.thread_id and res.thread_id != "t"


def test_model_swap_rebuilds(monkeypatch):
    ctx = _ctx(config=LunaConfig(model="claude-sonnet-4-5"))
    res = dispatch("/model claude-opus-4", ctx)
    assert ctx.config.model == "claude-opus-4"
    assert res.agent == "rebuilt"


def test_provider_without_key_does_not_swap(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ctx = _ctx(config=LunaConfig(provider="deepseek"))
    dispatch("/provider anthropic", ctx)
    assert ctx.config.provider == "deepseek"  # unchanged


def test_sessions_lists(monkeypatch, capsys):
    from luna.persistence import SessionIndex

    idx = SessionIndex()
    idx.record("t-old", ".", "older task")
    idx.record("t-new", ".", "newer task")
    ctx = _ctx(index=idx, workdir=".")
    res = dispatch("/sessions", ctx)
    assert res.handled is True
    out = ctx.console.file.getvalue()
    assert "older task" in out and "newer task" in out


def test_resume_by_number_swaps_thread():
    from luna.persistence import SessionIndex

    idx = SessionIndex()
    idx.record("t-old", ".", "older")
    idx.record("t-new", ".", "newer")
    ctx = _ctx(index=idx, workdir=".")
    # newest first → [1] = t-new, [2] = t-old
    res = dispatch("/resume 2", ctx)
    assert res.thread_id == "t-old"


def test_resume_no_arg_lists_and_hints():
    from luna.persistence import SessionIndex

    idx = SessionIndex()
    idx.record("t1", ".", "one")
    ctx = _ctx(index=idx, workdir=".")
    res = dispatch("/resume", ctx)
    assert res.thread_id is None
    assert "one" in ctx.console.file.getvalue()


def test_sessions_without_index_is_graceful():
    ctx = _ctx(index=None)
    res = dispatch("/sessions", ctx)
    assert res.handled is True  # no crash


def test_undo_declined_does_not_revert(tmp_path):
    from luna.turn.undo import journal_dir, snapshot

    f = tmp_path / "a.py"
    f.write_text("old\n")
    snapshot(str(tmp_path), "s1", "edit_file", "a.py")
    f.write_text("new\n")
    ctx = _ctx(workdir=str(tmp_path), session_id="s1", input_fn=lambda _: "n")
    dispatch("/undo", ctx)
    assert f.read_text() == "new\n"  # file untouched
    assert list(journal_dir(str(tmp_path), "s1").glob("[0-9]*.json"))  # entry kept


def test_undo_confirmed_reverts(tmp_path):
    from luna.turn.undo import snapshot

    f = tmp_path / "a.py"
    f.write_text("old\n")
    snapshot(str(tmp_path), "s1", "edit_file", "a.py")
    f.write_text("new\n")
    ctx = _ctx(workdir=str(tmp_path), session_id="s1", input_fn=lambda _: "y")
    dispatch("/undo", ctx)
    assert f.read_text() == "old\n"


def test_undo_without_input_fn_proceeds(tmp_path):
    from luna.turn.undo import snapshot

    f = tmp_path / "a.py"
    f.write_text("old\n")
    snapshot(str(tmp_path), "s1", "edit_file", "a.py")
    f.write_text("new\n")
    ctx = _ctx(workdir=str(tmp_path), session_id="s1")  # input_fn is None
    dispatch("/undo", ctx)
    assert f.read_text() == "old\n"


def test_model_switch_failure_rolls_back(monkeypatch):
    def boom():
        raise RuntimeError("no such model")

    ctx = _ctx(
        config=LunaConfig(model="claude-sonnet-4-5"),
        rebuild=boom,
        console=Console(file=io.StringIO()),
    )
    res = dispatch("/model claude-opus-4", ctx)
    assert res.agent is None
    assert ctx.config.model == "claude-sonnet-4-5"  # rolled back
    assert "could not switch" in ctx.console.file.getvalue()


def test_compact_failure_does_not_raise(tmp_path):
    class _BoomAgent:
        def invoke(self, *a, **k):
            raise RuntimeError("boom")

    ctx = _ctx(
        agent=_BoomAgent(),
        workdir=str(tmp_path),
        thread_id="old",
        console=Console(file=io.StringIO()),
    )
    res = dispatch("/compact", ctx)
    assert res.handled is True and res.thread_id is None
    assert "/compact failed" in ctx.console.file.getvalue()


def test_compact_replaces_history_in_place(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent

    agent = build_agent(
        LunaConfig(workdir=str(tmp_path)),
        model=fake_model(AIMessage(content="chatter"), AIMessage(content="SUMMARY: did X and Y")),
    )
    cfg = {"configurable": {"thread_id": "keep"}}
    agent.invoke({"messages": [{"role": "user", "content": "hello"}]}, config=cfg)

    ctx = _ctx(agent=agent, thread_id="keep", workdir=str(tmp_path), index=None)
    res = dispatch("/compact", ctx)

    assert res.thread_id is None  # same thread
    msgs = agent.get_state(cfg).values["messages"]
    assert len(msgs) == 1
    assert "did X and Y" in msgs[0].content
    assert msgs[0].type == "human"


def test_add_drop_context_handlers(tmp_path):
    from luna.turn.context import PinnedFiles

    (tmp_path / "p.py").write_text("P = 1\n")
    pins = PinnedFiles()
    # highlight=False: Rich would otherwise splice ANSI codes through "/add",
    # breaking a literal substring match on the usage line.
    ctx = _ctx(
        pinned=pins,
        workdir=str(tmp_path),
        console=Console(file=io.StringIO(), force_terminal=True, highlight=False),
    )

    dispatch("/add p.py", ctx)
    assert pins.paths == ["p.py"]
    dispatch("/context", ctx)
    assert "p.py" in ctx.console.file.getvalue()
    dispatch("/drop p.py", ctx)
    assert pins.paths == []
    dispatch("/add", ctx)  # no arg -> usage line, no crash
    assert "usage: /add" in ctx.console.file.getvalue()


def test_compact_no_summary_is_graceful(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent

    agent = build_agent(LunaConfig(workdir=str(tmp_path)), model=fake_model(AIMessage(content="")))
    ctx = _ctx(agent=agent, thread_id="t", workdir=str(tmp_path), index=None)
    res = dispatch("/compact", ctx)  # must not raise
    assert res.handled is True


def test_compact_empty_summary_trims_the_instruction_turn(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent

    # first turn produces no reusable AI text; the summary turn is empty too
    agent = build_agent(
        LunaConfig(workdir=str(tmp_path)),
        model=fake_model(AIMessage(content=""), AIMessage(content="")),
    )
    cfg = {"configurable": {"thread_id": "keep"}}
    agent.invoke({"messages": [{"role": "user", "content": "hello"}]}, config=cfg)
    before = len(agent.get_state(cfg).values["messages"])

    ctx = _ctx(agent=agent, thread_id="keep", workdir=str(tmp_path), index=None)
    dispatch("/compact", ctx)

    after = len(agent.get_state(cfg).values["messages"])
    assert after == before  # the failed-summary turn was rolled back
    assert "no summary produced" in ctx.console.file.getvalue()


def test_plan_toggle_and_explicit_state():
    ctx = _ctx()
    ctx.plan_state = [False]
    dispatch("/plan", ctx)
    assert ctx.plan_state[0] is True
    dispatch("/plan off", ctx)
    assert ctx.plan_state[0] is False
    dispatch("/plan on", ctx)
    assert ctx.plan_state[0] is True
