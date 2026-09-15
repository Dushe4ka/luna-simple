# Luna Model Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace free-typed model names in `luna setup` and `/model` with
an arrow-key pick-list, populated live from each provider's own API when
possible and from a small built-in offline list otherwise — and, in the
same rewrite, fix the bug where leaving the model field blank silently
keeps a stale (possibly garbage) `model.name` instead of resetting it to
the provider's default.

**Architecture:** A new `luna/config/model_discovery.py` module exposes
`list_models()` (live, provider-specific HTTP, never raises — returns
`None` on any failure) and `known_models()` (reads the new
`luna/config/known_models.toml` static fallback). `luna/repl/setup_wizard.py`
reorders its steps to ask for the API key before the model (the live path
needs it), and its new `choose_model()` tries live → known → free text,
using the same `arrow_pick` UI `_choose_provider()` already uses. Every
remaining text-input path treats a blank answer as an explicit request
for the provider's default, which is now always written to
`config.toml`, overwriting any previous value. `luna/repl/commands.py`'s
`/model` (no argument) gets the same three-tier picker using the
session's already-active provider and key.

**Tech Stack:** Python 3.11+, `httpx` (already an unconditional
transitive dependency via the `anthropic` SDK, itself in Luna's base
`dependencies` — no new dependency), `tomllib` (stdlib, same pattern
`luna/config/usage.py` already uses for `models.toml`), `pytest` (no
network calls, ever — matches every existing test in this project).

**Spec:** `docs/superpowers/specs/2026-09-15-luna-model-picker-design.md`

## Global Constraints

- Python 3.11+, PEP 8 / PEP 257, clean `ruff check` / `ruff format --check`.
- No `deepagents`/`langgraph` imports added anywhere by this plan.
- Tests never touch the network. The HTTP call is isolated behind one
  function (`_safe_get_json`) so every test monkeypatches
  `luna.config.model_discovery.httpx.get` with a fake response object,
  never a real request.
- `list_models()` never raises under any circumstances (timeout,
  connection error, non-2xx status, malformed/unexpected JSON shape all
  collapse to `None`) — matches `luna/config/usage.py`'s existing
  best-effort convention verbatim.
- A live network request must give up after 3 seconds
  (`timeout: float = 3.0` default) so a closed-network environment never
  hangs the wizard.
- Every endpoint URL, header, and response shape below was verified
  against each provider's current docs (not guessed) on 2026-09-15;
  Fireworks is a deliberate, documented exception — see Task 2.

---

### Task 1: `known_models.toml` + `known_models()` loader

**Files:**
- Create: `luna/config/known_models.toml`
- Create: `luna/config/model_discovery.py` (only the loader half this task — `known_models()`, `_load_known_models()`)
- Test: `tests/test_model_discovery.py` (new)

**Interfaces:**
- Produces: `luna.config.model_discovery.known_models(provider: str) -> list[str]` — Task 2 and Task 3 both call this.

Seed values are copied verbatim from each provider's `default_model` in
`luna/config/providers.py` (already-verified code, not new research) —
a minimal-but-always-non-empty v1 fallback per built-in provider. No
entry for custom providers; `known_models()` returning `[]` for an
unknown key is the documented, correct behavior for those.

- [ ] **Step 1: Create `luna/config/known_models.toml`**

```toml
[anthropic]
models = ["claude-sonnet-4-5"]

[deepseek]
models = ["deepseek-chat"]

[openai]
models = ["gpt-4.1"]

[google]
models = ["gemini-2.5-pro"]

[ollama]
models = ["qwen2.5-coder"]

[mistral]
models = ["mistral-large-latest"]

[xai]
models = ["grok-4"]

[groq]
models = ["openai/gpt-oss-120b"]

[fireworks]
models = ["accounts/fireworks/models/qwen3p5-397b-a17b"]

[together]
models = ["Qwen/Qwen2.5-Coder-32B-Instruct"]

[openrouter]
models = ["anthropic/claude-sonnet-4-6"]

[perplexity]
models = ["sonar"]

[cerebras]
models = ["gpt-oss-120b"]
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_model_discovery.py`:

```python
from luna.config.model_discovery import known_models


def test_known_models_returns_the_seeded_default_for_anthropic():
    assert known_models("anthropic") == ["claude-sonnet-4-5"]


def test_known_models_covers_every_built_in_provider():
    from luna.config.providers import PROVIDERS

    for key in PROVIDERS:
        assert known_models(key), f"{key} has no known_models.toml entry"


def test_known_models_returns_empty_list_for_an_unknown_provider():
    assert known_models("mylocal-custom-provider") == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_model_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna.config.model_discovery'`.

- [ ] **Step 4: Create `luna/config/model_discovery.py` (loader half only)**

```python
"""Live + offline model-id discovery per provider (best-effort, never raises).

``list_models`` tries a provider's own API; ``known_models`` reads the
packaged offline fallback (``known_models.toml``, same loading pattern as
``luna/config/usage.py``'s ``models.toml``). Neither ever raises — a
closed-network environment or an unsupported provider must never hang or
crash the caller, only fall back.
"""

from __future__ import annotations

import tomllib
from importlib import resources

_KNOWN_MODELS_CACHE: dict | None = None


def _load_known_models() -> dict:
    """Read the packaged known-models registry once, caching the result."""
    global _KNOWN_MODELS_CACHE
    if _KNOWN_MODELS_CACHE is not None:
        return _KNOWN_MODELS_CACHE
    try:
        text = resources.files("luna.config").joinpath("known_models.toml").read_text()
        _KNOWN_MODELS_CACHE = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError, ModuleNotFoundError):
        _KNOWN_MODELS_CACHE = {}
    return _KNOWN_MODELS_CACHE


def known_models(provider: str) -> list[str]:
    """Static fallback model ids for ``provider``, or ``[]`` if none are known."""
    entry = _load_known_models().get(provider)
    if not isinstance(entry, dict):
        return []
    models = entry.get("models")
    return list(models) if isinstance(models, list) else []
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_model_discovery.py -v`
Expected: all 3 pass.

- [ ] **Step 6: Ensure the new TOML file is packaged**

Check `pyproject.toml`'s `[tool.hatch.build.targets.wheel]` section — it
currently reads `packages = ["luna"]`, which packages the whole `luna/`
directory tree including data files like `models.toml` (confirmed by the
fact `models.toml` already ships this way, per `usage.py`'s identical
`resources.files("luna.config").joinpath(...)` pattern). No change
needed here — `known_models.toml` is picked up the same way. Just note
this in your task report; do not edit `pyproject.toml`.

- [ ] **Step 7: Lint**

Run: `uv run ruff check luna/config/model_discovery.py tests/test_model_discovery.py && uv run ruff format --check luna/config/model_discovery.py tests/test_model_discovery.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add luna/config/known_models.toml luna/config/model_discovery.py tests/test_model_discovery.py
git commit -m "feat: known_models.toml offline fallback + known_models() loader"
```

---

### Task 2: `list_models()` — live discovery, 4 provider shapes

**Files:**
- Modify: `luna/config/model_discovery.py` (add `list_models`, `_safe_get_json`, the base-URL table)
- Modify: `tests/test_model_discovery.py`

**Interfaces:**
- Consumes: `ProviderSpec` (existing, `luna/config/providers.py` — fields used: `.base_url`, `.env_var`).
- Produces: `luna.config.model_discovery.list_models(provider: str, spec: ProviderSpec, *, api_key: str | None, timeout: float = 3.0) -> list[str] | None`. Task 3 and Task 4 both call this.

Every URL/header/response-shape below was verified against each
provider's current docs on 2026-09-15 — not guessed:

| Case | Request | Response shape |
|---|---|---|
| Anthropic | `GET https://api.anthropic.com/v1/models`, headers `x-api-key: <key>`, `anthropic-version: 2023-06-01` | `{"data": [{"id": "..."}], "has_more": ...}` |
| Google | `GET https://generativelanguage.googleapis.com/v1beta/models`, header `x-goog-api-key: <key>` | `{"models": [{"name": "models/gemini-..."}]}` — names carry a `models/` prefix that must be stripped |
| Ollama | `GET {OLLAMA_HOST env var, default http://localhost:11434}/api/tags`, no auth | `{"models": [{"name": "llama3.2:latest"}]}` |
| Everyone else | `GET {base}/models`, header `Authorization: Bearer <key>` if a key is given, no auth header otherwise (keyless local custom endpoints) | `{"data": [{"id": "..."}]}` |

