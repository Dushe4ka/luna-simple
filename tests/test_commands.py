import io
from pathlib import Path

import pytest
from luna_core.messages import Message, Text, message_to_dict
from luna_core.providers.fake import FakeProvider
from luna_core.session import Session
from luna_core.usage import Usage
from rich.console import Console

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.session_state import SessionState, SessionStateStore
from luna.core.store import open_log
from luna.repl.commands import CommandContext, dispatch


class _Stub:
    """A rebuilt bundle stand-in."""

    def __init__(self, tag):
        self.tag, self.closed, self.agent = tag, False, None

    async def aclose(self):
        self.closed = True


@pytest.fixture
async def make_ctx(tmp_path):
    bundles = []

    async def _make(*script, **kw):
        cfg = kw.pop("config", None) or LunaConfig(workdir=str(tmp_path), model="m")
        bundle = await build_agent(
            cfg, provider=FakeProvider(list(script) or [FakeProvider.text("ok")])
        )
        bundles.append(bundle)
        log = open_log()
        session = kw.pop("session", None) or Session.create(log, str(Path(tmp_path).resolve()))

        async def rebuild():
            return _Stub("rebuilt")

        base = dict(
            console=Console(file=io.StringIO(), force_terminal=True, highlight=False),
            config=cfg,
            bundle=bundle,
            rebuild=rebuild,
            session=session,
            log=log,
            state=SessionState(session.id, session.workdir),
        )
        base.update(kw)
        return CommandContext(**base)

    yield _make
    for bundle in bundles:
        await bundle.aclose()


def _out(ctx):
    return ctx.console.file.getvalue()


def _turn(session, tid, user, reply):
    session.append("turn_started", {"turn_id": tid})
    session.append("user", {"message": message_to_dict(Message.user(user))})
    session.append(
        "assistant",
        {
            "message": message_to_dict(Message.assistant([Text(reply)])),
            "usage": Usage(5, 1).to_dict(),
        },
    )
    session.append("turn_done", {"turn_id": tid, "usage": Usage(5, 1).to_dict(), "cost": None})


async def test_unknown_command_handled_but_noop(make_ctx):
    res = await dispatch("/nope", await make_ctx())
    assert res.handled is True and res.exit is False and res.prompt is None


async def test_exit_command(make_ctx):
    assert (await dispatch("/exit", await make_ctx())).exit is True
    assert (await dispatch("/quit", await make_ctx())).exit is True


async def test_non_command_not_handled(make_ctx):
    assert (await dispatch("hello world", await make_ctx())).handled is False


async def test_user_command_falls_through_to_a_prompt(make_ctx):
    from luna.repl.usercmd import UserCommand

    ctx = await make_ctx(user_commands={"greet": UserCommand("greet", "", "hi $ARGUMENTS")})
    res = await dispatch("/greet world", ctx)
    assert res.handled is True and res.prompt == "hi world"


async def test_reload_swaps_and_closes_the_bundle(make_ctx):
    ctx = await make_ctx()
    first = _Stub("first")
    ctx.bundle = first
    await dispatch("/reload", ctx)
    assert ctx.bundle.tag == "rebuilt" and first.closed
    assert "reloaded" in _out(ctx)


async def test_reload_failure_does_not_raise(make_ctx):
    async def boom():
        raise RuntimeError("bad toml")

    ctx = await make_ctx(rebuild=boom)
    before = ctx.bundle
    res = await dispatch("/reload", ctx)
    assert res.handled is True and ctx.bundle is before
    assert "/reload failed" in _out(ctx)


async def test_agents_with_broken_subagents_toml_does_not_kill_repl(make_ctx, tmp_path):
    ctx = await make_ctx()  # the file breaks after the agent was built
    d = tmp_path / ".luna"
    d.mkdir(exist_ok=True)
    (d / "subagents.toml").write_text(
        '[subagent.x]\ndescription = "x"\ntools = ["execute", "read_file"]\n'
    )
    res = await dispatch("/agents", ctx)
    assert res.handled is True and res.exit is False
    assert "unsafe = true" in _out(ctx)


