import io

import pytest
from rich.console import Console

from luna.config import model_discovery
from luna.config.config import config_path, load_config
from luna.config.credentials import credentials_path, get_api_key
from luna.repl.setup_wizard import run_setup


def _console():
    return Console(file=io.StringIO(), force_terminal=True)


def _scripted(*answers):
    it = iter(answers)
    return lambda _prompt: next(it)


@pytest.fixture(autouse=True)
def _no_live_model_discovery(monkeypatch):
    """Every test in this file exercises wizard FLOW, not live model
    discovery (that's tests/test_model_discovery.py's job, Task 2) —
    force the live path to always miss, so known_models.toml's real,
    small, fast, offline fallback list drives the picker deterministically
    with zero network access. A test that wants a specific model list can
    still monkeypatch model_discovery.known_models on top of this."""
    monkeypatch.setattr(model_discovery, "list_models", lambda *a, **k: None)


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
        input_fn=_scripted("anthropic", "n", ""),  # provider, decline replacement, default model
        getpass_fn=_scripted("should-not-be-used"),
    )
    assert get_api_key("anthropic") == "sk-ant-original"


def test_wizard_always_writes_model_name_even_when_blank():
    """Regression test for the original bug: a blank model answer must
    overwrite config.toml's model.name with the provider's default, not
    just skip writing it and leave a stale value in place.

    known_models is left real/unmocked on purpose: deepseek's
    known_models.toml entry (Task 1) is non-empty, so this exercises the
    picker's own numbered-list text fallback (not the "no models at all"
    branch), which is the realistic path most users hit."""
    from luna.config.config import set_config_values

    set_config_values({"model.name": "some-stale-garbage-value"})
    run_setup(
        _console(),
        input_fn=_scripted("deepseek", ""),  # provider, blank model (numbered-list fallback)
        getpass_fn=_scripted("dsk-key"),
    )
    cfg = load_config({})
    assert cfg.model == "deepseek-chat"  # PROVIDERS["deepseek"].default_model, not the stale value


def test_wizard_uses_model_picker_when_known_models_available(monkeypatch):
    """With no real terminal, arrow_pick returns None and choose_model
    falls back to its own numbered plain-text list — driven here by an
    explicit known_models override (rather than deepseek/openai's real
    single-entry list) so the test can exercise a multi-choice pick.
    Answering with the model's number must pick it and write it."""
    from luna.config import model_discovery

    monkeypatch.setattr(model_discovery, "known_models", lambda provider: ["model-a", "model-b"])
    run_setup(
        _console(),
        input_fn=_scripted("openai", "2"),  # provider, pick #2 from the numbered list
        getpass_fn=_scripted("sk-openai"),
    )
    assert load_config({}).model == "model-b"


def test_choose_model_falls_back_to_free_text_when_no_list_available(monkeypatch):
    """A provider with neither a live list nor a known_models.toml entry
    (or both fail) falls back to today's free-text prompt."""
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS
    from luna.repl.setup_wizard import choose_model

    monkeypatch.setattr(model_discovery, "known_models", lambda provider: [])
    result = choose_model(
        _console(),
        lambda _p: "custom-typed-model",
        "openai",
        PROVIDERS["openai"],
        api_key="sk-test",
    )
    assert result == "custom-typed-model"


def test_choose_model_manual_entry_sentinel_falls_through_to_free_text(monkeypatch):
    """The arrow-pick list ends with a "type it manually" sentinel entry —
    without it, a real interactive terminal (where arrow_pick never returns
    None) could never reach the free-text tier, which for a built-in
    provider with a single-entry known_models.toml list means no way at all
    to type a different model id. arrow_pick itself can't be driven into
    its questionary branch without a real tty, so it is patched to return
    the sentinel directly."""
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS
    from luna.repl import setup_wizard
    from luna.repl.setup_wizard import choose_model

    monkeypatch.setattr(model_discovery, "known_models", lambda provider: ["model-a", "model-b"])
    monkeypatch.setattr(setup_wizard, "arrow_pick", lambda *a, **k: "\x00__manual_entry__")
    result = choose_model(
        _console(),
        lambda _p: "my-custom-model",
        "openai",
        PROVIDERS["openai"],
        api_key="sk-test",
    )
    assert result == "my-custom-model"


def test_wizard_finds_an_existing_key_from_the_environment(monkeypatch):
    """A user with only an env-var key (no stored credentials.toml) must
    still have that key resolved for the live model-discovery attempt —
    previously run_setup only checked credentials.toml, so an env-only
    key was invisible to it even though the rest of the codebase
    (cli.py's _has_api_key, commands.py's _model) already checks the
    environment first."""
    from luna.config import model_discovery

    captured = {}

    def fake_list_models(provider, spec, *, api_key, **kwargs):
        captured["api_key"] = api_key
        return None

    monkeypatch.setattr(model_discovery, "list_models", fake_list_models)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    run_setup(
        _console(),
        # provider, decline replacing the (env-provided) key, blank model
        input_fn=_scripted("anthropic", "", ""),
        getpass_fn=_scripted("should-not-be-used"),
    )
    assert captured["api_key"] == "sk-from-env"


def test_choose_model_free_text_blank_returns_default(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS
    from luna.repl.setup_wizard import choose_model

    monkeypatch.setattr(model_discovery, "known_models", lambda provider: [])
    result = choose_model(
        _console(), lambda _p: "", "openai", PROVIDERS["openai"], api_key="sk-test"
    )
    assert result == PROVIDERS["openai"].default_model