`base` for "everyone else" is `spec.base_url` when set (Cerebras, any
`[provider.custom.*]` entry), otherwise looked up in a small constant
table for the providers that reach their model via a native LangChain
integration rather than the `base_url` mechanism:

```python
_OPENAI_COMPATIBLE_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "together": "https://api.together.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
    "perplexity": "https://api.perplexity.ai/v1",
}
```

**Documented exception — Fireworks:** Fireworks' actual list-models
endpoint is `https://api.fireworks.ai/v1/accounts/{account_id}/models`,
which needs an account id Luna has no way to know (it's not the API key
and Luna doesn't collect it anywhere). This plan does **not** add
account-id collection just for this — that's new scope for a cosmetic
convenience. Fireworks uses the same generic `GET {base}/models` call as
everyone else in this table; in practice that call will not resolve to
the real per-account listing endpoint and `list_models` will return
`None` for Fireworks, falling back to `known_models("fireworks")`
(Task 1's seeded single entry) exactly like any other provider whose
live call fails. This is a deliberate, accepted limitation — write it as
a code comment next to the constant, not a silent gap.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_model_discovery.py`:

```python
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
        return _FakeResponse(200, {"data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-opus-4-1"}]})

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
        return _FakeResponse(200, {"models": [{"name": "llama3.2:latest"}, {"name": "qwen2.5-coder:latest"}]})

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
    spec = ProviderSpec("mylocal", "openai", "local-model", None, "openai", "http://localhost:8000/v1")
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
    assert model_discovery.list_models("anthropic", PROVIDERS["anthropic"], api_key="sk-test") is None


def test_list_models_returns_none_on_non_200(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(401, raise_error=True)

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    assert model_discovery.list_models("anthropic", PROVIDERS["anthropic"], api_key="bad-key") is None


def test_list_models_returns_none_on_malformed_json(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, {"unexpected": "shape"})

    monkeypatch.setattr(model_discovery.httpx, "get", fake_get)
    assert model_discovery.list_models("anthropic", PROVIDERS["anthropic"], api_key="sk-test") is None


def test_list_models_returns_none_when_no_key_and_key_required():
    from luna.config.providers import PROVIDERS

    from luna.config.model_discovery import list_models

    assert list_models("anthropic", PROVIDERS["anthropic"], api_key=None) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_model_discovery.py -v -k "list_models"`
Expected: FAIL — `AttributeError`/`ImportError: cannot import name 'list_models'`.

- [ ] **Step 3: Implement `list_models` and `_safe_get_json`**

Add to `luna/config/model_discovery.py` (append after the existing
`known_models`/loader code from Task 1; add `import httpx` and
`import os` to the top of the file alongside the existing `tomllib`/
`importlib.resources` imports):

```python
import os

import httpx

from luna.config.providers import ProviderSpec

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"
_GOOGLE_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"

# Base URLs for providers reached via their own native LangChain
# integration rather than Luna's base_url passthrough mechanism (which
# already carries a base_url on their ProviderSpec, e.g. Cerebras and
# any [provider.custom.*] entry). Verified against each provider's docs
# on 2026-09-15.
#
# Fireworks is a documented exception: its real list-models endpoint is
# https://api.fireworks.ai/v1/accounts/{account_id}/models, which needs
# an account id Luna never collects. The generic GET {base}/models call
# below will not resolve to that endpoint, so list_models() returns None
# for Fireworks and callers fall back to known_models("fireworks") —
# accepted, not a bug.
_OPENAI_COMPATIBLE_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "together": "https://api.together.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
    "perplexity": "https://api.perplexity.ai/v1",
}


def _safe_get_json(url: str, headers: dict, *, timeout: float):
    """GET url, return parsed JSON, or None on any failure. Never raises."""
    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _ids_from(items, key: str) -> list[str]:
    return [item[key] for item in items if isinstance(item, dict) and key in item]