async def test_new_starts_a_fresh_session(make_ctx):
    ctx = await make_ctx()
    old = ctx.session.id
    ctx.state.pinned.add("a.py")
    await dispatch("/new", ctx)
    assert ctx.session.id != old and ctx.state.thread_id == ctx.session.id
    assert ctx.state.pinned.paths == []


async def test_model_swap_rebuilds(make_ctx, tmp_path):
    ctx = await make_ctx(config=LunaConfig(workdir=str(tmp_path), model="claude-sonnet-4-5"))
    await dispatch("/model claude-opus-4", ctx)
    assert ctx.config.model == "claude-opus-4" and ctx.bundle.tag == "rebuilt"


async def test_model_switch_failure_rolls_back(make_ctx, tmp_path):
    async def boom():
        raise RuntimeError("no such model")

    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), model="claude-sonnet-4-5"), rebuild=boom
    )
    await dispatch("/model claude-opus-4", ctx)
    assert ctx.config.model == "claude-sonnet-4-5"  # rolled back
    assert "could not switch" in _out(ctx)


async def test_model_with_no_arg_and_no_input_fn_just_prints(make_ctx, tmp_path):
    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), provider="anthropic", model="claude-sonnet-4-5"),
        input_fn=None,
    )
    res = await dispatch("/model", ctx)
    assert res.handled is True and "claude-sonnet-4-5" in _out(ctx)


async def test_model_with_no_arg_uses_the_picker_and_rebuilds(make_ctx, tmp_path, monkeypatch):
    from luna.config import model_discovery

    monkeypatch.setattr(model_discovery, "list_models", lambda *a, **k: None)
    monkeypatch.setattr(model_discovery, "known_models", lambda provider: ["model-a", "model-b"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    lines = iter(["2"])  # pick #2 from the numbered plain-text list
    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), provider="anthropic"),
        input_fn=lambda _prompt: next(lines),
    )
    await dispatch("/model", ctx)
    assert ctx.config.model == "model-b" and ctx.bundle.tag == "rebuilt"


async def test_model_with_no_arg_falls_back_to_free_text_when_no_list(
    make_ctx, tmp_path, monkeypatch
):
    from luna.config import model_discovery

    monkeypatch.setattr(model_discovery, "list_models", lambda *a, **k: None)
    monkeypatch.setattr(model_discovery, "known_models", lambda provider: [])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    lines = iter(["claude-opus-4-1"])
    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), provider="anthropic"),
        input_fn=lambda _prompt: next(lines),
    )
    await dispatch("/model", ctx)
    assert ctx.config.model == "claude-opus-4-1"


async def test_model_without_arg_and_no_terminal_does_not_hit_the_network(make_ctx, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_models_for", lambda cfg: pytest.fail("network call"))
    ctx = await make_ctx()
    await dispatch("/model", ctx)
    assert "model: m" in _out(ctx)


async def test_provider_without_key_does_not_swap(make_ctx, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ctx = await make_ctx(config=LunaConfig(workdir=str(tmp_path), provider="deepseek"))
    await dispatch("/provider anthropic", ctx)
    assert ctx.config.provider == "deepseek"  # unchanged


async def test_provider_recognizes_a_custom_provider_from_config(make_ctx, tmp_path, monkeypatch):
    from luna.config.providers import ProviderSpec

    monkeypatch.setenv("MYLOCAL_API_KEY", "sk-test")
    custom = {
        "mylocal": ProviderSpec(
            "mylocal", "openai_chat", "local-model", "MYLOCAL_API_KEY", "http://localhost:8000/v1"
        )
    }
    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), provider="anthropic", custom_providers=custom)
    )
    await dispatch("/provider mylocal", ctx)
    assert ctx.config.provider == "mylocal"
    assert "unknown provider" not in _out(ctx)


