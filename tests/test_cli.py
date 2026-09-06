import pytest

from luna.cli import build_parser, main


def test_parser_accepts_flags():
    ns = build_parser().parse_args(["do a thing", "--provider", "openai", "--yolo", "--no-splash"])
    assert ns.prompt_pos == "do a thing"
    assert ns.provider == "openai"
    assert ns.yolo is True
    assert ns.no_splash is True


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


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
