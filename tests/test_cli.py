import pytest

from luna.cli import build_parser, main


def test_parser_accepts_flags():
    ns = build_parser().parse_args(["do a thing", "--provider", "openai", "--yolo", "--no-splash"])
    assert ns.prompt_pos == "do a thing"
    assert ns.provider == "openai"
    assert ns.yolo is True
    assert ns.no_splash is True


def test_parser_has_resume_flags():
    ns = build_parser().parse_args(["-c"])
    assert ns.cont is True
    ns2 = build_parser().parse_args(["--resume"])
    assert ns2.resume == "__list__"
    ns3 = build_parser().parse_args(["--resume", "abc123"])
    assert ns3.resume == "abc123"


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "0.4.0" in capsys.readouterr().out


def test_bad_provider_is_config_error(capsys):
    code = main(["hi", "--provider", "grok"])
    assert code == 2
    assert "grok" in capsys.readouterr().err


def test_provider_flag_accepts_a_provider_not_in_the_builtin_choices_list():
    """argparse itself must not reject an unknown-looking --provider value
    before Luna gets a chance to check it against a project's
    [provider.custom.*] — that check happens later, in load_config."""
    args = build_parser().parse_args(["--provider", "some-custom-name", "hello"])
    assert args.provider == "some-custom-name"


def test_has_api_key_checks_a_custom_provider_via_the_given_registry(monkeypatch):
    from luna.cli import _has_api_key
    from luna.config.providers import ProviderSpec

    monkeypatch.setenv("MYLOCAL_API_KEY", "sk-test")
    registry = {
        "mylocal": ProviderSpec(
            "mylocal", "openai_chat", "local-model", "MYLOCAL_API_KEY", "http://localhost:8000/v1"
        )
    }
    assert _has_api_key("mylocal", registry) is True


def test_unknown_provider_via_env_is_config_error(monkeypatch, capsys):
    monkeypatch.setenv("LUNA_PROVIDER", "grok")
    code = main(["hi", "--no-splash"])
    assert code == 2
    assert "grok" in capsys.readouterr().err


class _FakeBundle:
    agent = None
    closed = False

    async def aclose(self):
        _FakeBundle.closed = True


async def _fake_build(*a, **k):
    return _FakeBundle()


def _async(fn):
    async def run(*a, **k):
        return fn(*a, **k)

    return run


