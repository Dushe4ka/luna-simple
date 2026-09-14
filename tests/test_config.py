import pytest

from luna.config.config import load_config
from luna.config.providers import PROVIDERS, LunaConfigError, merge_providers


def test_defaults(tmp_path):
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    assert cfg.provider == "anthropic"
    assert cfg.model is None
    assert cfg.yolo is False
    assert cfg.show_splash is True


def test_cli_beats_env_beats_project_toml(tmp_path):
    (tmp_path / ".luna.toml").write_text(
        '[model]\nprovider = "openai"\nname = "gpt-4o"\n[ui]\nsplash = false\n'
    )
    env = {"LUNA_PROVIDER": "google"}
    cfg = load_config({"provider": "deepseek"}, env=env, cwd=str(tmp_path))
    assert cfg.provider == "deepseek"  # CLI wins
    assert cfg.model == "gpt-4o"  # from project toml, not overridden
    assert cfg.show_splash is False  # from [ui].splash


def test_env_beats_project_toml(tmp_path):
    (tmp_path / ".luna.toml").write_text('[model]\nprovider = "openai"\n')
    cfg = load_config({}, env={"LUNA_PROVIDER": "google"}, cwd=str(tmp_path))
    assert cfg.provider == "google"


def test_yolo_from_env_truthy(tmp_path):
    cfg = load_config({}, env={"LUNA_YOLO": "1"}, cwd=str(tmp_path))
    assert cfg.yolo is True


def test_model_kwargs_composed(tmp_path):
    cfg = load_config({"temperature": 0.2, "max_tokens": 1000}, env={}, cwd=str(tmp_path))
    assert cfg.model_kwargs == {"max_retries": 0, "temperature": 0.2, "max_tokens": 1000}


def test_model_kwargs_defaults_max_retries_to_zero(tmp_path):
    """The provider SDK's own retry silently restarts a whole streamed
    generation on a transient mid-stream error, duplicating already-shown
    content — Luna's own turn-level retry replaces it, so max_retries
    defaults to 0 unless the user sets their own in [agent.extra]."""
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    assert cfg.model_kwargs["max_retries"] == 0


def test_model_kwargs_lets_extra_override_max_retries(tmp_path):
    cfg = load_config({"extra_model_kwargs": {"max_retries": 3}}, env={}, cwd=str(tmp_path))
    assert cfg.model_kwargs["max_retries"] == 3


def test_verify_command_settable(tmp_path, isolated_config_home):
    from luna.config.config import load_config, set_config_values

    set_config_values({"agent.verify_command": "pytest -q"})
    assert load_config({}).verify_command == "pytest -q"


def test_fast_model_settable(isolated_config_home):
    from luna.config.config import load_config, set_config_values

    set_config_values({"model.fast": "anthropic:claude-haiku-4-5"})
    assert load_config({}).fast_model == "anthropic:claude-haiku-4-5"


def test_format_command_settable(isolated_config_home):
    from luna.config.config import load_config, set_config_values

    set_config_values({"agent.format_command": "ruff format"})
    assert load_config({}).format_command == "ruff format"


def test_pricing_table_is_loaded(isolated_config_home):
    from luna.config.config import config_dir, load_config

    (config_dir()).mkdir(parents=True, exist_ok=True)
    (config_dir() / "config.toml").write_text(
        '[model.pricing."my-model"]\ninput = 2.0\noutput = 6.0\nwindow = 128000\n'
    )
    cfg = load_config({})
    assert cfg.pricing.get("my-model") == {"input": 2.0, "output": 6.0, "window": 128000}


def test_user_config_is_lowest_layer(tmp_path, monkeypatch):
    cfg_home = tmp_path / "xdg"
    (cfg_home / "luna").mkdir(parents=True)
    (cfg_home / "luna" / "config.toml").write_text('[model]\nprovider = "openai"\n')
    (tmp_path / ".luna.toml").write_text('[model]\nprovider = "deepseek"\n')
    cfg = load_config({}, env={"XDG_CONFIG_HOME": str(cfg_home)}, cwd=str(tmp_path))
    assert cfg.provider == "deepseek"  # project toml overrides user toml


def test_custom_provider_parsed_from_project_toml(tmp_path):
    (tmp_path / ".luna.toml").write_text(
        "[provider.custom.mylocal]\n"
        'base_url = "http://localhost:8000/v1"\n'
        'env_var = "MYLOCAL_API_KEY"\n'
        'default_model = "local-model"\n'
    )
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    assert "mylocal" in cfg.custom_providers
    spec = cfg.custom_providers["mylocal"]
    assert spec.base_url == "http://localhost:8000/v1"
    assert spec.env_var == "MYLOCAL_API_KEY"
    assert spec.default_model == "local-model"
    assert spec.init_prefix == "openai"
    assert spec.pip_extra == "openai"


def test_custom_provider_default_model_is_optional(tmp_path):
    (tmp_path / ".luna.toml").write_text(
        "[provider.custom.mylocal]\n"
        'base_url = "http://localhost:8000/v1"\n'
        'env_var = "MYLOCAL_API_KEY"\n'
    )
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    # non-empty fallback, exact value not asserted here
    assert cfg.custom_providers["mylocal"].default_model


def test_custom_provider_missing_base_url_raises(tmp_path):
    (tmp_path / ".luna.toml").write_text('[provider.custom.mylocal]\nenv_var = "MYLOCAL_API_KEY"\n')
    with pytest.raises(LunaConfigError):
        load_config({}, env={}, cwd=str(tmp_path))


def test_custom_provider_env_var_is_optional_for_a_keyless_endpoint(tmp_path):
    # A local server (vLLM, LM Studio) needs no API key at all — same shape as
    # the built-in ``ollama`` provider, which carries ``env_var=None``.
    (tmp_path / ".luna.toml").write_text(
        '[provider.custom.mylocal]\nbase_url = "http://localhost:8000/v1"\n'
    )
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    spec = cfg.custom_providers["mylocal"]
    assert spec.env_var is None
    assert spec.base_url == "http://localhost:8000/v1"


def test_custom_provider_cannot_redirect_a_builtin_provider(tmp_path):
    # The security-relevant invariant of the whole custom-provider feature,
    # exercised end-to-end through .luna.toml parsing rather than against
    # merge_providers() alone.
    (tmp_path / ".luna.toml").write_text(
        '[provider.custom.anthropic]\nbase_url = "http://evil.example/v1"\nenv_var = "EVIL_KEY"\n'
    )
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    # load_config itself does not filter the colliding entry ...
    assert cfg.custom_providers["anthropic"].base_url == "http://evil.example/v1"
    # ... but the merged registry every call site actually uses does.
    resolved = merge_providers(cfg.custom_providers)["anthropic"]
    assert resolved is PROVIDERS["anthropic"]
    assert resolved.base_url is None


def test_no_custom_providers_section_gives_empty_dict(tmp_path):
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    assert cfg.custom_providers == {}


def test_set_config_values_still_validates_provider_against_builtin(isolated_config_home):
    from luna.config.config import set_config_values
    from luna.config.providers import LunaConfigError

    with pytest.raises(LunaConfigError):
        set_config_values({"model.provider": "not-a-real-provider"})
