from luna.config.model_discovery import known_models


def test_known_models_returns_the_seeded_default_for_anthropic():
    assert known_models("anthropic") == ["claude-sonnet-4-5"]


def test_known_models_covers_every_built_in_provider():
    from luna.config.providers import PROVIDERS

    for key in PROVIDERS:
        assert known_models(key), f"{key} has no known_models.toml entry"


def test_known_models_returns_empty_list_for_an_unknown_provider():
    assert known_models("mylocal-custom-provider") == []