def list_models(
    provider: str,
    spec: ProviderSpec,
    *,
    api_key: str | None,
    timeout: float = 3.0,
) -> list[str] | None:
    """Live model ids for ``provider``, or ``None`` on any failure.

    Never raises. ``api_key`` may be ``None`` only when ``spec.env_var``
    is also ``None`` (Ollama, or a keyless custom endpoint) — such a
    provider is queried with no auth header.
    """
    if provider == "anthropic":
        if not api_key:
            return None
        data = _safe_get_json(
            _ANTHROPIC_MODELS_URL,
            {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION},
            timeout=timeout,
        )
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            return None
        return _ids_from(data["data"], "id")

    if provider == "google":
        if not api_key:
            return None
        data = _safe_get_json(_GOOGLE_MODELS_URL, {"x-goog-api-key": api_key}, timeout=timeout)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            return None
        names = _ids_from(data["models"], "name")
        return [n.removeprefix("models/") for n in names]

    if provider == "ollama":
        host = os.environ.get("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST).rstrip("/")
        data = _safe_get_json(f"{host}/api/tags", {}, timeout=timeout)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            return None
        return _ids_from(data["models"], "name")

    base = spec.base_url or _OPENAI_COMPATIBLE_BASE_URLS.get(provider)
    if base is None:
        return None
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = _safe_get_json(f"{base.rstrip('/')}/models", headers, timeout=timeout)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    return _ids_from(data["data"], "id")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_model_discovery.py -v`
Expected: all pass (11 from this task + 3 from Task 1 = 14 — run the file and confirm the actual count rather than trusting this arithmetic).

- [ ] **Step 5: Lint**

Run: `uv run ruff check luna/config/model_discovery.py tests/test_model_discovery.py && uv run ruff format --check luna/config/model_discovery.py tests/test_model_discovery.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add luna/config/model_discovery.py tests/test_model_discovery.py
git commit -m "feat: list_models() — live model discovery across 4 provider shapes"
```

---

### Task 3: `setup_wizard.py` — reorder steps, `choose_model()`, fix the blank-model-reset bug

**Files:**
- Modify: `luna/repl/setup_wizard.py` (whole file — `run_setup`, new `choose_model`)
- Modify: `tests/test_setup_wizard.py`

**Interfaces:**
- Consumes: `luna.config.model_discovery.{list_models, known_models}` (Tasks 1-2), `luna.ui.interact.arrow_pick` (existing).
- Produces: `choose_model(console, input_fn, provider, spec, *, api_key: str | None) -> str` — a module-level (not underscore-prefixed) function because Task 4 (`commands.py`) imports and calls it directly for `/model`'s no-argument picker, rather than duplicating the live→known→text fallback logic.

**Pre-existing tests this task will change (found by reading
`tests/test_setup_wizard.py` before writing anything new — read it
yourself too before starting, it's short):**

`run_setup`'s step order changes from provider → model → key to
provider → key → model. This does not break
`test_wizard_writes_config_and_key`, `test_wizard_accepts_provider_by_name_and_custom_model`,
or `test_wizard_skips_key_for_ollama` — in each, the key step either
never touches `input_fn` (no existing key, or `env_var is None`), so the
`input_fn`/`getpass_fn` answer sequences still line up in the new order.
It DOES break `test_wizard_keeps_existing_key_when_declined`: today's
`input_fn=_scripted("anthropic", "", "n")` answers provider, then model
(blank), then the decline-replace confirm ("n") — in the new order the
confirm comes before the model question, so the sequence must become
`_scripted("anthropic", "n", "")`. Fix this test's script in Step 1
below; do not leave the old sequence in place expecting it to still
pass.

- [ ] **Step 1: Write the failing tests**

**Critical, easy to miss:** once `run_setup` is rewritten (Step 3 below),
every wizard test unconditionally reaches `choose_model` → `list_models`
for whatever provider it picks. None of the pre-existing tests in this
file mock `model_discovery.list_models` today because that call doesn't
exist yet — left alone, every one of them would start making a real
network request the moment Step 3 lands, breaking this project's
"tests never touch the network" rule. Fix this once, for the whole file,
with an autouse fixture — add it at the top of `tests/test_setup_wizard.py`,
right after the existing `_scripted` helper:

```python
import pytest

from luna.config import model_discovery


@pytest.fixture(autouse=True)
def _no_live_model_discovery(monkeypatch):
    """Every test in this file exercises wizard FLOW, not live model
    discovery (that's tests/test_model_discovery.py's job, Task 2) —
    force the live path to always miss, so known_models.toml's real,
    small, fast, offline fallback list drives the picker deterministically
    with zero network access. A test that wants a specific model list can
    still monkeypatch model_discovery.known_models on top of this."""
    monkeypatch.setattr(model_discovery, "list_models", lambda *a, **k: None)
```

Then replace `test_wizard_keeps_existing_key_when_declined` with:

```python
def test_wizard_keeps_existing_key_when_declined():
    from luna.config.credentials import set_api_key

    set_api_key("anthropic", "sk-ant-original")
    run_setup(
        _console(),
        input_fn=_scripted("anthropic", "n", ""),  # provider, decline replacement, default model
        getpass_fn=_scripted("should-not-be-used"),
    )
    assert get_api_key("anthropic") == "sk-ant-original"
```

Add these new tests to the same file (the autouse fixture above already
covers `list_models` for all of them — no per-test mock needed for it):

```python
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
    result = choose_model(_console(), lambda _p: "custom-typed-model", "openai", PROVIDERS["openai"], api_key="sk-test")
    assert result == "custom-typed-model"


def test_choose_model_free_text_blank_returns_default(monkeypatch):
    from luna.config import model_discovery
    from luna.config.providers import PROVIDERS
    from luna.repl.setup_wizard import choose_model

    monkeypatch.setattr(model_discovery, "known_models", lambda provider: [])
    result = choose_model(_console(), lambda _p: "", "openai", PROVIDERS["openai"], api_key="sk-test")
    assert result == PROVIDERS["openai"].default_model
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_setup_wizard.py -v`
Expected: FAIL — `test_wizard_keeps_existing_key_when_declined` fails against the *old* code with the *new* script (proves the reorder is really needed); the four new tests fail with `ImportError`/`AttributeError` (`choose_model` doesn't exist yet) or wrong values (stale `model.name` not overwritten).

- [ ] **Step 3: Rewrite `luna/repl/setup_wizard.py`**

Replace the whole file:

```python
"""Interactive ``luna setup`` wizard: pick a provider, model, and API key.

Only lists built-in providers (``luna.config.providers.PROVIDERS``) — a
user-defined ``[provider.custom.<name>]`` (see ``config.py``) is configured
by hand in ``.luna.toml``/``config.toml``, not offered here, since the
wizard has no way to prompt for an arbitrary provider's base_url/env_var
pair sensibly.
"""

from __future__ import annotations

import getpass
from collections.abc import Callable, Mapping

from rich.console import Console

from luna.config import model_discovery
from luna.config.config import set_config_values
from luna.config.credentials import get_api_key, mask_key, set_api_key
from luna.config.providers import DEFAULT_PROVIDER, PROVIDERS, ProviderSpec
from luna.ui.interact import arrow_confirm, arrow_pick
from luna.ui.theme import PALETTE


def _choose_provider(console: Console, input_fn: Callable[[str], str]) -> str:
    keys = list(PROVIDERS)
    default_idx = keys.index(DEFAULT_PROVIDER) + 1
    options: list[tuple[str, str]] = []
    for key in keys:
        spec = PROVIDERS[key]
        note = "local, no key" if spec.env_var is None else spec.env_var
        options.append((key, f"{key}  ({note})"))

    picked = arrow_pick(console, input_fn, options, default=DEFAULT_PROVIDER)
    if picked is not None:
        return picked

    console.print(f"[{PALETTE['peri']}]Choose a provider:[/]")
    for i, key in enumerate(keys, 1):
        spec = PROVIDERS[key]
        note = "local, no key" if spec.env_var is None else spec.env_var
        console.print(f"  {i}. {key}  [dim {PALETTE['blue']}]({note})[/]")

    while True:
        raw = input_fn(f"provider [{default_idx}]: ").strip().lower()
        if not raw:
            return DEFAULT_PROVIDER
        if raw in PROVIDERS:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        console.print(f"[{PALETTE['mauve']}]pick 1-{len(keys)} or a provider name[/]")


def choose_model(
    console: Console,
    input_fn: Callable[[str], str],
    provider: str,
    spec: ProviderSpec,
    *,
    api_key: str | None,
) -> str:
    """Pick a model id: live list, then known_models.toml, then free text.

    A blank free-text answer always returns ``spec.default_model`` —
    callers must write that return value unconditionally, overwriting
    any previously-stored ``model.name`` (this is the fix for the bug
    where a stale/garbage model name could never be reset via the
    wizard).
    """
    models = model_discovery.list_models(provider, spec, api_key=api_key)
    if models is None:
        models = model_discovery.known_models(provider)

    if not models:
        model = input_fn(f"model [{spec.default_model}]: ").strip()
        return model or spec.default_model

    default = spec.default_model if spec.default_model in models else models[0]
    options = [(m, m) for m in models]
    picked = arrow_pick(console, input_fn, options, default=default)
    if picked is not None:
        return picked

    console.print(f"[{PALETTE['peri']}]Choose a model:[/]")
    for i, m in enumerate(models, 1):
        console.print(f"  {i}. {m}")
    default_idx = models.index(default) + 1
    while True:
        raw = input_fn(f"model [{default_idx}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(models):
            return models[int(raw) - 1]
        if raw in models:
            return raw
        console.print(f"[{PALETTE['mauve']}]pick 1-{len(models)} or a model name[/]")


def run_setup(
    console: Console,
    *,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run the wizard. Returns a process exit code."""
    console.print(f"[bold {PALETTE['accent']}]Luna setup[/]\n")

    provider = _choose_provider(console, input_fn)
    spec = PROVIDERS[provider]

    resolved_key: str | None = None
    creds_file = None
    if spec.env_var is None:
        console.print(
            f"\n[{PALETTE['blue']}]{provider} runs locally - no API key needed."
            f" Set OLLAMA_HOST if it is not on the default port.[/]"
        )
    else:
        existing = get_api_key(provider, env=env)
        resolved_key = existing
        prompt = "replace stored key" if existing else "paste your API key"
        if existing:
            console.print(f"\n[{PALETTE['blue']}]a key is already stored ({mask_key(existing)})[/]")
            picked = arrow_confirm(console, input_fn, "replace it?", default=False)
            if picked is None:
                picked = input_fn("replace it? [y/N]: ").strip().lower() in ("y", "yes")
            if not picked:
                prompt = None
        if prompt is not None:
            key = getpass_fn(f"{spec.env_var} ({prompt}): ").strip()
            if key:
                creds_file = set_api_key(provider, key, env=env)
                resolved_key = key
            else:
                console.print(
                    f"[{PALETTE['mauve']}]no key entered - set one later with"
                    f" 'luna config set-key {provider}'[/]"
                )

    model = choose_model(console, input_fn, provider, spec, api_key=resolved_key)

    config_file = set_config_values({"model.provider": provider, "model.name": model}, env=env)

    console.print("\n[green]saved.[/]")
    console.print(f"  config:      {config_file}")
    if creds_file:
        console.print(f"  credentials: {creds_file}  [dim](mode 0600)[/]")
    console.print(
        f"\n[{PALETTE['peri']}]Run [bold]luna[/bold] to start, or"
        f' [bold]luna "your task"[/bold] for one-shot.[/]'
    )
    return 0
```

Note what changed versus the original: `model.name` is now included in
the `set_config_values` call unconditionally (was: only when the typed
value differed from the default) — this is the actual bug fix, applied
uniformly whether the value came from the picker or the free-text
fallback.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_setup_wizard.py -v`
Expected: all pass, including every pre-existing test in the file.

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -q`
Expected: all pass — this is where a stray caller elsewhere expecting
the old `choose_model`-less `run_setup` internals would surface.

- [ ] **Step 6: Lint**

Run: `uv run ruff check luna/repl/setup_wizard.py tests/test_setup_wizard.py && uv run ruff format --check luna/repl/setup_wizard.py tests/test_setup_wizard.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add luna/repl/setup_wizard.py tests/test_setup_wizard.py
git commit -m "fix: luna setup asks for the API key before the model, offers a picker, and always overwrites model.name"
```

---

### Task 4: `/model` (REPL, no argument) gets the same picker

**Files:**
- Modify: `luna/repl/commands.py` (`_model` function, currently ~line 231)
- Modify: `tests/test_commands.py`

**Interfaces:**
- Consumes: `luna.config.model_discovery.{list_models, known_models}` (Tasks 1-2), `luna.ui.interact.arrow_pick` (existing, already imported in this file).
- Produces: no new public interface — `/model` with no argument now runs a picker instead of only printing; `/model <name>` is unchanged.

`commands.py` already imports `os`, `get_api_key`, `merge_providers`,
`LunaConfigError`, `arrow_confirm`, `arrow_pick` — no new imports needed
beyond `model_discovery`.

- [ ] **Step 1: Read `_model`'s current code and the file's `_ctx` test helper**

Already read while writing this plan — `_model` (around line 231 of
`luna/repl/commands.py`) currently:

```python
def _model(ctx: CommandContext, arg: str) -> DispatchResult | None:
    if not arg:
        ctx.console.print(f"model: {ctx.config.model or '(provider default)'}")
        return None
    previous_model = ctx.config.model
    ctx.config.model = arg
    try:
        new_agent = ctx.rebuild()
    except Exception as exc:  # noqa: BLE001 - a bad model must not kill the REPL
        ctx.config.model = previous_model
        ctx.console.print(f"[{PALETTE['mauve']}]could not switch: {exc}[/]")
        return None
    ctx.console.print(f"[{PALETTE['blue']}]model → {arg}[/]")
    return DispatchResult(agent=new_agent)
```

`tests/test_commands.py`'s `_ctx(**kw)` helper (already read for the
provider-expansion plan) builds a `CommandContext` with keyword
overrides on top of these defaults: `console`, `config=LunaConfig()`,
`agent=object()`, `rebuild=lambda: "rebuilt"`, `thread_id="t"`,
`workdir="."`, `index=None` — `input_fn` is NOT in that base dict, so a
test that needs one must pass it explicitly via `_ctx(input_fn=...)`.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_commands.py`:

```python
def test_model_with_no_arg_and_no_input_fn_just_prints(monkeypatch):
    """No input_fn available (e.g. non-interactive) — keep today's
    read-only behavior rather than trying to prompt."""
    ctx = _ctx(config=LunaConfig(provider="anthropic", model="claude-sonnet-4-5"), input_fn=None)
    res = dispatch("/model", ctx)
    assert res.handled is True
    assert "claude-sonnet-4-5" in ctx.console.file.getvalue()


def test_model_with_no_arg_uses_the_picker_and_rebuilds(monkeypatch):
    from luna.config import model_discovery

    monkeypatch.setattr(model_discovery, "list_models", lambda *a, **k: None)
    monkeypatch.setattr(model_discovery, "known_models", lambda provider: ["model-a", "model-b"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    lines = iter(["2"])  # pick #2 from the numbered plain-text list (no real terminal in tests)
    ctx = _ctx(
        config=LunaConfig(provider="anthropic"),
        input_fn=lambda _prompt: next(lines),
    )
    res = dispatch("/model", ctx)
    assert res.agent == "rebuilt"
    assert ctx.config.model == "model-b"


def test_model_with_no_arg_falls_back_to_free_text_when_no_list(monkeypatch):
    from luna.config import model_discovery

    monkeypatch.setattr(model_discovery, "list_models", lambda *a, **k: None)
    monkeypatch.setattr(model_discovery, "known_models", lambda provider: [])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    lines = iter(["claude-opus-4-1"])
    ctx = _ctx(
        config=LunaConfig(provider="anthropic"),
        input_fn=lambda _prompt: next(lines),
    )
    res = dispatch("/model", ctx)
    assert res.agent == "rebuilt"
    assert ctx.config.model == "claude-opus-4-1"
```

`LunaConfig` is already imported at the top of `tests/test_commands.py`
(confirmed while writing the provider-expansion plan) — no new import
needed for these tests.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_commands.py -v -k "model_with_no_arg"`
Expected: FAIL — the two picker tests fail because `/model` with no arg
still just prints today; the no-`input_fn` test currently passes already
(document that in your report, don't be alarmed if it's not RED — the
other two are the real RED here).

- [ ] **Step 4: Update `_model` in `luna/repl/commands.py`**

Add the import at the top of the file (alongside the existing
`from luna.config.providers import LunaConfigError, merge_providers`
line):

```python
from luna.config import model_discovery
```

Replace `_model`:

```python
def _model(ctx: CommandContext, arg: str) -> DispatchResult | None:
    if not arg:
        if ctx.input_fn is None:
            ctx.console.print(f"model: {ctx.config.model or '(provider default)'}")
            return None
        registry = merge_providers(ctx.config.custom_providers)
        spec = registry[ctx.config.provider]
        api_key = os.environ.get(spec.env_var) if spec.env_var else None
        if not api_key and spec.env_var:
            api_key = get_api_key(ctx.config.provider)
        arg = choose_model(ctx.console, ctx.input_fn, ctx.config.provider, spec, api_key=api_key)

    previous_model = ctx.config.model
    ctx.config.model = arg
    try:
        new_agent = ctx.rebuild()
    except Exception as exc:  # noqa: BLE001 - a bad model must not kill the REPL
        ctx.config.model = previous_model
        ctx.console.print(f"[{PALETTE['mauve']}]could not switch: {exc}[/]")
        return None
    ctx.console.print(f"[{PALETTE['blue']}]model → {arg}[/]")
    return DispatchResult(agent=new_agent)
```

This calls `choose_model` from `luna.repl.setup_wizard` — add that
import too:

```python
from luna.repl.setup_wizard import choose_model
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_commands.py -v -k "model"`
Expected: all pass, including every pre-existing `/model`/`/provider`
test in the file.

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 7: Lint**

Run: `uv run ruff check luna/repl/commands.py tests/test_commands.py && uv run ruff format --check luna/repl/commands.py tests/test_commands.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add luna/repl/commands.py tests/test_commands.py
git commit -m "feat: /model with no argument uses the live/known/text model picker"
```

---

### Task 5: Documentation + final verification

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `README.md` (only if it documents `/model`/`luna setup`'s current text-entry behavior — check first)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing (terminal task).

- [ ] **Step 1: Check whether README documents the old free-text model prompt**

```bash
grep -n "model \[" README.md
```

If this finds a line describing the old `model [default]:` free-text
prompt as the only way to pick a model, add one sentence noting it now
offers an arrow-key list when one is available (live or offline),
falling back to free text otherwise. If it finds nothing (the current
README doesn't describe this step in that level of detail), skip this
file — do not invent a section that wasn't there before.

- [ ] **Step 2: Add a CHANGELOG entry**

Under the existing `## [Unreleased]` section in `CHANGELOG.md`, add a new
bullet under `### Добавлено` if that subsection exists, else create it
(check the file's current structure first — it may already have one from
recent work):

```markdown
- `luna setup` и `/model` (без аргумента) теперь предлагают выбор модели
  стрелками вместо ввода имени вручную — список подтягивается живьём из
  API провайдера, если ключ уже введён и сеть доступна (таймаут 3с), иначе
  берётся из встроенного офлайн-списка `luna/config/known_models.toml`,
  иначе — прежний свободный ввод текстом. Заодно починен баг: пустой ответ
  на вопрос про модель раньше не сбрасывал `model.name` на дефолт
  провайдера, если там уже было сохранено другое значение — теперь сбрасывает
  всегда. Порядок шагов `luna setup` поменялся на провайдер → ключ → модель
  (было: провайдер → модель → ключ), чтобы живой список успевал
  подключиться к только что введённому ключу.
```

- [ ] **Step 3: Run the full verification gate**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all clean — this is the final gate for the whole feature.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md README.md
git commit -m "docs: document the model picker and the blank-model-reset fix"
```
