from luna.config.providers import PROVIDERS, ProviderSpec, merge_providers


def test_merge_providers_adds_a_new_key():
    custom = {
        "mylocal": ProviderSpec(
            "mylocal", "openai_chat", "local-model", "MYLOCAL_API_KEY", "http://localhost:8000/v1"
        )
    }
    merged = merge_providers(custom)
    assert merged["mylocal"].base_url == "http://localhost:8000/v1"
    assert merged["mylocal"].api == "openai_chat"
    assert merged["anthropic"] is PROVIDERS["anthropic"]


def test_merge_providers_builtin_wins_on_collision():
    fake_anthropic = ProviderSpec("anthropic", "openai_chat", "fake", "FAKE_KEY", "http://fake/v1")
    merged = merge_providers({"anthropic": fake_anthropic})
    assert merged["anthropic"] is PROVIDERS["anthropic"]
    assert merged["anthropic"].base_url is None
    assert merged["anthropic"].api == "anthropic"


def test_merge_providers_with_no_custom_entries_returns_builtin_only():
    assert merge_providers({}) == PROVIDERS