def test_one_shot_dispatches_run_once(monkeypatch, tmp_path):
    calls = {}
    _FakeBundle.closed = False
    monkeypatch.setattr("luna.core.agent.build_agent", _fake_build)
    monkeypatch.setattr(
        "luna.core.session.run_once", _async(lambda *a, **k: calls.setdefault("once", True) or "ok")
    )
    monkeypatch.setattr(
        "luna.core.session.run_repl", _async(lambda *a, **k: calls.setdefault("repl", True) or 0)
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    code = main(["fix the bug", "--no-splash", "--workdir", str(tmp_path)])
    assert code == 0
    assert calls == {"once": True}
    assert _FakeBundle.closed  # the bundle (MCP servers, sandbox) is closed on the way out


def test_no_prompt_dispatches_repl(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr("luna.core.agent.build_agent", _fake_build)
    monkeypatch.setattr(
        "luna.core.session.run_repl", _async(lambda *a, **k: calls.setdefault("repl", 0))
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    code = main(["--no-splash", "--workdir", str(tmp_path)])
    assert code == 0
    assert calls == {"repl": 0}


def test_mcp_add_from_registry_and_list(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    assert main(["mcp", "add", "filesystem"]) == 0
    assert main(["mcp", "list"]) == 0
    assert "filesystem" in capsys.readouterr().out


def test_mcp_add_explicit_command(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    assert main(["mcp", "add", "custom", "--", "node", "srv.js"]) == 0
    from luna.extensions.mcp import load_mcp_config

    assert load_mcp_config(".")["custom"]["command"] == "node"


def test_agents_list_shows_builtins(capsys):
    assert main(["agents", "list"]) == 0
    assert "researcher" in capsys.readouterr().out


def test_agents_list_reports_broken_subagents_toml(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "subagents.toml").write_text(
        '[subagent.x]\ndescription = "x"\ntools = ["execute", "read_file"]\n'
    )
    code = main(["agents", "list"])
    assert code == 2
    assert "unsafe = true" in capsys.readouterr().err


def test_skills_list_empty_ok(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    assert main(["skills", "list"]) == 0


def test_setup_subcommand_dispatches_wizard(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        "luna.repl.setup_wizard.run_setup", lambda *a, **k: calls.setdefault("setup", 0)
    )
    assert main(["setup"]) == 0
    assert calls == {"setup": 0}


def test_config_path_subcommand(capsys):
    assert main(["config", "path"]) == 0
    out = capsys.readouterr().out
    assert "credentials.toml" in out and "config.toml" in out


def test_config_set_key_and_show(capsys):
    assert main(["config", "set-key", "deepseek", "dsk-abcd1234"]) == 0
    assert "****1234" in capsys.readouterr().out
    assert main(["config", "show"]) == 0
    assert "key.deepseek" in capsys.readouterr().out


def test_config_set_value(capsys):
    assert main(["config", "set", "model.provider", "openai"]) == 0
    from luna.config.config import load_config

    assert load_config({}).provider == "openai"


def test_config_set_rejects_unknown_key():
    assert main(["config", "set", "bogus.key", "x"]) == 2


def test_missing_key_non_interactive_is_config_error(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = main(["hi", "--no-splash", "--no-input", "--workdir", str(tmp_path)])
    assert code == 2
    assert "luna setup" in capsys.readouterr().err


def test_json_output_is_one_parseable_object(tmp_path, monkeypatch, capsys):
    import json

    from luna_core.providers.fake import FakeProvider

    import luna.cli as cli
    from luna.core.agent import build_agent as real_build

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    async def build(config, **kw):
        return await real_build(config, provider=FakeProvider([FakeProvider.text("hi")]), **kw)

    monkeypatch.setattr("luna.core.agent.build_agent", build)
    rc = cli.main(["--json", "hello"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    data = json.loads(out.splitlines()[-1])
    assert data["text"] == "hi"
    assert data["session_id"]


def test_continue_reuses_the_latest_session_of_this_folder(tmp_path, monkeypatch):
    import luna.cli as cli
    from luna.core.store import open_log

    seen = {}

    async def fake_once(bundle, prompt, **kw):
        seen["session"] = kw["session"]
        return ""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("luna.core.agent.build_agent", _fake_build)
    monkeypatch.setattr("luna.core.session.run_once", fake_once)
    log = open_log()
    workdir = str(tmp_path.resolve())
    from luna_core.messages import Message, message_to_dict

    for sid, parent in (("s-old", None), ("s-new", None), ("s-child", "s-new")):
        log.create(workdir, title=sid, session_id=sid, parent_id=parent)
        log.append(sid, "user", {"message": message_to_dict(Message.user("hi"))})
    assert cli.main(["-c", "-p", "more", "--workdir", workdir, "--no-splash"]) == 0
    assert seen["session"].id == "s-new"  # the newest top-level one, never the child
    assert cli.main(["--resume", "nope", "-p", "x", "--workdir", workdir]) == 2


def test_missing_key_interactive_runs_wizard(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "luna.repl.setup_wizard.run_setup", lambda *a, **k: calls.setdefault("wiz", 0)
    )
    monkeypatch.setattr("luna.core.agent.build_agent", _fake_build)
    monkeypatch.setattr("luna.core.session.run_once", _async(lambda *a, **k: "ok"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("luna.ui.console.Console.is_terminal", property(lambda self: True))
    code = main(["hi", "--no-splash", "--workdir", str(tmp_path)])
    assert calls == {"wiz": 0}
    assert code == 0


def test_a_failed_one_shot_turn_exits_1(tmp_path, monkeypatch):
    from luna_core.errors import ProviderError
    from luna_core.providers.fake import FakeProvider

    import luna.cli as cli
    from luna.core.agent import build_agent as real_build

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    async def build(config, **kw):
        script = [[ProviderError("no route", kind="network")]]
        return await real_build(config, provider=FakeProvider(script), **kw)

    monkeypatch.setattr("luna.core.agent.build_agent", build)
    assert cli.main(["-p", "hi", "--workdir", str(tmp_path), "--no-splash"]) == 1


def test_continue_skips_empty_sessions(tmp_path, monkeypatch):
    from luna_core.messages import Message, message_to_dict
    from luna_core.session import Session

    import luna.cli as cli
    from luna.core.store import open_log

    seen = {}

    async def fake_once(bundle, prompt, **kw):
        seen["session"] = kw["session"]
        return ""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("luna.core.agent.build_agent", _fake_build)
    monkeypatch.setattr("luna.core.session.run_once", fake_once)
    log = open_log()
    workdir = str(tmp_path.resolve())
    real = Session.create(log, workdir, title="real work")
    real.append("user", {"message": message_to_dict(Message.user("hi"))})
    Session.create(log, workdir)  # an empty REPL start, newer
    assert [r.thread_id for r in cli._LogIndex(log).list(workdir)] == [real.id]
    assert cli.main(["-c", "-p", "more", "--workdir", workdir, "--no-splash"]) == 0
    assert seen["session"].id == real.id


def _log_session(log, workdir, title, *, parent_id=None, meta=None, updated=None):
    from luna_core.messages import Message, message_to_dict

    sid = log.create(str(workdir), title=title, parent_id=parent_id, meta=meta)
    log.append(sid, "user", {"message": message_to_dict(Message.user(title))})
    if updated is not None:
        log._conn.execute("UPDATE sessions SET updated = ? WHERE id = ?", (updated, sid))
    return sid


def test_sessions_list_shows_conversations_and_forks_but_not_children(
    tmp_path, capsys, monkeypatch
):
    from luna.core.store import open_log

    monkeypatch.chdir(tmp_path)
    log = open_log()
    top = _log_session(log, tmp_path.resolve(), "keep me")
    fork = _log_session(log, tmp_path.resolve(), "a fork", parent_id=top, meta={"kind": "fork"})
    child = _log_session(log, tmp_path.resolve(), "sub run", parent_id=top)
    assert main(["sessions"]) == 0
    out = capsys.readouterr().out
    assert top in out and fork in out and child not in out


def test_sessions_rm_removes_the_conversation_and_its_children_but_not_forks(
    tmp_path, capsys, monkeypatch
):
    from luna.core.store import open_log

    monkeypatch.chdir(tmp_path)
    log = open_log()
    wd = tmp_path.resolve()
    top = _log_session(log, wd, "top")
    child = _log_session(log, wd, "child", parent_id=top)
    fork = _log_session(log, wd, "fork", parent_id=top, meta={"kind": "fork"})
    assert main(["sessions", "rm", child]) == 2  # a child is removed with its parent
    assert "delete that session instead" in capsys.readouterr().err
    assert main(["sessions", "rm", top]) == 0
    assert log.info(top) is None and log.info(child) is None
    assert log.info(fork) is not None
    assert main(["sessions", "rm", "nope"]) == 2


def test_sessions_rm_goes_through_a_live_server_and_respects_busy(tmp_path, monkeypatch, capsys):
    from luna_core.client import ServerError

    from luna.core.store import open_log

    monkeypatch.chdir(tmp_path)
    log = open_log()
    sid = _log_session(log, tmp_path.resolve(), "busy one")
    monkeypatch.setattr("luna.cli._live_server", lambda: {"port": 1, "token": "t"})

    class Busy:
        def __init__(self, *a, **k):
            pass

        async def agents(self):
            return {"runs": [], "tasks": []}

        async def delete_session(self, session_id):
            raise ServerError("HTTP 409: session_busy", 409)

        async def aclose(self):
            pass

    monkeypatch.setattr("luna_core.client.LunaClient", Busy)
    assert main(["sessions", "rm", sid]) == 1
    assert "running" in capsys.readouterr().err and log.info(sid) is not None


def test_sessions_prune_removes_old_conversations_and_their_children(tmp_path, capsys, monkeypatch):
    from luna.core.store import open_log

    monkeypatch.chdir(tmp_path)
    log = open_log()
    wd = tmp_path.resolve()
    fresh = _log_session(log, wd, "fresh")
    old = _log_session(log, wd, "old one", updated=0)
    old_child = _log_session(log, wd, "its child", parent_id=old, updated=0)
    assert main(["sessions", "prune", "--days", "30"]) == 0
    out = capsys.readouterr().out
    assert old in out and "pruned 1 session(s)" in out
    assert log.info(old) is None and log.info(old_child) is None and log.info(fresh) is not None


def test_sessions_prune_skips_running_sessions(tmp_path, capsys, monkeypatch):
    from luna.core.store import open_log

    monkeypatch.chdir(tmp_path)
    log = open_log()
    running = _log_session(log, tmp_path.resolve(), "running", updated=0)
    monkeypatch.setattr("luna.cli._running_ids", lambda workdir: {running})
    assert main(["sessions", "prune", "--days", "1"]) == 0
    assert log.info(running) is not None and "pruned 0 session(s)" in capsys.readouterr().out


def test_run_background_starts_a_server_run(monkeypatch, capsys, tmp_path):
    calls = {}

    class FakeClient:
        def __init__(self, base_url, token):
            calls["base"] = base_url

        async def create_session(self, workdir):
            return "sid123"

        async def background(self, session_id, text, workdir):
            calls["bg"] = (session_id, text)
            return {"status": "running"}

        async def aclose(self):
            pass

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("luna.server.run.ensure_running", lambda *a, **k: {"port": 1, "token": "t"})
    monkeypatch.setattr("luna_core.client.LunaClient", FakeClient)
    monkeypatch.setattr("luna.cli._trusted", lambda workdir: True)
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--background", "index the repo"]) == 0
    assert calls["bg"] == ("sid123", "index the repo")
    assert "luna --resume sid123" in capsys.readouterr().out


def test_run_background_refuses_an_untrusted_folder(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--background", "x"]) == 1
    assert "not trusted" in capsys.readouterr().err


def test_agents_lists_server_runs(monkeypatch, capsys):
    monkeypatch.setattr(
        "luna.cli._server_runs",
        lambda: [{"id": "r1", "label": "index", "status": "running", "cost": 0.5}],
    )
    assert main(["agents"]) == 0
    out = capsys.readouterr().out
    assert "background runs:" in out and "r1 · index · running · $0.5000" in out


def test_acp_without_the_extra_says_how_to_install(monkeypatch, capsys):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "luna_core.acp":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert main(["acp"]) == 2
    assert "luna-simple[acp]" in capsys.readouterr().err


def test_p_without_a_key_is_a_readable_error_not_a_traceback(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["-p", "hi", "--workdir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("luna:") and "Traceback" not in err


@pytest.mark.parametrize("days", ["-1", "0", "nan", "inf", "-inf", "abc"])
def test_sessions_prune_rejects_a_non_positive_or_non_finite_days(
    days, tmp_path, monkeypatch, capsys
):
    from luna.core.store import open_log

    monkeypatch.chdir(tmp_path)
    log = open_log()
    sid = _log_session(log, tmp_path.resolve(), "precious")
    with pytest.raises(SystemExit) as exc:
        main(["sessions", "prune", "--days", days])
    assert exc.value.code == 2 and "--days" in capsys.readouterr().err
    assert log.info(sid) is not None


def test_server_error_carries_the_http_status():
    import httpx
    from luna_core.client import ServerError, _error

    resp = httpx.Response(409, json={"error": "session_busy"})
    assert _error(resp).status == 409 and ServerError("x").status is None


def test_sessions_rm_refuses_when_a_descendant_runs_on_the_live_server(
    tmp_path, monkeypatch, capsys
):
    from luna.core.store import open_log

    monkeypatch.chdir(tmp_path)
    log = open_log()
    top = _log_session(log, tmp_path.resolve(), "top")
    child = _log_session(log, tmp_path.resolve(), "child", parent_id=top)
    monkeypatch.setattr("luna.cli._live_server", lambda: {"port": 1, "token": "t"})
    deleted = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def agents(self):
            return {"runs": [], "tasks": [{"session_id": child, "status": "running"}]}

        async def delete_session(self, session_id):
            deleted.append(session_id)

        async def aclose(self):
            pass

    monkeypatch.setattr("luna_core.client.LunaClient", Client)
    assert main(["sessions", "rm", top]) == 1
    assert deleted == [] and log.info(top) is not None and log.info(child) is not None


def test_run_background_without_a_key_is_a_readable_error(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("luna.cli._trusted", lambda workdir: True)
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--background", "x"]) == 2
    err = capsys.readouterr().err
    assert "No API key" in err and "Traceback" not in err


def test_run_background_reports_a_server_that_cannot_start(monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise OSError("no port")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("luna.server.run.ensure_running", boom)
    monkeypatch.setattr("luna.cli._trusted", lambda workdir: True)
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--background", "x"]) == 1
    assert "cannot start the local server" in capsys.readouterr().err


def test_acp_on_eof_exits_cleanly_without_output(tmp_path):
    import subprocess
    import sys

    pytest.importorskip("acp")
    result = subprocess.run(
        [sys.executable, "-m", "luna", "acp"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**__import__("os").environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")},
    )
    assert result.returncode == 0 and result.stdout == "" and "Traceback" not in result.stderr
