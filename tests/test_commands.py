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


async def test_export_rejects_unknown_formats_and_extensions(env):
    for line in ("/export txt", "/export pdf", "/export out.txt"):
        res = await run_line(line, env)
        assert res.notices[0].level == "error", line


async def test_export_infers_the_format_from_the_extension(env, tmp_path):
    _turn(env.session, "t1", "q", "a")
    res = await run_line("/export notes.html", env)
    assert (tmp_path / "notes.html").read_text().startswith("<!doctype html>")
    assert res.effects["path"] == "notes.html"
    bad = await run_line("/export md notes.html", env)
    assert bad.notices[0].level == "error" and "does not match" in bad.notices[0].text


async def test_export_never_overwrites_without_force(env, tmp_path):
    (tmp_path / "app.py").write_text("keep\n")
    res = await run_line("/export app.md", env)  # fresh file is fine
    assert res.notices[0].level == "info"
    clash = await run_line("/export app.md", env)
    assert clash.notices[0].level == "error" and "--force" in clash.notices[0].text
    assert (await run_line("/export app.md --force", env)).notices[0].level == "info"
    (tmp_path / "src.md").write_text("keep\n")
    assert (await run_line("/export src.md", env)).notices[0].level == "error"
    assert (tmp_path / "src.md").read_text() == "keep\n"


async def test_export_stays_inside_the_workspace_and_handles_spaces(env, tmp_path):
    outside = await run_line("/export ../escape.md", env)
    assert outside.notices[0].level == "error" and not (tmp_path.parent / "escape.md").exists()
    spaced = await run_line('/export "my notes.md"', env)
    assert (tmp_path / "my notes.md").exists() and spaced.effects["path"] == "my notes.md"


async def test_rewind_validates_and_clamps(env):
    for i in range(2):
        _turn(env.session, f"t{i}", f"q{i}", f"a{i}")
    for line in ("/rewind 0", "/rewind -1", "/rewind abc"):
        res = await run_line(line, env)
        assert res.notices[0].level == "error", line
    done = await run_line("/rewind 5 --yes", env)
    assert "rewound 2 turn(s)" in done.notices[0].text and env.session.messages() == []


async def test_steer_when_idle_says_next_turn(env):
    res = await run_line("/steer more", env)
    assert "next turn" in res.notices[0].text


async def test_undo_redo_rewind_refused_while_background_tasks_run(make_ctx):
    import asyncio

    ctx = await make_ctx(input_fn=None)
    _turn(ctx.session, "t1", "first", "a")
    background = ctx.bundle.agent.services["background"]
    gate = asyncio.Event()
    handle = background.start("slow", gate.wait, session=ctx.session)
    await asyncio.sleep(0)
    try:
        for line in ("/undo", "/rewind 1", "/redo"):
            await dispatch(line, ctx)
    finally:
        gate.set()
        await background.wait(handle)
    assert [m.text for m in ctx.session.messages()] == ["first", "a"]
    assert _out(ctx).count("background tasks are still running") == 3


# --- /provider: every provider listed, a missing key is collected -----------


def _clear_provider_keys(monkeypatch):
    from luna.config.providers import PROVIDERS

    for spec in PROVIDERS.values():
        if spec.env_var:
            monkeypatch.delenv(spec.env_var, raising=False)


async def test_provider_lists_every_provider_ready_ones_first(make_ctx, tmp_path, monkeypatch):
    from luna.config.providers import PROVIDERS
    from luna.repl.commands import _ReplEnv

    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    ctx = await make_ctx(config=LunaConfig(workdir=str(tmp_path), provider="deepseek"))
    result = await run_line("/provider", _ReplEnv(ctx))
    options = result.choice.options
    assert sorted(v for v, _ in options) == sorted(PROVIDERS)
    assert options[0] == ("deepseek", "deepseek  ✓ текущий")
    assert options[1] == ("ollama", "ollama")
    labels = dict(options)
    assert labels["anthropic"] == "anthropic  · нужен ключ"
    assert labels["openrouter"] == "openrouter  · нужен ключ"
    # registry order is kept inside each group
    rest = [v for v, _ in options[2:]]
    assert rest == [k for k in PROVIDERS if k not in ("deepseek", "ollama")]
    assert result.choice.resubmit == "/provider {value}"


async def test_provider_list_includes_custom_providers(make_ctx, tmp_path, monkeypatch):
    from luna.config.providers import ProviderSpec
    from luna.repl.commands import _ReplEnv

    _clear_provider_keys(monkeypatch)
    monkeypatch.delenv("MYLOCAL_API_KEY", raising=False)
    custom = {
        "mylocal": ProviderSpec("mylocal", "openai_chat", "m", "MYLOCAL_API_KEY", "http://x/v1")
    }
    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), provider="ollama", custom_providers=custom)
    )
    result = await run_line("/provider", _ReplEnv(ctx))
    assert ("mylocal", "mylocal  · нужен ключ") in result.choice.options


