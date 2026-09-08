from luna.config import load_config


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
    assert cfg.model_kwargs == {"temperature": 0.2, "max_tokens": 1000}


def test_verify_command_settable(tmp_path, isolated_config_home):
    from luna.config import load_config, set_config_values

    set_config_values({"agent.verify_command": "pytest -q"})
    assert load_config({}).verify_command == "pytest -q"


def test_fast_model_settable(isolated_config_home):
    from luna.config import load_config, set_config_values

    set_config_values({"model.fast": "anthropic:claude-haiku-4-5"})
    assert load_config({}).fast_model == "anthropic:claude-haiku-4-5"


def test_user_config_is_lowest_layer(tmp_path, monkeypatch):
    cfg_home = tmp_path / "xdg"
    (cfg_home / "luna").mkdir(parents=True)
    (cfg_home / "luna" / "config.toml").write_text('[model]\nprovider = "openai"\n')
    (tmp_path / ".luna.toml").write_text('[model]\nprovider = "deepseek"\n')
    cfg = load_config({}, env={"XDG_CONFIG_HOME": str(cfg_home)}, cwd=str(tmp_path))
    assert cfg.provider == "deepseek"  # project toml overrides user toml