async def test_sessions_lists_only_this_folders_top_level_sessions(make_ctx, tmp_path):
    ctx = await make_ctx()
    log = ctx.log
    workdir = ctx.session.workdir
    for title, folder, parent in (
        ("older task", workdir, None),
        ("newer task", workdir, None),
        ("a subagent run", workdir, ctx.session.id),
        ("other project", str(tmp_path / "elsewhere"), None),
    ):
        sid = log.create(folder, title=title, parent_id=parent)
        _turn(Session(log, sid, workdir=folder, undo=False), "t", title, "ok")
    log.create(workdir, title="never used")  # an empty REPL start
    res = await dispatch("/sessions", ctx)
    assert res.handled is True
    out = _out(ctx)
    assert "older task" in out and "newer task" in out
    assert "subagent run" not in out and "other project" not in out
    assert "never used" not in out and "(untitled)" not in out  # empty sessions are skipped


async def test_resume_by_number_switches_session_and_prints_a_recap(make_ctx):
    ctx = await make_ctx()
    log, workdir = ctx.log, ctx.session.workdir
    old = Session.create(log, workdir, title="older")
    _turn(old, "t1", "what does the old code do?", "it parses the config file")
    store = SessionStateStore()
    saved = store.load(old.id, workdir)
    saved.pinned.add("kept.py")
    store.save(saved)
    newer = Session.create(log, workdir, title="newer")
    _turn(newer, "t1", "newer question", "newer answer")
    active = ctx.session.id
    # newest first, empty sessions (the active one) skipped → [1] = newer, [2] = older
    await dispatch("/resume 2", ctx)
    out = _out(ctx)
    assert ctx.session.id == old.id != active
    assert "— resuming, last" in out and "it parses the config file" in out
    assert "resumed session" in out
    assert ctx.state.pinned.paths == ["kept.py"]  # the resumed session's own settings


async def test_resume_no_arg_lists_and_hints(make_ctx):
    ctx = await make_ctx(input_fn=None)
    _turn(Session.create(ctx.log, ctx.session.workdir, title="one"), "t1", "q", "a")
    before = ctx.session.id
    await dispatch("/resume", ctx)
    assert ctx.session.id == before
    assert "one" in _out(ctx) and "usage: /resume" in _out(ctx)


async def test_resume_refuses_another_folders_session(make_ctx, tmp_path):
    ctx = await make_ctx()
    other = ctx.log.create(str(tmp_path / "elsewhere"), title="x")
    before = ctx.session.id
    await dispatch(f"/resume {other}", ctx)
    assert ctx.session.id == before and "another folder" in _out(ctx)


async def test_resume_unknown_id_is_graceful(make_ctx):
    ctx = await make_ctx()
    await dispatch("/resume nope", ctx)
    assert "no session 'nope'" in _out(ctx)


async def test_undo_declined_does_not_rewind(make_ctx):
    ctx = await make_ctx(input_fn=lambda _: "n")
    _turn(ctx.session, "t1", "first", "a")
    await dispatch("/undo", ctx)
    assert "undo cancelled" in _out(ctx)
    assert [m.text for m in ctx.session.messages()] == ["first", "a"]


async def test_undo_confirmed_rewinds_and_redo_restores(make_ctx):
    ctx = await make_ctx(input_fn=lambda _: "y")
    _turn(ctx.session, "t1", "first", "a")
    _turn(ctx.session, "t2", "second", "b")
    await dispatch("/undo", ctx)
    assert [m.text for m in ctx.session.messages()] == ["first", "a"]
    await dispatch("/redo", ctx)
    assert [m.text for m in ctx.session.messages()] == ["first", "a", "second", "b"]
    assert "turn restored" in _out(ctx)


async def test_undo_without_input_fn_proceeds(make_ctx):
    ctx = await make_ctx(input_fn=None)
    _turn(ctx.session, "t1", "first", "a")
    await dispatch("/undo", ctx)
    assert ctx.session.messages() == []


