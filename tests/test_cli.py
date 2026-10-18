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
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert "0.3.0" in capsys.readouterr().out


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
    monkeypatch.setattr("luna.cli.build_agent", _fake_build)
    monkeypatch.setattr(
        "luna.cli.run_once", _async(lambda *a, **k: calls.setdefault("once", True) or "ok")
    )
    monkeypatch.setattr(
        "luna.cli.run_repl", _async(lambda *a, **k: calls.setdefault("repl", True) or 0)
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    code = main(["fix the bug", "--no-splash", "--workdir", str(tmp_path)])
    assert code == 0
    assert calls == {"once": True}
    assert _FakeBundle.closed  # the bundle (MCP servers, sandbox) is closed on the way out


def test_no_prompt_dispatches_repl(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr("luna.cli.build_agent", _fake_build)
    monkeypatch.setattr("luna.cli.run_repl", _async(lambda *a, **k: calls.setdefault("repl", 0)))
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
    monkeypatch.setattr("luna.cli.run_setup", lambda *a, **k: calls.setdefault("setup", 0))
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

    monkeypatch.setattr(cli, "build_agent", build)
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
    monkeypatch.setattr(cli, "build_agent", _fake_build)
    monkeypatch.setattr(cli, "run_once", fake_once)
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
    monkeypatch.setattr("luna.cli.run_setup", lambda *a, **k: calls.setdefault("wiz", 0))
    monkeypatch.setattr("luna.cli.build_agent", _fake_build)
    monkeypatch.setattr("luna.cli.run_once", _async(lambda *a, **k: "ok"))
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

    monkeypatch.setattr(cli, "build_agent", build)
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
    monkeypatch.setattr(cli, "build_agent", _fake_build)
    monkeypatch.setattr(cli, "run_once", fake_once)
    log = open_log()
    workdir = str(tmp_path.resolve())
    real = Session.create(log, workdir, title="real work")
    real.append("user", {"message": message_to_dict(Message.user("hi"))})
    Session.create(log, workdir)  # an empty REPL start, newer
    assert [r.thread_id for r in cli._LogIndex(log).list(workdir)] == [real.id]
    assert cli.main(["-c", "-p", "more", "--workdir", workdir, "--no-splash"]) == 0
    assert seen["session"].id == real.id
