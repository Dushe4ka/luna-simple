import pytest
from luna_core import catalog
from luna_core.errors import ProviderError
from luna_core.usage import Usage, cost


def test_usage_adds_and_totals():
    a = Usage(input=100, output=20, cache_read=50)
    b = Usage(input=10, output=5, reasoning=3)
    s = a + b
    assert s == Usage(input=110, output=25, cache_read=50, reasoning=3)
    assert s.total == 135
    assert Usage.from_dict(s.to_dict()) == s


def test_lookup_prefers_the_longest_matching_key():
    info = catalog.lookup("claude-sonnet-4-5-20250929")
    assert info.key == "claude-sonnet-4"
    assert info.context_window == 200_000
    assert info.vision is True


def test_lookup_unknown_model_gets_safe_defaults():
    info = catalog.lookup("totally-new-model")
    assert info.context_window == 128_000
    assert info.input_price is None


def test_overrides_win_over_packaged_entries():
    info = catalog.lookup(
        "deepseek-v4-flash", {"deepseek-v4": {"window": 1_000_000, "input": 0.14, "output": 0.28}}
    )
    assert (info.context_window, info.input_price, info.output_price) == (1_000_000, 0.14, 0.28)


def test_cost_counts_cached_input_at_the_cache_price():
    info = catalog.ModelInfo("m", 100_000, None, 1.0, 2.0, 0.1, 1.25, False, False)
    usage = Usage(input=1_000_000, output=1_000_000, cache_read=500_000)
    # 0.5M fresh * 1.0 + 0.5M cached * 0.1 + 1M out * 2.0
    assert cost(usage, info) == pytest.approx(0.5 + 0.05 + 2.0)


def test_cost_unknown_price_is_none():
    assert cost(Usage(input=10), catalog.lookup("totally-new-model")) is None


def test_provider_error_kinds():
    err = ProviderError("slow down", kind="rate_limit", status=429, retry_after=2.0)
    assert err.retryable is True
    assert ProviderError("bad key", kind="auth", status=401).retryable is False
    with pytest.raises(ValueError):
        ProviderError("x", kind="weird")