async def test_provider_without_key_asks_the_client_for_one(make_ctx, tmp_path, monkeypatch):
    from luna.repl.commands import _ReplEnv

    _clear_provider_keys(monkeypatch)
    ctx = await make_ctx(config=LunaConfig(workdir=str(tmp_path), provider="ollama"))
    result = await run_line("/provider anthropic", _ReplEnv(ctx))
    need = result.effects["need_key"]
    assert need["provider"] == "anthropic"
    assert need["env_var"] == "ANTHROPIC_API_KEY"
    assert "console.anthropic.com" in need["hint"]
    assert need["path"].endswith("credentials.toml")
    assert ctx.config.provider == "ollama"
    assert "provider" not in result.effects
    assert result.to_json()["effects"]["need_key"] == need


async def test_repl_collects_a_missing_key_hidden_and_switches(make_ctx, tmp_path, monkeypatch):
    import os
    import stat

    from luna.config.credentials import credentials_path, get_api_key

    _clear_provider_keys(monkeypatch)
    prompts = []

    def secret(prompt):
        prompts.append(prompt)
        return "  sk-ant-SECRET-123  "

    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), provider="ollama"),
        input_fn=lambda p: pytest.fail("the key must not be read with a visible prompt"),
        secret_fn=secret,
    )
    await dispatch("/provider anthropic", ctx)
    assert get_api_key("anthropic") == "sk-ant-SECRET-123"
    assert stat.S_IMODE(credentials_path().stat().st_mode) == 0o600
    assert ctx.config.provider == "anthropic"
    assert "ANTHROPIC_API_KEY" in prompts[0]
    assert "sk-ant-SECRET-123" not in _out(ctx)
    assert "sk-ant-SECRET-123" not in os.environ.values()


@pytest.mark.parametrize("answer", [KeyboardInterrupt, EOFError, ""])
async def test_repl_key_prompt_cancel_or_empty_keeps_everything(
    make_ctx, tmp_path, monkeypatch, answer
):
    from luna.config.credentials import credentials_path

    _clear_provider_keys(monkeypatch)

    def secret(prompt):
        if isinstance(answer, str):
            return answer
        raise answer

    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), provider="ollama"),
        input_fn=lambda p: "",
        secret_fn=secret,
    )
    await dispatch("/provider anthropic", ctx)
    assert not credentials_path().exists()
    assert ctx.config.provider == "ollama"
    assert "отмен" in _out(ctx) or "пуст" in _out(ctx)


async def test_repl_refuses_a_malformed_key_without_echoing_it(make_ctx, tmp_path, monkeypatch):
    from luna.config.credentials import credentials_path

    _clear_provider_keys(monkeypatch)
    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), provider="ollama"),
        input_fn=lambda p: "",
        secret_fn=lambda p: "sk bad key",
    )
    await dispatch("/provider anthropic", ctx)
    assert not credentials_path().exists()
    assert ctx.config.provider == "ollama"
    assert "printable ASCII" in _out(ctx)
    assert "sk bad key" not in _out(ctx)


async def test_repl_notice_says_type_it_below(make_ctx, tmp_path, monkeypatch):
    _clear_provider_keys(monkeypatch)
    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), provider="ollama"),
        input_fn=lambda p: "",
        secret_fn=lambda p: "",
    )
    await dispatch("/provider anthropic", ctx)
    out = _out(ctx)
    assert "нужен ключ для anthropic — введите его ниже (или: luna config set-key anthropic)" in out
    assert "нет ключа" not in out


async def test_one_shot_only_suggests_set_key(make_ctx, tmp_path, monkeypatch):
    _clear_provider_keys(monkeypatch)
    ctx = await make_ctx(config=LunaConfig(workdir=str(tmp_path), provider="ollama"))
    await dispatch("/provider anthropic", ctx)
    out = _out(ctx)
    assert "нужен ключ для anthropic — luna config set-key anthropic" in out
    assert "введите" not in out


async def test_repl_without_a_tty_refuses_instead_of_echoing(make_ctx, tmp_path, monkeypatch):
    import getpass
    import sys

    from luna.config.credentials import credentials_path

    _clear_provider_keys(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda p: pytest.fail("getpass would echo"))
    ctx = await make_ctx(
        config=LunaConfig(workdir=str(tmp_path), provider="ollama"), input_fn=lambda p: ""
    )
    await dispatch("/provider anthropic", ctx)
    assert not credentials_path().exists()
    assert ctx.config.provider == "ollama"
    assert "скрытый ввод недоступен" in _out(ctx)


