import io
from pathlib import Path

import pytest
from luna_core.background import AgentManager
from luna_core.errors import ProviderError
from luna_core.messages import Message, Text, message_to_dict
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session
from luna_core.usage import Usage
from rich.console import Console

from luna.commands import run_line
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
    assert "turns: 2  in: 10  out: 2  cached: 0  total: 12" in _out(ctx)


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

    async def spy(line, env):
        seen.append(line)
        return await real(line, env)

    monkeypatch.setattr(repl, "run_line", spy)
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


# -- commands on the core (registry handlers, no REPL/server involved) ----------------------


class Env:
    """A bare :class:`luna.commands.CommandEnv` over a core session."""

    can_rebuild = False

    def __init__(self, tmp_path, bundle, session, config=None):
        self.workdir = str(tmp_path)
        self.session = session
        self.state = SessionState(session.id, str(tmp_path))
        self.pinned = self.state.pinned
        self.bundle = bundle
        self.agent = bundle.agent
        self.manager = AgentManager()
        self.user_commands = {}
        self.config = config or LunaConfig(workdir=str(tmp_path))

    def get_plan(self):
        return False


@pytest.fixture
async def env(tmp_path):
    log = EventLog(tmp_path / "s.db")
    session = Session.create(log, str(tmp_path), undo=False)
    script = [FakeProvider.text("SUMMARY"), FakeProvider.text("side")]
    bundle = await build_agent(
        LunaConfig(workdir=str(tmp_path), model="m"), provider=FakeProvider(script)
    )
    yield Env(tmp_path, bundle, session)
    await bundle.aclose()
    log.close()


async def test_undo_confirms_then_rewinds(env):
    _turn(env.session, "t1", "first", "a")
    _turn(env.session, "t2", "second", "b")
    asked = await run_line("/undo", env)
    assert asked.confirm and "second" in asked.confirm.question
    done = await run_line("/undo --yes", env)
    assert "rewound" in done.notices[0].text
    assert [m.text for m in env.session.messages()] == ["first", "a"]
    assert "restored" in (await run_line("/redo", env)).notices[0].text


async def test_rewind_n(env):
    for i in range(3):
        _turn(env.session, f"t{i}", f"q{i}", f"a{i}")
    asked = await run_line("/rewind 2", env)
    assert asked.confirm and asked.confirm.resubmit == "/rewind 2 --yes"
    await run_line("/rewind 2 --yes", env)
    assert [m.text for m in env.session.messages()] == ["q0", "a0"]


async def test_compact_btw_steer_fork_export(env, tmp_path):
    for i in range(6):
        _turn(env.session, f"t{i}", f"q{i}", f"a{i}")
    res = await run_line("/compact", env)
    assert "compacted" in res.notices[0].text and "SUMMARY" in env.session.messages()[0].text
    side = await run_line("/btw what is x?", env)
    assert side.text == "side"
    assert not any("what is x" in m.text for m in env.session.messages())
    await run_line("/steer check tests too", env)
    assert env.session.drain_queue() == ["check tests too"]
    fork = await run_line("/fork", env)
    assert fork.effects["session"] != env.session.id
    out = await run_line("/export md", env)
    text = (tmp_path / out.effects["path"]).read_text()
    assert text.startswith("# ") and "## You" in text


async def test_btw_turns_a_provider_error_into_a_readable_notice(tmp_path):
    log = EventLog(tmp_path / "s.db")
    session = Session.create(log, str(tmp_path), undo=False)
    err = ProviderError("bad key", kind="auth")
    bundle = await build_agent(
        LunaConfig(workdir=str(tmp_path), model="m"), provider=FakeProvider([[err]])
    )
    try:
        res = await run_line("/btw hi", Env(tmp_path, bundle, session))
    finally:
        await bundle.aclose()
    assert res.notices[0].level == "error" and "rejected the API key" in res.notices[0].text


async def test_agents_lists_subagents_and_runs(env):
    res = await run_line("/agents", env)
    assert "researcher" in res.text and "general-purpose" in res.text
    assert "(no background work)" in res.text


async def test_tools_lists_real_tools(env):
    res = await run_line("/tools", env)
    assert "write_todos" in res.text and "task_status" in res.text


async def test_usage_and_diff_on_the_core_session(env):
    assert (await run_line("/usage", env)).notices[0].text == "no usage recorded yet"
    _turn(env.session, "t1", "q", "a")
    assert "in: 5  out: 1" in (await run_line("/usage", env)).text
    assert (await run_line("/diff", env)).notices[0].text == "no changes this session"


async def test_unknown_command_without_a_user_command(env):
    res = await run_line("/nope", env)
    assert res.notices[0].level == "error" and "/nope" in res.notices[0].text


async def test_a_crashing_handler_becomes_an_error_notice(env, monkeypatch):
    monkeypatch.setattr(env.session, "fork", lambda: 1 / 0)
    res = await run_line("/fork", env)
    assert res.notices[0].level == "error" and "/fork failed" in res.notices[0].text


async def test_reload_refreshes_the_allow_rules(make_ctx, tmp_path):
    """A rule added to .luna/permissions.toml after the build takes effect after /reload."""
    cfg = LunaConfig(workdir=str(tmp_path), model="m")

    async def rebuild():
        return await build_agent(cfg, provider=FakeProvider([FakeProvider.text("ok")]))

    ctx = await make_ctx(config=cfg, rebuild=rebuild)
    args = {"file_path": "a.py", "content": "x"}

    def verdict():
        agent = ctx.bundle.agent
        return agent.policy.check(agent.tools["write_file"], args).action

    assert verdict() == "ask"
    (tmp_path / ".luna").mkdir(exist_ok=True)
    (tmp_path / ".luna" / "permissions.toml").write_text('allow = ["write_file:a.py"]\n')
    await dispatch("/reload", ctx)
    assert verdict() == "allow"