async def test_undo_and_redo_with_nothing_to_do(make_ctx):
    ctx = await make_ctx()
    await dispatch("/undo", ctx)
    await dispatch("/redo", ctx)
    assert "nothing to undo" in _out(ctx) and "nothing to redo" in _out(ctx)


async def test_diff_without_changes(make_ctx):
    ctx = await make_ctx()
    await dispatch("/diff", ctx)
    assert "no changes this session" in _out(ctx)


async def test_compact_summarises_older_turns(make_ctx):
    ctx = await make_ctx(FakeProvider.text("SUMMARY of the work"))
    for i in range(6):
        _turn(ctx.session, f"t{i}", f"q{i}", f"a{i}")
    await dispatch("/compact", ctx)
    assert "compacted" in _out(ctx)
    assert "SUMMARY of the work" in ctx.session.messages()[0].text


async def test_compact_with_too_little_history_says_so(make_ctx):
    ctx = await make_ctx()
    _turn(ctx.session, "t1", "q", "a")
    await dispatch("/compact", ctx)
    assert "nothing to compact yet" in _out(ctx)


async def test_compact_failure_does_not_raise(make_ctx):
    ctx = await make_ctx([ValueError("boom")])
    for i in range(6):
        _turn(ctx.session, f"t{i}", f"q{i}", f"a{i}")
    res = await dispatch("/compact", ctx)
    assert res.handled is True and "/compact failed" in _out(ctx)


async def test_usage_reads_the_event_log(make_ctx):
    ctx = await make_ctx()
    _turn(ctx.session, "t1", "q", "a")
    _turn(ctx.session, "t2", "q", "a")
    await dispatch("/usage", ctx)
    assert "turns: 2  in: 10  out: 2  total: 12" in _out(ctx)


async def test_add_drop_context_handlers(make_ctx, tmp_path):
    (tmp_path / "p.py").write_text("P = 1\n")
    ctx = await make_ctx()
    await dispatch("/add p.py", ctx)
    assert ctx.state.pinned.paths == ["p.py"]
    await dispatch("/context", ctx)
    assert "p.py" in _out(ctx)
    await dispatch("/drop p.py", ctx)
    assert ctx.state.pinned.paths == []
    await dispatch("/add", ctx)  # no arg -> usage line, no crash
    assert "usage: /add" in _out(ctx)


async def test_plan_toggle_and_explicit_state(make_ctx):
    ctx = await make_ctx(plan_state=[False])
    await dispatch("/plan", ctx)
    assert ctx.plan_state[0] is True
    await dispatch("/plan off", ctx)
    assert ctx.plan_state[0] is False
    await dispatch("/plan on", ctx)
    assert ctx.plan_state[0] is True


async def test_init_becomes_a_prompt_with_a_reload_after(make_ctx):
    res = await dispatch("/init", await make_ctx())
    assert "AGENTS.md" in res.prompt and res.reload_after is True


async def test_dispatch_goes_through_the_shared_registry(make_ctx, monkeypatch):
    import luna.repl.commands as repl

    seen = []
    real = repl.run_line
    monkeypatch.setattr(repl, "run_line", lambda line, env: seen.append(line) or real(line, env))
    await dispatch("/tools", await make_ctx())
    assert seen == ["/tools"]


async def test_reload_rereads_custom_commands(make_ctx, tmp_path):
    ctx = await make_ctx(user_commands={})
    ctx.bundle = _Stub("first")
    (tmp_path / ".luna" / "commands").mkdir(parents=True)
    (tmp_path / ".luna" / "commands" / "greet.md").write_text("hi $ARGUMENTS\n")
    await dispatch("/reload", ctx)
    assert "greet" in ctx.user_commands
    assert (await dispatch("/greet you", ctx)).prompt == "hi you"


async def test_env_rebuild_on_the_loop_thread_raises_instead_of_deadlocking(make_ctx):
    import asyncio

    from luna.repl.commands import _ReplEnv

    env = _ReplEnv(await make_ctx(), asyncio.get_running_loop())
    with pytest.raises(RuntimeError, match="worker thread"):
        env.rebuild()
