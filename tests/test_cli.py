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
    assert "0.2.1" in capsys.readouterr().out


def test_bad_provider_is_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        main(["hi", "--provider", "grok"])
    assert e.value.code == 2  # argparse rejects the choice


def test_unknown_provider_via_env_is_config_error(monkeypatch, capsys):
    monkeypatch.setenv("LUNA_PROVIDER", "grok")
    code = main(["hi", "--no-splash"])
    assert code == 2
    assert "grok" in capsys.readouterr().err


def test_one_shot_dispatches_run_once(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr("luna.cli.build_agent", lambda *a, **k: object())
    monkeypatch.setattr("luna.cli.run_once", lambda *a, **k: calls.setdefault("once", True) or "ok")
    monkeypatch.setattr("luna.cli.run_repl", lambda *a, **k: calls.setdefault("repl", True) or 0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    code = main(["fix the bug", "--no-splash", "--workdir", str(tmp_path)])
    assert code == 0
    assert calls == {"once": True}


def test_no_prompt_dispatches_repl(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr("luna.cli.build_agent", lambda *a, **k: object())
    monkeypatch.setattr("luna.cli.run_repl", lambda *a, **k: calls.setdefault("repl", 0))
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
    from luna.mcp import load_mcp_config

    assert load_mcp_config(".")["custom"]["command"] == "node"


def test_agents_list_shows_builtins(capsys):
    assert main(["agents", "list"]) == 0
    assert "researcher" in capsys.readouterr().out


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
    from luna.config import load_config

    assert load_config({}).provider == "openai"


def test_config_set_rejects_unknown_key():
    assert main(["config", "set", "bogus.key", "x"]) == 2


def test_missing_key_non_interactive_is_config_error(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = main(["hi", "--no-splash", "--no-input", "--workdir", str(tmp_path)])
    assert code == 2
    assert "luna setup" in capsys.readouterr().err


def test_missing_key_interactive_runs_wizard(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("luna.cli.run_setup", lambda *a, **k: calls.setdefault("wiz", 0))
    monkeypatch.setattr("luna.cli.build_agent", lambda *a, **k: object())
    monkeypatch.setattr("luna.cli.run_once", lambda *a, **k: "ok")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("luna.ui.console.Console.is_terminal", property(lambda self: True))
    code = main(["hi", "--no-splash", "--workdir", str(tmp_path)])
    assert calls == {"wiz": 0}
    assert code == 0
