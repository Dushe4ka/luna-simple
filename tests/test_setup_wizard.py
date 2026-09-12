import io

from rich.console import Console

from luna.config.config import config_path, load_config
from luna.config.credentials import credentials_path, get_api_key
from luna.repl.setup_wizard import run_setup


def _console():
    return Console(file=io.StringIO(), force_terminal=True)


def _scripted(*answers):
    it = iter(answers)
    return lambda _prompt: next(it)


def test_wizard_writes_config_and_key():
    code = run_setup(
        _console(),
        input_fn=_scripted("2", ""),  # provider #2 (deepseek), default model
        getpass_fn=_scripted("dsk-live-key"),
    )
    assert code == 0
    assert config_path().exists()
    cfg = load_config({})
    assert cfg.provider == "deepseek"
    assert get_api_key("deepseek") == "dsk-live-key"
    assert credentials_path().exists()


def test_wizard_accepts_provider_by_name_and_custom_model():
    run_setup(
        _console(),
        input_fn=_scripted("openai", "gpt-4o-mini"),
        getpass_fn=_scripted("sk-openai"),
    )
    cfg = load_config({})
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o-mini"


def test_wizard_skips_key_for_ollama():
    calls = []
    run_setup(
        _console(),
        input_fn=_scripted("ollama", ""),
        getpass_fn=lambda p: calls.append(p) or "",
    )
    assert calls == []  # never prompted for a key
    assert load_config({}).provider == "ollama"


def test_wizard_keeps_existing_key_when_declined():
    from luna.config.credentials import set_api_key

    set_api_key("anthropic", "sk-ant-original")
    run_setup(
        _console(),
        input_fn=_scripted("anthropic", "", "n"),  # decline replacement
        getpass_fn=_scripted("should-not-be-used"),
    )
    assert get_api_key("anthropic") == "sk-ant-original"