async def test_project_provider_is_labelled_and_needs_its_own_key(make_ctx, tmp_path, monkeypatch):
    from luna.config.config import load_config
    from luna.repl.commands import _ReplEnv

    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-real")
    (tmp_path / ".luna.toml").write_text(
        '[model]\nprovider = "ollama"\n'
        '[provider.custom.myproxy]\nbase_url = "https://api.example.com/v1"\n'
        'env_var = "GITHUB_TOKEN"\n'
        '[provider.custom.broken]\nenv_var = "X"\n'
    )
    cfg = load_config({}, cwd=str(tmp_path))
    ctx = await make_ctx(config=cfg)
    result = await run_line("/provider", _ReplEnv(ctx))
    labels = dict(result.choice.options)
    assert labels["myproxy"] == "myproxy  · из проекта (api.example.com) · нужен ключ"
    assert any(n.level == "warn" and "broken" in n.text for n in result.notices)
    need = (await run_line("/provider myproxy", _ReplEnv(ctx))).effects["need_key"]
    assert need["provider"] == "myproxy" and need["env_var"] is None
    assert ctx.config.provider == "ollama"


async def test_need_key_for_a_project_provider_names_its_host(make_ctx, tmp_path, monkeypatch):
    from luna.config.config import load_config
    from luna.repl.commands import _ReplEnv

    _clear_provider_keys(monkeypatch)
    (tmp_path / ".luna.toml").write_text(
        '[model]\nprovider = "ollama"\n'
        '[provider.custom.litellm]\nbase_url = "https://evil.example/v1"\nenv_var = "K"\n'
    )
    ctx = await make_ctx(config=load_config({}, cwd=str(tmp_path)))
    need = (await run_line("/provider litellm", _ReplEnv(ctx))).effects["need_key"]
    assert need["hint"] == "ключ для evil.example"


@pytest.fixture
def restore_theme():
    from luna.ui import console as console_mod
    from luna.ui.themes import active

    before = active()
    yield
    console_mod.use_theme(before)


async def test_repl_theme_picks_with_the_arrow_picker_and_applies(
    make_ctx, monkeypatch, restore_theme
):
    from luna.config.config import user_setting
    from luna.ui.themes import active

    calls = []

    def fake_pick(**kw):
        calls.append(kw)
        return "daymoon"

    monkeypatch.setattr("luna.ui.theme_picker.pick_theme", fake_pick)
    monkeypatch.setattr("luna.repl.commands._real_terminal", lambda *a: True)
    ctx = await make_ctx(input_fn=lambda _p: "")
    await dispatch("/theme", ctx)
    assert calls == [{"current": "auto", "dark_terminal": True, "escape_value": "auto"}]
    assert user_setting("ui.theme") == "daymoon"
    assert ctx.config.theme == "daymoon"
    assert active().id == "daymoon"
    assert "Дневная луна" in ctx.console.file.getvalue()


async def test_repl_theme_esc_keeps_the_current_theme(make_ctx, monkeypatch, restore_theme):
    from luna.config.config import user_setting

    monkeypatch.setattr("luna.ui.theme_picker.pick_theme", lambda **kw: kw["current"])
    monkeypatch.setattr("luna.repl.commands._real_terminal", lambda *a: True)
    ctx = await make_ctx(input_fn=lambda _p: "")
    await dispatch("/theme", ctx)
    assert user_setting("ui.theme") is None
    assert ctx.config.theme == "auto"


async def test_repl_theme_without_a_terminal_only_names_the_theme(make_ctx, restore_theme):
    ctx = await make_ctx(input_fn=None)
    await dispatch("/theme", ctx)  # the conftest guard fails if the picker opens
    assert "тема: Авто" in ctx.console.file.getvalue()  # the title, not the raw id


async def test_repl_theme_on_a_pipe_does_not_open_the_picker(make_ctx, restore_theme):
    # `printf '/theme\nfix bug\n' | luna`: input_fn is the real input(), but
    # stdin is a pipe — the picker would read (and eat) the next line.
    ctx = await make_ctx(input_fn=input, console=Console(file=io.StringIO(), width=200))
    await dispatch("/theme", ctx)  # the conftest guard fails if the picker opens
    out = ctx.console.file.getvalue()
    assert "тема: Авто" in out
    assert "сменить: /theme <id>" in out
    assert "moonlight" in out and "daymoon-cb" in out


async def test_repl_theme_with_an_id_applies_it(make_ctx, restore_theme):
    from luna.ui.themes import active

    ctx = await make_ctx(input_fn=None)
    await dispatch("/theme daymoon-cb", ctx)
    assert active().id == "daymoon-cb"
