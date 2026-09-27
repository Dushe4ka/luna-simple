import httpx
import pytest

from luna.extensions import websearch


def _response(url: str, method: str, json_body: dict) -> httpx.Response:
    """A real ``httpx.Response`` with a request attached.

    Constructing one bare (``httpx.Response(200, json=...)``, no
    ``request=``) makes ``.raise_for_status()`` raise its own internal
    ``RuntimeError`` ("Cannot call raise_for_status as the request instance
    has not been set") — caught by ``web_search``'s own per-backend
    ``try/except`` and silently treated as "this backend failed", which
    made every early version of these tests fall through to a REAL
    DuckDuckGo network call instead of ever exercising the mock.
    """
    return httpx.Response(200, json=json_body, request=httpx.Request(method, url))


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch):
    """Every test starts with a clean slate — no provider picked by accident."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)


def test_uses_tavily_when_its_key_is_set(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def fake_post(url, *, headers, json, timeout):
        assert url == "https://api.tavily.com/search"
        assert headers["Authorization"] == "Bearer tvly-test"
        assert json["query"] == "langchain 1.4 release notes"
        hit = {"title": "LangChain 1.4", "url": "https://x.test/a", "content": "notes"}
        return _response(url, "POST", {"results": [hit]})

    monkeypatch.setattr(websearch.httpx, "post", fake_post)
    result = websearch.web_search.invoke(
        {"query": "langchain 1.4 release notes", "max_results": 5}
    )
    assert "via tavily" in result
    assert "LangChain 1.4" in result
    assert "https://x.test/a" in result


def test_falls_back_to_brave_when_no_tavily_key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test")

    def fake_get(url, *, headers, params, timeout):
        assert url == "https://api.search.brave.com/res/v1/web/search"
        assert headers["X-Subscription-Token"] == "brave-test"
        assert params["q"] == "hi"
        hit = {"title": "Hi", "url": "https://x.test/b", "description": "d"}
        return _response(url, "GET", {"web": {"results": [hit]}})

    monkeypatch.setattr(websearch.httpx, "get", fake_get)
    result = websearch.web_search.invoke({"query": "hi", "max_results": 5})
    assert "via brave" in result
    assert "https://x.test/b" in result


def test_falls_back_to_ddgs_when_no_keys_configured(monkeypatch):
    class _FakeDDGS:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results):
            assert query == "hi"
            return [{"title": "Hi", "href": "https://x.test/c", "body": "d"}]

    import ddgs

    monkeypatch.setattr(ddgs, "DDGS", _FakeDDGS)
    result = websearch.web_search.invoke({"query": "hi", "max_results": 5})
    assert "via duckduckgo" in result
    assert "https://x.test/c" in result


def test_no_backend_available_gives_an_actionable_message(monkeypatch):
    """No keys, and ddgs "not installed" — must not crash, must say what to do."""
    monkeypatch.setattr(websearch, "_search_ddgs", lambda query, max_results: None)
    result = websearch.web_search.invoke({"query": "hi", "max_results": 5})
    assert "TAVILY_API_KEY" in result
    assert "BRAVE_API_KEY" in result
    assert "ddgs" in result


def test_a_failing_backend_does_not_block_the_next_one(monkeypatch):
    """Regression target: Tavily configured but erroring (bad key, rate limit,
    network blip) must not sink the whole search — it should fall through to
    the next backend rather than surfacing a raw exception.
    """
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-bad")

    def broken_post(*a, **k):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(websearch.httpx, "post", broken_post)
    monkeypatch.setattr(
        websearch,
        "_search_brave",
        lambda query, max_results: [{"title": "OK", "url": "https://x.test/d", "snippet": "d"}],
    )
    result = websearch.web_search.invoke({"query": "hi", "max_results": 5})
    assert "via brave" in result


def test_max_results_is_clamped_to_a_sane_range(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    seen: dict = {}

    def fake_post(url, *, headers, json, timeout):
        seen["max_results"] = json["max_results"]
        return _response(url, "POST", {"results": []})

    monkeypatch.setattr(websearch.httpx, "post", fake_post)
    websearch.web_search.invoke({"query": "hi", "max_results": 999})
    assert seen["max_results"] == 10


def test_no_results_is_reported_plainly_not_as_an_error(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(
        websearch.httpx,
        "post",
        lambda url, **k: _response(url, "POST", {"results": []}),
    )
    result = websearch.web_search.invoke({"query": "asdkfjhasdkfjh", "max_results": 5})
    assert "No results" in result
