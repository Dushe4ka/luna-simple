from luna.config.model_discovery import known_models


def test_known_models_returns_the_seeded_default_for_anthropic():
    assert known_models("anthropic") == ["claude-sonnet-4-5"]


def test_known_models_covers_every_built_in_provider():
    from luna.config.providers import PROVIDERS

    for key in PROVIDERS:
        assert known_models(key), f"{key} has no known_models.toml entry"


def test_known_models_returns_empty_list_for_an_unknown_provider():
    assert known_models("mylocal-custom-provider") == []


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, raise_error: bool = False):
        self.status_code = status_code
        self._json_body = json_body
        self._raise_error = raise_error

    def raise_for_status(self):
        if self._raise_error or self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("bad status", request=None, response=self)

    def json(self):
        if self._json_body is None:
            raise ValueError("no body")
        return self._json_body


def test_list_models_anthropic_success(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(
            200, {"data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-opus-4-1"}]}
        )

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    result = model_discovery.list_models("anthropic", PROVIDERS["anthropic"], api_key="sk-ant-test")
    assert result == ["claude-sonnet-4-5", "claude-opus-4-1"]
    assert captured["url"] == "https://api.anthropic.com/v1/models"
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"


def test_list_models_google_success_strips_models_prefix(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, {"models": [{"name": "models/gemini-2.5-pro"}]})

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    result = model_discovery.list_models("google", PROVIDERS["google"], api_key="goog-test")
    assert result == ["gemini-2.5-pro"]


def test_list_models_ollama_success_uses_default_host(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(
            200, {"models": [{"name": "llama3.2:latest"}, {"name": "qwen2.5-coder:latest"}]}
        )

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    result = model_discovery.list_models("ollama", PROVIDERS["ollama"], api_key=None)
    assert result == ["llama3.2:latest", "qwen2.5-coder:latest"]
    assert captured["url"] == "http://localhost:11434/api/tags"


def test_list_models_ollama_honors_custom_host(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(200, {"models": []})

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    monkeypatch.setenv("OLLAMA_HOST", "http://myhost:9999")
    model_discovery.list_models("ollama", PROVIDERS["ollama"], api_key=None)
    assert captured["url"] == "http://myhost:9999/api/tags"


def test_list_models_openai_compatible_success(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(200, {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4o"}]})

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    result = model_discovery.list_models("openai", PROVIDERS["openai"], api_key="sk-oa-test")
    assert result == ["gpt-4.1", "gpt-4o"]
    assert captured["url"] == "https://api.openai.com/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-oa-test"


def test_list_models_uses_spec_base_url_when_present(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(200, {"data": [{"id": "gpt-oss-120b"}]})

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    result = model_discovery.list_models("cerebras", PROVIDERS["cerebras"], api_key="csk-test")
    assert result == ["gpt-oss-120b"]
    assert captured["url"] == "https://api.cerebras.ai/v1/models"


def test_list_models_keyless_custom_provider_sends_no_auth_header(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import ProviderSpec

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse(200, {"data": [{"id": "local-model"}]})

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    spec = ProviderSpec(
        "mylocal", "openai", "local-model", None, "openai", "http://localhost:8000/v1"
    )
    result = model_discovery.list_models("mylocal", spec, api_key=None)
    assert result == ["local-model"]
    assert "Authorization" not in captured["headers"]


def test_list_models_returns_none_on_timeout(monkeypatch):
    import httpx

    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    def fake_get(url, headers=None, timeout=None):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    result = model_discovery.list_models("anthropic", PROVIDERS["anthropic"], api_key="sk-test")
    assert result is None


def test_list_models_returns_none_on_non_200(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(401, raise_error=True)

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    result = model_discovery.list_models("anthropic", PROVIDERS["anthropic"], api_key="bad-key")
    assert result is None


def test_list_models_returns_none_on_malformed_json(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, {"unexpected": "shape"})

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    result = model_discovery.list_models("anthropic", PROVIDERS["anthropic"], api_key="sk-test")
    assert result is None


def test_list_models_returns_none_when_no_key_and_key_required():
    from luna.config.model_discovery import list_models
    from luna.config.providers import PROVIDERS

    assert list_models("anthropic", PROVIDERS["anthropic"], api_key=None) is None
