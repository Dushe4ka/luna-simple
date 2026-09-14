# Luna Provider Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grow Luna's LLM-provider registry from 5 to 13 built-in providers
(7 new native LangChain integrations + Cerebras via a new OpenAI-compatible
`base_url` mechanism), and let a user register their own arbitrary
OpenAI-compatible endpoint (vLLM, LM Studio, a corporate proxy, ...) through
`.luna.toml` without touching Luna's code.

**Architecture:** `ProviderSpec` gains an optional `base_url` field; when
set, `build_model` routes the call through the `openai:` `init_chat_model`
prefix with `base_url` injected into `model_kwargs` instead of using
`spec.init_prefix` directly for a dedicated integration. Seven new
providers get real native LangChain integration packages (each recognized
directly by `init_chat_model`'s own provider-prefix registry, confirmed
against current LangChain docs — not routed through the `base_url` path).
Cerebras is the first built-in consumer of the new `base_url` path. A new
`[provider.custom.<name>]` TOML section lets a user declare their own
`base_url`-based provider; `load_config` turns those into `ProviderSpec`
objects and `providers.merge_providers()` combines them with the built-in
registry (built-in names always win on collision). Every call site that
today imports the bare `PROVIDERS` dict to validate or look up a provider
(`cli.py`, `config.py`, `credentials.py`, `repl/commands.py`) is updated to
go through the merged registry instead, and `cli.py`'s `argparse` no longer
hard-validates `--provider`/`set-key`/`unset-key` against the static list
(that validation ran *before* `.luna.toml` was even read, which would have
rejected every custom provider before Luna got a chance to find it).

**Tech Stack:** Python 3.11+, `langchain~=1.4` (`init_chat_model`), `tomllib`
(stdlib, already used by `config.py`), `pytest` (existing `FakeToolCallingModel`-free
style already used in `tests/test_providers.py`/`tests/test_config.py` — no
network calls, ever).

**Spec:** No separate spec document — this is a *bounded* change (an
existing, well-defined flow in `luna/config/providers.py` + `config.py`
being extended along an established pattern), agreed directly in chat during
brainstorming rather than a written architectural spec. This plan document
*is* the record of that agreed design.

## Global Constraints

- Python 3.11+, PEP 8 / PEP 257, clean `ruff check` / `ruff format --check`.
- No `deepagents`/`langgraph` imports added anywhere by this plan (it only
  touches `luna/config/*`, `luna/cli.py`, `luna/repl/commands.py`,
  `luna/core/agent.py`'s single `build_model` call site, `pyproject.toml`,
  `README.md`, `CHANGELOG.md`) — the framework-import confinement rule in
  `AGENTS.md` is unaffected.
- Tests never touch the network. Every new test follows the existing
  `tests/test_providers.py` style: assert on `PROVIDERS`/`merge_providers()`
  contents, `resolve_model_string()` output, and `LunaConfigError` messages
  — never a real `init_chat_model()`/HTTP call.
- Model catalogs for hosted-inference providers (Groq, Fireworks, Together,
  OpenRouter, Cerebras, xAI, Mistral) rotate every few weeks. Every default
  model id below was verified against current provider/LangChain
  documentation on 2026-09-15 (via WebSearch and the `context7` MCP server
  against `/langchain-ai/docs`) — not guessed. Task 2 and Task 3 each
  include an explicit re-verification step so the executor confirms the id
  is still valid at implementation time rather than trusting a value that
  may already be stale.
- `ProviderSpec` stays a frozen `dataclass` (existing convention) — the new
  `base_url` field is added with a default so all 5 existing built-in
  entries remain valid without changes.

---

### Task 1: `base_url` mechanism on `ProviderSpec` / `build_model`

**Files:**
- Modify: `luna/config/providers.py` (whole file — `ProviderSpec`, `_spec`,
  `resolve_model_string`, `build_model`)
- Modify: `tests/test_providers.py` (add new tests; existing 6 tests must
  keep passing unmodified except the one hardcoding "5 providers", see Task
  2)

**Interfaces:**
- Produces: `ProviderSpec.base_url: str | None = None` (new field, default
  `None`); `_spec(provider: str, registry: dict[str, ProviderSpec] | None = None) -> ProviderSpec`;
  `resolve_model_string(provider: str, model: str | None, registry: dict[str, ProviderSpec] | None = None) -> str`;
  `build_model(provider: str, model: str | None = None, model_kwargs: dict | None = None, *, registry: dict[str, ProviderSpec] | None = None) -> BaseChatModel`.
  All three functions default `registry` to the module-level `PROVIDERS`
  when omitted, so every existing caller (which passes no `registry` arg)
  keeps working unchanged. Task 2/3 add real entries; Task 4 adds
  `merge_providers()`; Task 6 threads a real merged registry through from
  `LunaConfig`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_providers.py`:

```python
def test_provider_spec_base_url_defaults_to_none():
    assert PROVIDERS["anthropic"].base_url is None


def test_spec_accepts_an_explicit_registry():
    custom = {"fake": ProviderSpec("fake", "openai", "fake-model", "FAKE_API_KEY", "openai", "https://fake.example/v1")}
    from luna.config.providers import _spec

    assert _spec("fake", custom).key == "fake"
    with pytest.raises(LunaConfigError):
        _spec("fake")  # not in the default PROVIDERS registry


def test_resolve_model_string_uses_given_registry():
    custom = {"fake": ProviderSpec("fake", "openai", "fake-model", None, "openai", "https://fake.example/v1")}
    assert resolve_model_string("fake", None, custom) == "openai:fake-model"


def test_build_model_injects_base_url_into_kwargs(monkeypatch):
    captured = {}

    def fake_init_chat_model(model_string, **kwargs):
        captured["model_string"] = model_string
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)
    monkeypatch.setenv("FAKE_API_KEY", "sk-test")
    custom = {
        "fake": ProviderSpec("fake", "openai", "fake-model", "FAKE_API_KEY", "openai", "https://fake.example/v1")
    }
    build_model("fake", registry=custom)
    assert captured["model_string"] == "openai:fake-model"
    assert captured["kwargs"]["base_url"] == "https://fake.example/v1"


def test_build_model_does_not_override_an_explicit_base_url_kwarg(monkeypatch):
    captured = {}

    def fake_init_chat_model(model_string, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)
    monkeypatch.setenv("FAKE_API_KEY", "sk-test")
    custom = {
        "fake": ProviderSpec("fake", "openai", "fake-model", "FAKE_API_KEY", "openai", "https://fake.example/v1")
    }
    build_model("fake", model_kwargs={"base_url": "https://override.example/v1"}, registry=custom)
    assert captured["kwargs"]["base_url"] == "https://override.example/v1"
```

`ProviderSpec` is already imported in the test file's target module; add it
to the existing `from luna.config.providers import (...)` block at the top
of `tests/test_providers.py` alongside `PROVIDERS`, `LunaConfigError`,
`build_model`, `resolve_model_string`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_providers.py -v -k "base_url or explicit_registry or given_registry"`
Expected: FAIL — `TypeError: ProviderSpec.__init__() takes ... positional arguments` (no `base_url` field yet) and/or `TypeError: _spec() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Implement `base_url` on `ProviderSpec` and thread `registry` through**

Rewrite `luna/config/providers.py` in full:

```python
"""Provider registry: map a provider key to a LangChain chat model."""

from __future__ import annotations

import os
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel


class LunaConfigError(Exception):
    """Raised for unrecoverable configuration problems (bad provider, missing key)."""


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of a supported model provider.

    ``base_url`` is only set for providers reached through the generic
    OpenAI-compatible path (Cerebras, and any user-defined
    ``[provider.custom.<name>]`` entry): for those, ``init_prefix`` is
    always ``"openai"`` and ``build_model`` injects ``base_url`` into the
    keyword arguments passed to ``init_chat_model`` instead of relying on
    a dedicated per-provider integration package.
    """

    key: str
    init_prefix: str
    default_model: str
    env_var: str | None
    pip_extra: str
    base_url: str | None = None


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        "anthropic", "anthropic", "claude-sonnet-4-5", "ANTHROPIC_API_KEY", "anthropic"
    ),
    "deepseek": ProviderSpec(
        "deepseek", "deepseek", "deepseek-chat", "DEEPSEEK_API_KEY", "deepseek"
    ),
    "openai": ProviderSpec("openai", "openai", "gpt-4.1", "OPENAI_API_KEY", "openai"),
    "google": ProviderSpec("google", "google_genai", "gemini-2.5-pro", "GOOGLE_API_KEY", "google"),
    "ollama": ProviderSpec("ollama", "ollama", "qwen2.5-coder", None, "ollama"),
}

DEFAULT_PROVIDER = "anthropic"


def _spec(provider: str, registry: dict[str, ProviderSpec] | None = None) -> ProviderSpec:
    reg = PROVIDERS if registry is None else registry
    try:
        return reg[provider]
    except KeyError:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(sorted(reg))}."
        ) from None


def resolve_model_string(
    provider: str,
    model: str | None,
    registry: dict[str, ProviderSpec] | None = None,
) -> str:
    """Return the ``<prefix>:<model>`` string passed to ``init_chat_model``."""
    spec = _spec(provider, registry)
    return f"{spec.init_prefix}:{model or spec.default_model}"


def build_model(
    provider: str,
    model: str | None = None,
    model_kwargs: dict | None = None,
    *,
    registry: dict[str, ProviderSpec] | None = None,
) -> BaseChatModel:
    """Instantiate a LangChain chat model for ``provider``.

    Raises:
        LunaConfigError: unknown provider, missing API key, or missing
            integration package.

    """
    spec = _spec(provider, registry)
    if spec.env_var and not os.environ.get(spec.env_var):
        from luna.config.credentials import apply_stored_key

        apply_stored_key(provider, spec.env_var)
    if spec.env_var and not os.environ.get(spec.env_var):
        raise LunaConfigError(
            f"No API key for {spec.key}. Run 'luna setup' to configure one, "
            f"export {spec.env_var}, or pick another provider with --provider."
        )
    from langchain.chat_models import init_chat_model

    kwargs = dict(model_kwargs or {})
    if spec.base_url:
        kwargs.setdefault("base_url", spec.base_url)
    try:
        return init_chat_model(resolve_model_string(provider, model, registry), **kwargs)
    except ImportError as exc:
        raise LunaConfigError(
            f"The {spec.key} integration is not installed. "
            f'Run:  pip install "luna-simple[{spec.pip_extra}]"'
        ) from exc
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_providers.py -v`
Expected: all pass (the original 6 tests plus the 5 new ones — 11 total).

- [ ] **Step 5: Lint**

Run: `uv run ruff check luna/config/providers.py tests/test_providers.py && uv run ruff format --check luna/config/providers.py tests/test_providers.py`
Expected: clean (run `uv run ruff format` on both paths and re-check if not).

- [ ] **Step 6: Commit**

```bash
git add luna/config/providers.py tests/test_providers.py
git commit -m "feat: base_url mechanism on ProviderSpec/build_model for OpenAI-compatible endpoints"
```

---

### Task 2: Seven new native providers (Mistral, xAI, Groq, Fireworks, Together, OpenRouter, Perplexity)

**Files:**
- Modify: `luna/config/providers.py` (`PROVIDERS` dict — add 7 entries)
- Modify: `pyproject.toml` (`[project.optional-dependencies]` — 7 new extras
  + append each new package to the existing `all` extra)
- Modify: `tests/test_providers.py`

**Interfaces:**
- Consumes: `ProviderSpec` (Task 1).
- Produces: 7 new keys in `PROVIDERS` — `mistral`, `xai`, `groq`,
  `fireworks`, `together`, `openrouter`, `perplexity` — each with a real,
  currently-valid `init_prefix` recognized natively by LangChain's
  `init_chat_model` (confirmed 2026-09-15 against `/langchain-ai/docs` via
  `context7` and the LangChain provider-prefix list: `mistralai`, `xai`,
  `groq`, `fireworks`, `together`, `openrouter`, `perplexity` are all
  first-class prefixes, each backed by its own `langchain-<name>` pip
  package — none of these seven go through the `base_url` path from Task 1).

Every default model id below is a real, currently-documented id (not the
provider's cheapest/smallest model, but not necessarily the single newest
release either, since these catalogs move week to week) — Step 1 embeds a
mandatory re-check before you rely on any of them for real usage.

- [ ] **Step 1: Re-verify the seven default model ids are still current**

Before writing the entries below, run these two checks and compare against
the values already filled in in Step 2 — if a provider has since retired
the model listed (as Groq did to `llama-3.3-70b-versatile` in August 2026,
mid-plan-writing), replace it with that provider's current recommended
coding/general model and note the change in the commit message:

```bash
# Quick sanity check via WebSearch (already run once during planning,
# 2026-09-15): re-run for any provider you're unsure about, e.g.:
#   WebSearch("Groq console models list current recommended coding model")
#   WebSearch("Cerebras / Mistral / xAI / Together / Fireworks / OpenRouter / Perplexity current default model")
```

If nothing changed, proceed directly to Step 2 with the values below.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_providers.py`:

```python
def test_registry_has_thirteen_providers():
    assert set(PROVIDERS) == {
        "anthropic",
        "deepseek",
        "openai",
        "google",
        "ollama",
        "mistral",
        "xai",
        "groq",
        "fireworks",
        "together",
        "openrouter",
        "perplexity",
        "cerebras",
    }


def test_mistral_uses_mistralai_prefix():
    assert PROVIDERS["mistral"].init_prefix == "mistralai"
    assert resolve_model_string("mistral", None) == "mistralai:mistral-large-latest"


def test_xai_uses_xai_prefix():
    assert PROVIDERS["xai"].init_prefix == "xai"
    assert resolve_model_string("xai", None) == "xai:grok-4"


def test_groq_uses_groq_prefix():
    assert PROVIDERS["groq"].init_prefix == "groq"
    assert resolve_model_string("groq", None) == "groq:openai/gpt-oss-120b"


def test_fireworks_uses_fireworks_prefix():
    assert PROVIDERS["fireworks"].init_prefix == "fireworks"
    assert resolve_model_string("fireworks", None) == (
        "fireworks:accounts/fireworks/models/qwen3p5-397b-a17b"
    )


def test_together_uses_together_prefix():
    assert PROVIDERS["together"].init_prefix == "together"
    assert resolve_model_string("together", None) == "together:Qwen/Qwen2.5-Coder-32B-Instruct"


def test_openrouter_uses_openrouter_prefix():
    assert PROVIDERS["openrouter"].init_prefix == "openrouter"
    assert resolve_model_string("openrouter", None) == "openrouter:anthropic/claude-sonnet-4-6"


def test_perplexity_uses_perplexity_prefix():
    assert PROVIDERS["perplexity"].init_prefix == "perplexity"
    assert resolve_model_string("perplexity", None) == "perplexity:sonar"


def test_new_native_providers_have_no_base_url():
    for key in ("mistral", "xai", "groq", "fireworks", "together", "openrouter", "perplexity"):
        assert PROVIDERS[key].base_url is None


def test_new_native_providers_each_use_their_own_pip_extra():
    expected = {
        "mistral": "mistral",
        "xai": "xai",
        "groq": "groq",
        "fireworks": "fireworks",
        "together": "together",
        "openrouter": "openrouter",
        "perplexity": "perplexity",
    }
    for key, extra in expected.items():
        assert PROVIDERS[key].pip_extra == extra
```

Note: `test_registry_has_thirteen_providers` already lists `cerebras`,
added in Task 3 — this test is written now but stays RED until Task 3 also
lands; run only the other new tests in the next step, then come back and
confirm this one once Task 3 is done (Task 3, Step 2 re-runs the full file).

- [ ] **Step 3: Run the tests to verify they fail (except the 13-provider one)**

Run: `uv run pytest tests/test_providers.py -v -k "not thirteen"`
Expected: FAIL with `KeyError: 'mistral'` (and similarly for the other six) — none of the new entries exist in `PROVIDERS` yet.

- [ ] **Step 4: Add the 7 entries to `PROVIDERS`**

In `luna/config/providers.py`, extend the `PROVIDERS` dict (keep `"ollama"` last, append after it):

```python
    "ollama": ProviderSpec("ollama", "ollama", "qwen2.5-coder", None, "ollama"),
    "mistral": ProviderSpec(
        "mistral", "mistralai", "mistral-large-latest", "MISTRAL_API_KEY", "mistral"
    ),
    "xai": ProviderSpec("xai", "xai", "grok-4", "XAI_API_KEY", "xai"),
    "groq": ProviderSpec(
        "groq", "groq", "openai/gpt-oss-120b", "GROQ_API_KEY", "groq"
    ),
    "fireworks": ProviderSpec(
        "fireworks",
        "fireworks",
        "accounts/fireworks/models/qwen3p5-397b-a17b",
        "FIREWORKS_API_KEY",
        "fireworks",
    ),
    "together": ProviderSpec(
        "together",
        "together",
        "Qwen/Qwen2.5-Coder-32B-Instruct",
        "TOGETHER_API_KEY",
        "together",
    ),
    "openrouter": ProviderSpec(
        "openrouter",
        "openrouter",
        "anthropic/claude-sonnet-4-6",
        "OPENROUTER_API_KEY",
        "openrouter",
    ),
    "perplexity": ProviderSpec(
        "perplexity", "perplexity", "sonar", "PPLX_API_KEY", "perplexity"
    ),
```

`PPLX_API_KEY` (not `PERPLEXITY_API_KEY`) is Perplexity's own documented
environment variable name for their SDKs/integrations — keep it exact, it's
what `langchain-perplexity` reads.

- [ ] **Step 5: Add the 7 new pip extras to `pyproject.toml`**

In `pyproject.toml`, extend `[project.optional-dependencies]` (after the
existing `ollama = [...]` line, before `mcp = [...]`):

```toml
mistral = ["langchain-mistralai>=1.0"]
xai = ["langchain-xai>=1.0"]
groq = ["langchain-groq>=1.0"]
fireworks = ["langchain-fireworks>=1.0"]
together = ["langchain-together>=1.0"]
openrouter = ["langchain-openrouter>=0.1"]
perplexity = ["langchain-perplexity>=1.0"]
```

And extend the `all` extra to include all seven new packages, so it reads:

```toml
all = [
    "langchain-deepseek>=0.1",
    "langchain-openai>=1.0",
    "langchain-google-genai>=3.0",
    "langchain-ollama>=1.0",
    "langchain-mistralai>=1.0",
    "langchain-xai>=1.0",
    "langchain-groq>=1.0",
    "langchain-fireworks>=1.0",
    "langchain-together>=1.0",
    "langchain-openrouter>=0.1",
    "langchain-perplexity>=1.0",
    "langchain-mcp-adapters>=0.3",
    "multilspy>=0.0.15",
]
```

- [ ] **Step 6: Run the tests to verify they pass (except the 13-provider one, still pending Task 3)**

Run: `uv run pytest tests/test_providers.py -v -k "not thirteen"`
Expected: all pass.

- [ ] **Step 7: Lint**

Run: `uv run ruff check luna/config/providers.py tests/test_providers.py && uv run ruff format --check luna/config/providers.py tests/test_providers.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add luna/config/providers.py pyproject.toml tests/test_providers.py
git commit -m "feat: add Mistral, xAI, Groq, Fireworks, Together, OpenRouter, Perplexity providers"
```

---

### Task 3: Cerebras via the `base_url` path

**Files:**
- Modify: `luna/config/providers.py` (`PROVIDERS` dict — 1 entry)
- Modify: `tests/test_providers.py`

**Interfaces:**
- Consumes: `ProviderSpec.base_url` (Task 1).
- Produces: `PROVIDERS["cerebras"]` — the first built-in provider that
  exercises the `base_url` mechanism end to end, proving it works before
  Task 4 lets a user define their own such entry via config.

Cerebras is not in LangChain's native `init_chat_model` provider-prefix
list (confirmed 2026-09-15 — unlike the seven in Task 2) but exposes a
fully OpenAI-compatible endpoint. Its documented base URL is
`https://api.cerebras.ai/v1` and its documented default model is
`gpt-oss-120b`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_providers.py`:

```python
def test_cerebras_uses_base_url_path():
    spec = PROVIDERS["cerebras"]
    assert spec.init_prefix == "openai"
    assert spec.base_url == "https://api.cerebras.ai/v1"
    assert spec.pip_extra == "openai"
    assert resolve_model_string("cerebras", None) == "openai:gpt-oss-120b"
```

- [ ] **Step 2: Run the tests to verify this one fails and the 13-provider one now also fails for a different reason**

Run: `uv run pytest tests/test_providers.py -v -k "cerebras or thirteen"`
Expected: both FAIL — `test_cerebras_uses_base_url_path` with `KeyError: 'cerebras'`, `test_registry_has_thirteen_providers` with a set-mismatch missing `'cerebras'`.

- [ ] **Step 3: Add the Cerebras entry**

In `luna/config/providers.py`, append after the `"perplexity"` entry added in Task 2:

```python
    "cerebras": ProviderSpec(
        "cerebras",
        "openai",
        "gpt-oss-120b",
        "CEREBRAS_API_KEY",
        "openai",
        "https://api.cerebras.ai/v1",
    ),
```

No new `pyproject.toml` extra is needed — Cerebras reuses the existing
`openai` extra (`langchain-openai`, already a dependency of the `openai`
provider), since it's reached through `ChatOpenAI` with a different
`base_url`, not a dedicated Cerebras integration package.

- [ ] **Step 4: Run the full provider test file to verify everything passes, including the 13-provider count**

Run: `uv run pytest tests/test_providers.py -v`
Expected: all pass (18 tests: the original 6 + 5 from Task 1 + 9 from Task 2 minus the duplicate 13-provider one already counted + 1 from this task — run the file and confirm the actual count rather than trusting this arithmetic).

- [ ] **Step 5: Lint**

Run: `uv run ruff check luna/config/providers.py tests/test_providers.py && uv run ruff format --check luna/config/providers.py tests/test_providers.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add luna/config/providers.py tests/test_providers.py
git commit -m "feat: add Cerebras provider via the base_url path"
```

---

### Task 4: `[provider.custom.<name>]` config parsing + `merge_providers()`

**Files:**
- Modify: `luna/config/config.py` (`_apply_toml`, `LunaConfig`, `load_config`)
- Modify: `luna/config/providers.py` (new `merge_providers()` function)
- Modify: `luna/repl/setup_wizard.py` (one-line docstring/comment recording
  the scope decision — custom providers are not offered in the interactive
  wizard)
- Test: `tests/test_custom_providers.py` (new file)
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `ProviderSpec`, `PROVIDERS`, `LunaConfigError` (existing,
  `providers.py`).
- Produces: `luna.config.providers.merge_providers(custom: dict[str, ProviderSpec]) -> dict[str, ProviderSpec]`
  — built-in `PROVIDERS` entries always win on a name collision with
  `custom`. `LunaConfig.custom_providers: dict[str, ProviderSpec]` (new
  field, default `{}`) — already-validated `ProviderSpec` objects, ready
  for `merge_providers()`, not raw TOML dicts. Task 5 (validation) and Task
  6 (call sites) both consume `custom_providers` and `merge_providers`.

- [ ] **Step 1: Write the failing tests for `merge_providers()`**

Create `tests/test_custom_providers.py`:

```python
from luna.config.providers import PROVIDERS, ProviderSpec, merge_providers


def test_merge_providers_adds_a_new_key():
    custom = {
        "mylocal": ProviderSpec("mylocal", "openai", "local-model", "MYLOCAL_API_KEY", "openai", "http://localhost:8000/v1")
    }
    merged = merge_providers(custom)
    assert merged["mylocal"].base_url == "http://localhost:8000/v1"
    assert merged["anthropic"] is PROVIDERS["anthropic"]


def test_merge_providers_builtin_wins_on_collision():
    fake_anthropic = ProviderSpec("anthropic", "openai", "fake", "FAKE_KEY", "openai", "http://fake/v1")
    merged = merge_providers({"anthropic": fake_anthropic})
    assert merged["anthropic"] is PROVIDERS["anthropic"]
    assert merged["anthropic"].base_url is None


def test_merge_providers_with_no_custom_entries_returns_builtin_only():
    assert merge_providers({}) == PROVIDERS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_custom_providers.py -v`
Expected: FAIL — `ImportError: cannot import name 'merge_providers'`.

- [ ] **Step 3: Implement `merge_providers()`**

In `luna/config/providers.py`, add after the `PROVIDERS` dict / `DEFAULT_PROVIDER` line:

```python
def merge_providers(custom: dict[str, ProviderSpec]) -> dict[str, ProviderSpec]:
    """Combine user-defined custom providers with the built-in registry.

    A custom entry can never shadow a built-in provider name — if a user's
    ``[provider.custom.<name>]`` collides with a built-in key, the built-in
    ``ProviderSpec`` wins silently (no error): this keeps well-known names
    like ``anthropic``/``openai`` from ever being redirected to an
    unexpected endpoint by a stray config entry.
    """
    merged = dict(custom)
    merged.update(PROVIDERS)
    return merged
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_custom_providers.py -v`
Expected: all 3 pass.

- [ ] **Step 5: Write the failing tests for TOML parsing + `LunaConfig.custom_providers`**

Add to `tests/test_config.py`:

```python
def test_custom_provider_parsed_from_project_toml(tmp_path):
    (tmp_path / ".luna.toml").write_text(
        '[provider.custom.mylocal]\n'
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
        '[provider.custom.mylocal]\n'
        'base_url = "http://localhost:8000/v1"\n'
        'env_var = "MYLOCAL_API_KEY"\n'
    )
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    assert cfg.custom_providers["mylocal"].default_model  # non-empty fallback, exact value not asserted here


def test_custom_provider_missing_base_url_raises(tmp_path):
    (tmp_path / ".luna.toml").write_text(
        '[provider.custom.mylocal]\nenv_var = "MYLOCAL_API_KEY"\n'
    )
    with pytest.raises(LunaConfigError):
        load_config({}, env={}, cwd=str(tmp_path))


def test_custom_provider_missing_env_var_raises(tmp_path):
    (tmp_path / ".luna.toml").write_text(
        '[provider.custom.mylocal]\nbase_url = "http://localhost:8000/v1"\n'
    )
    with pytest.raises(LunaConfigError):
        load_config({}, env={}, cwd=str(tmp_path))


def test_no_custom_providers_section_gives_empty_dict(tmp_path):
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    assert cfg.custom_providers == {}
```

This needs `pytest` and `LunaConfigError` imported at the top of
`tests/test_config.py` — check the existing imports first (the file
already imports `load_config`; add
`import pytest` and `from luna.config.providers import LunaConfigError`
if not already present).

- [ ] **Step 6: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v -k custom_provider`
Expected: FAIL — `AttributeError: 'LunaConfig' object has no attribute 'custom_providers'`.

- [ ] **Step 7: Parse `[provider.custom.*]` in `_apply_toml`**

In `luna/config/config.py`, add this block to `_apply_toml` (after the
existing `ui = data.get("ui", {})` / `into["show_splash"] = ...` lines, at
the end of the function):

```python
    provider = data.get("provider", {})
    if isinstance(provider.get("custom"), dict):
        into["custom_providers"] = {
            k: dict(v) for k, v in provider["custom"].items() if isinstance(v, dict)
        }
```

This mirrors the existing `pricing` handling immediately above it in the
same function (a later TOML layer fully replaces the raw dict from an
earlier one — no deep merge across layers, consistent with how `pricing`
already behaves).

- [ ] **Step 8: Add `custom_providers` to `LunaConfig` and build it in `load_config`**

In `luna/config/config.py`:

1. Update the import line at the top:

```python
from luna.config.providers import DEFAULT_PROVIDER, PROVIDERS, LunaConfigError, ProviderSpec, merge_providers
```

2. Add a field to the `LunaConfig` dataclass (after `extra_model_kwargs: dict = field(default_factory=dict)`):

```python
    custom_providers: dict[str, ProviderSpec] = field(default_factory=dict)
```

3. In `load_config`, replace the existing validation block:

```python
    provider = merged.get("provider", DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(PROVIDERS)}."
        )

    return LunaConfig(
```

with:

```python
    custom_providers: dict[str, ProviderSpec] = {}
    for name, fields in merged.get("custom_providers", {}).items():
        base_url = fields.get("base_url")
        env_var = fields.get("env_var")
        if not base_url:
            raise LunaConfigError(
                f"Custom provider {name!r} needs base_url in [provider.custom.{name}]."
            )
        if not env_var:
            raise LunaConfigError(
                f"Custom provider {name!r} needs env_var in [provider.custom.{name}]."
            )
        custom_providers[name] = ProviderSpec(
            key=name,
            init_prefix="openai",
            default_model=fields.get("default_model") or "gpt-4o",
            env_var=env_var,
            pip_extra="openai",
            base_url=base_url,
        )

    registry = merge_providers(custom_providers)
    provider = merged.get("provider", DEFAULT_PROVIDER)
    if provider not in registry:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(sorted(registry))}."
        )

    return LunaConfig(
```

4. Add `custom_providers=custom_providers,` to the `LunaConfig(...)` constructor call's keyword arguments (anywhere in the list — e.g. right after `provider=provider,`).

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py tests/test_custom_providers.py tests/test_providers.py -v`
Expected: all pass, including every pre-existing test in `test_config.py`
(the new field has a default-factory empty dict, so no existing test
should need changes).

- [ ] **Step 10: Document the setup-wizard scope decision**

In `luna/repl/setup_wizard.py`, find the module-level docstring (or the
top of the file if there is none) and add one sentence — do not otherwise
change this file's behavior:

```python
"""Interactive first-run wizard for ``luna setup``.

Only lists built-in providers (``luna.config.providers.PROVIDERS``) — a
user-defined ``[provider.custom.<name>]`` (see ``config.py``) is configured
by hand in ``.luna.toml``/``config.toml``, not offered here, since the
wizard has no way to prompt for an arbitrary provider's base_url/env_var
pair sensibly.
"""
```

If the file already has a module docstring, append this note to it rather
than replacing it wholesale — read the file first to match its exact
current wording.

- [ ] **Step 11: Lint**

Run: `uv run ruff check luna/config/config.py luna/config/providers.py luna/repl/setup_wizard.py tests/test_config.py tests/test_custom_providers.py && uv run ruff format --check luna/config/config.py luna/config/providers.py luna/repl/setup_wizard.py tests/test_config.py tests/test_custom_providers.py`
Expected: clean.

- [ ] **Step 12: Commit**

```bash
git add luna/config/config.py luna/config/providers.py luna/repl/setup_wizard.py tests/test_config.py tests/test_custom_providers.py
git commit -m "feat: [provider.custom.*] config section + merge_providers()"
```

---

### Task 5: Registry-aware validation in `config.py` / `credentials.py`

**Files:**
- Modify: `luna/config/config.py` (`set_config_values`)
- Modify: `luna/config/credentials.py` (`set_api_key`, `unset_api_key`)
- Modify: `tests/test_config.py`
- Modify: `tests/test_credentials.py` (if it exists — check first; if not,
  create it following the style of the other `tests/test_*.py` files)

**Interfaces:**
- Consumes: `merge_providers`, `ProviderSpec` (Task 4).
- Produces: `set_api_key(provider, api_key, *, env=None, registry=None) -> Path`
  and `unset_api_key(provider, *, env=None, registry=None) -> bool` both
  accept an optional `registry` keyword (defaulting to `PROVIDERS`, same
  pattern as Task 1's `_spec`/`build_model`) so a custom provider's key can
  be stored/removed once Task 6 passes a real merged registry in from
  `cli.py`.

- [ ] **Step 1: Check whether `tests/test_credentials.py` exists**

```bash
ls tests/test_credentials.py 2>/dev/null && echo EXISTS || echo MISSING
```

If `MISSING`, Step 3 below creates the file from scratch with a minimal
existing-behavior test alongside the new one; if `EXISTS`, Step 3 only adds
the new test to it — read the file first so the added test matches its
existing import style.

- [ ] **Step 2: Write the failing test for `set_config_values`**

Add to `tests/test_config.py`:

```python
def test_set_config_values_still_validates_provider_against_builtin(isolated_config_home):
    from luna.config.config import set_config_values
    from luna.config.providers import LunaConfigError

    with pytest.raises(LunaConfigError):
        set_config_values({"model.provider": "not-a-real-provider"})
```

This must already pass unmodified (it's `set_config_values`'s existing
behavior against the bare `PROVIDERS`, from before this plan) — it's
written here as a regression guard, not a new requirement: `set_config_values`
has no way to know about a project's `.luna.toml`-only custom providers (it
writes to the *global* `~/.config/luna/config.toml`, unrelated to any one
project's custom entries), so it intentionally keeps validating against
`PROVIDERS` only, not a merged registry. No production code change is
needed for this step — it documents and locks in that intentional scope
boundary.

- [ ] **Step 3: Write the failing tests for `set_api_key`/`unset_api_key` registry support**

If `tests/test_credentials.py` is `MISSING`, create it:

```python
import pytest

from luna.config.credentials import get_api_key, set_api_key, unset_api_key
from luna.config.providers import LunaConfigError, ProviderSpec


def test_set_api_key_rejects_unknown_provider(isolated_config_home):
    with pytest.raises(LunaConfigError):
        set_api_key("not-a-real-provider", "sk-test")


def test_set_and_get_api_key_roundtrip(isolated_config_home):
    set_api_key("anthropic", "sk-test-123")
    assert get_api_key("anthropic") == "sk-test-123"


def test_set_api_key_accepts_a_provider_from_a_custom_registry(isolated_config_home):
    custom_registry = {
        "mylocal": ProviderSpec("mylocal", "openai", "local-model", "MYLOCAL_API_KEY", "openai", "http://localhost:8000/v1")
    }
    set_api_key("mylocal", "sk-local-test", registry=custom_registry)
    assert get_api_key("mylocal") == "sk-local-test"


def test_set_api_key_still_rejects_unknown_provider_against_a_custom_registry(isolated_config_home):
    custom_registry = {
        "mylocal": ProviderSpec("mylocal", "openai", "local-model", "MYLOCAL_API_KEY", "openai", "http://localhost:8000/v1")
    }
    with pytest.raises(LunaConfigError):
        set_api_key("not-in-either-registry", "sk-test", registry=custom_registry)


def test_unset_api_key_works_with_a_custom_registry(isolated_config_home):
    custom_registry = {
        "mylocal": ProviderSpec("mylocal", "openai", "local-model", "MYLOCAL_API_KEY", "openai", "http://localhost:8000/v1")
    }
    set_api_key("mylocal", "sk-local-test", registry=custom_registry)
    assert unset_api_key("mylocal") is True
    assert get_api_key("mylocal") is None
```

`isolated_config_home` is the existing autouse-style fixture already used
by `tests/test_config.py` (see `AGENTS.md`: "`conftest.py` autouse fixture
points `XDG_CONFIG_HOME` at tmp") — check `tests/conftest.py` for its exact
name/signature before using it verbatim; use whatever fixture the existing
`test_verify_command_settable` test in `tests/test_config.py` uses (it's
named `isolated_config_home` in that test's signature already).

If `tests/test_credentials.py` `EXISTS` already, only add the three new
`registry=`-parametrized tests to it (`test_set_api_key_accepts_a_provider_from_a_custom_registry`,
`test_set_api_key_still_rejects_unknown_provider_against_a_custom_registry`,
`test_unset_api_key_works_with_a_custom_registry`), matching its existing
fixture usage rather than the snippet above verbatim.

- [ ] **Step 4: Run the tests to verify the new ones fail**

Run: `uv run pytest tests/test_credentials.py tests/test_config.py -v -k "registry or custom_registry"`
Expected: FAIL — `TypeError: set_api_key() got an unexpected keyword argument 'registry'`.

- [ ] **Step 5: Add `registry` support to `credentials.py`**

In `luna/config/credentials.py`:

1. Update the import line:

```python
from luna.config.providers import PROVIDERS, LunaConfigError, ProviderSpec
```

2. Replace `set_api_key`:

```python
def set_api_key(
    provider: str,
    api_key: str,
    *,
    env: Mapping[str, str] | None = None,
    registry: dict[str, ProviderSpec] | None = None,
) -> Path:
    """Store ``api_key`` for ``provider``. Returns the credentials file path."""
    reg = PROVIDERS if registry is None else registry
    if provider not in reg:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(sorted(reg))}."
        )
    if not api_key.strip():
        raise LunaConfigError("API key must not be empty.")
    data = {k: dict(v) for k, v in load_credentials(env).items()}
    data.setdefault(provider, {})["api_key"] = api_key.strip()
    path = credentials_path(env)
    _write(data, path)
    return path
```

3. `unset_api_key` needs no signature change for correctness (it only
   removes a key by name, no validation against the registry happens
   there today) — but add the same `registry` keyword for interface
   symmetry with `set_api_key`, unused internally:

```python
def unset_api_key(
    provider: str,
    *,
    env: Mapping[str, str] | None = None,
    registry: dict[str, ProviderSpec] | None = None,
) -> bool:
    """Remove the stored key for ``provider``. Returns True if one was removed."""
    data = {k: dict(v) for k, v in load_credentials(env).items()}
    if data.pop(provider, None) is None:
        return False
    _write(data, credentials_path(env))
    return True
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_credentials.py tests/test_config.py -v`
Expected: all pass, including every pre-existing test.

- [ ] **Step 7: Lint**

Run: `uv run ruff check luna/config/credentials.py tests/test_credentials.py tests/test_config.py && uv run ruff format --check luna/config/credentials.py tests/test_credentials.py tests/test_config.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add luna/config/credentials.py tests/test_credentials.py tests/test_config.py
git commit -m "feat: set_api_key/unset_api_key accept an optional custom-provider registry"
```

---

### Task 6: Wire the merged registry through `cli.py` / `repl/commands.py` / `agent.py`

**Files:**
- Modify: `luna/cli.py`
- Modify: `luna/repl/commands.py`
- Modify: `luna/core/agent.py`
- Modify: `tests/test_cli.py` (check it exists first — if not, look for
  wherever `main()`/`build_parser()` is currently tested and extend that)
- Modify: `tests/test_repl_commands.py` (or wherever `_provider`/`/provider`
  is currently tested — check first)

**Interfaces:**
- Consumes: `merge_providers`, `LunaConfig.custom_providers` (Task 4/5).
- Produces: no new public interface — `--provider`/`config set-key`/`config
  unset-key` accept any string at the `argparse` level now (validation
  moves to `load_config`/`set_api_key`, which already raise `LunaConfigError`
  with a clear message); `/provider <name>` in the REPL recognizes a
  project's custom providers; `build_model`'s one call site in
  `luna/core/agent.py` passes a real merged registry instead of relying on
  the default bare `PROVIDERS`.

**Pre-existing test this task will break (found by reading `tests/test_cli.py`
before writing anything new):** `test_bad_provider_is_usage_error` (line 30)
currently asserts `main(["hi", "--provider", "grok"])` raises `SystemExit`
with code 2, because `argparse`'s `choices=` rejects `"grok"` itself. Once
`choices=` is removed (Step 4 below), `"grok"` instead flows through to
`load_config`, which raises `LunaConfigError`, caught by `main()`'s own
`try/except` and turned into a *returned* exit code 2 (not a `SystemExit`)
— the same pattern the neighboring `test_unknown_provider_via_env_is_config_error`
(line 36) already exercises for the env-var path. Step 2 replaces this test
rather than leaving it to fail.

- [ ] **Step 1: Confirm the two target test files and their fixture patterns**

Already located by reading the repo directly while writing this plan:
`tests/test_cli.py` (imports `from luna.cli import build_parser, main`; no
existing `_has_api_key` coverage) and `tests/test_commands.py` (defines a
`_ctx(**kw) -> CommandContext` helper built on
`from luna.repl.commands import CommandContext, dispatch` and
`from luna.config.config import LunaConfig`, exercised via
`dispatch("/provider anthropic", ctx)` in the existing
`test_provider_without_key_does_not_swap`). Steps 2+ use these two files
and this exact `_ctx`/`dispatch` pattern — no need to re-grep.

- [ ] **Step 2: Write the failing tests**

In `tests/test_cli.py`, replace the now-outdated test:

```python
def test_bad_provider_is_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        main(["hi", "--provider", "grok"])
    assert e.value.code == 2  # argparse rejects the choice
```

with:

```python
def test_bad_provider_is_config_error(capsys):
    code = main(["hi", "--provider", "grok"])
    assert code == 2
    assert "grok" in capsys.readouterr().err


def test_provider_flag_accepts_a_provider_not_in_the_builtin_choices_list():
    """argparse itself must not reject an unknown-looking --provider value
    before Luna gets a chance to check it against a project's
    [provider.custom.*] — that check happens later, in load_config."""
    args = build_parser().parse_args(["--provider", "some-custom-name", "hello"])
    assert args.provider == "some-custom-name"
```

Also add to `tests/test_cli.py`:

```python
def test_has_api_key_checks_a_custom_provider_via_the_given_registry(monkeypatch):
    from luna.cli import _has_api_key
    from luna.config.providers import ProviderSpec

    monkeypatch.setenv("MYLOCAL_API_KEY", "sk-test")
    registry = {
        "mylocal": ProviderSpec("mylocal", "openai", "local-model", "MYLOCAL_API_KEY", "openai", "http://localhost:8000/v1")
    }
    assert _has_api_key("mylocal", registry) is True
```

And to `tests/test_commands.py`:

```python
def test_provider_recognizes_a_custom_provider_from_config(monkeypatch):
    from luna.config.providers import ProviderSpec

    monkeypatch.setenv("MYLOCAL_API_KEY", "sk-test")
    custom = {
        "mylocal": ProviderSpec(
            "mylocal", "openai", "local-model", "MYLOCAL_API_KEY", "openai", "http://localhost:8000/v1"
        )
    }
    ctx = _ctx(config=LunaConfig(provider="anthropic", custom_providers=custom))
    dispatch("/provider mylocal", ctx)
    assert ctx.config.provider == "mylocal"
    assert "unknown provider" not in ctx.console.file.getvalue()
```

- [ ] **Step 3: Run the tests to verify the new/changed ones fail**

Run: `uv run pytest tests/test_cli.py tests/test_commands.py -v -k "bad_provider_is_config_error or not_in_the_builtin_choices_list or checks_a_custom_provider_via_the_given_registry or recognizes_a_custom_provider_from_config"`
Expected: FAIL — `test_bad_provider_is_config_error` gets `SystemExit` instead of a clean return (old `choices=` still in place), `test_provider_flag_accepts_a_provider_not_in_the_builtin_choices_list` also raises `SystemExit`, `test_has_api_key_checks_a_custom_provider_via_the_given_registry` fails with `TypeError: _has_api_key() takes 1 positional argument but 2 were given`, `test_provider_recognizes_a_custom_provider_from_config` fails with `TypeError: __init__() got an unexpected keyword argument 'custom_providers'`.

- [ ] **Step 4: Update `luna/cli.py`**

1. Change the import line:

```python
from luna.config.providers import PROVIDERS, LunaConfigError, merge_providers
```

2. Remove the `choices=` constraint on `--provider` (`build_parser`):

```python
    parser.add_argument("--provider", help="model provider")
```

3. Remove `choices=` on the two `config` subcommand provider args
   (`_config_parser`):

```python
    p_key = sub.add_parser("set-key", help="store an API key for a provider")
    p_key.add_argument("provider")
    p_key.add_argument("api_key", nargs="?")
    p_unset = sub.add_parser("unset-key", help="remove a stored API key")
    p_unset.add_argument("provider")
```

4. Make `_has_api_key` registry-aware:

```python
def _has_api_key(provider: str, registry: dict) -> bool:
    spec = registry[provider]
    if spec.env_var is None:
        return True
    return bool(os.environ.get(spec.env_var) or get_api_key(provider))
```

5. Update `_has_api_key`'s one call site inside `main()` — find:

```python
    if not _has_api_key(config.provider):
```

and replace with:

```python
    if not _has_api_key(config.provider, merge_providers(config.custom_providers)):
```

6. In `_run_config`, the `set-key`/`unset-key`/`show` branches need a
   merged registry built from the current project's config. Replace the
   `set-key` branch:

```python
    if args.cmd == "set-key":
        registry = merge_providers(load_config({}).custom_providers)
        key = args.api_key or getpass.getpass(f"{args.provider} API key: ")
        path = set_api_key(args.provider, key, registry=registry)
        console.print(f"stored {mask_key(key)} for {args.provider}  ->  {path}")
        return 0
```

and the `unset-key` branch:

```python
    if args.cmd == "unset-key":
        registry = merge_providers(load_config({}).custom_providers)
        removed = unset_api_key(args.provider, registry=registry)
        console.print(
            f"removed key for {args.provider}" if removed else f"no stored key for {args.provider}"
        )
        return 0
```

   `set_api_key`/`unset_api_key` are already imported at the top of
   `cli.py` (from `luna.config.credentials`) — no import change needed
   there.

7. `_run_config`'s `show` branch (`for name in PROVIDERS:`) is left
   unchanged — per the Task 4 scope decision, `config show` lists built-in
   providers' stored keys only, same as the setup wizard.

- [ ] **Step 5: Update `luna/repl/commands.py`**

In `_provider`, replace:

```python
    if arg not in PROVIDERS:
        ctx.console.print(f"[{PALETTE['mauve']}]unknown provider {arg!r}[/]")
        return None
    spec = PROVIDERS[arg]
```

with:

```python
    registry = merge_providers(ctx.config.custom_providers)
    if arg not in registry:
        ctx.console.print(f"[{PALETTE['mauve']}]unknown provider {arg!r}[/]")
        return None
    spec = registry[arg]
```

Update the import line at the top of the file:

```python
from luna.config.providers import PROVIDERS, LunaConfigError, merge_providers
```

(Keep `PROVIDERS` in the import if anything else in the file still uses it
directly — check with `grep -n "PROVIDERS" luna/repl/commands.py` before
removing it from the import list; only drop it if `_provider` was its only
user.)

- [ ] **Step 6: Update `luna/core/agent.py`'s `build_model` call**

Find:

```python
        model=model or build_model(config.provider, config.model, config.model_kwargs),
```

Replace with:

```python
        model=model
        or build_model(
            config.provider,
            config.model,
            config.model_kwargs,
            registry=merge_providers(config.custom_providers),
        ),
```

Update the import line at the top of `luna/core/agent.py`:

```python
from luna.config.providers import build_model, merge_providers
```

- [ ] **Step 7: Run every test touched by this task**

Run: `uv run pytest tests/test_cli.py tests/test_commands.py -v -k "provider or has_api_key"`
Expected: all pass.

- [ ] **Step 8: Run the full test suite**

Run: `uv run pytest -q`
Expected: all pass — this is where a stray remaining `choices=sorted(PROVIDERS)`
or an untouched `PROVIDERS[...]` lookup elsewhere in the REPL/CLI layer
would surface as a new failure.

- [ ] **Step 9: Lint**

Run: `uv run ruff check luna/cli.py luna/repl/commands.py luna/core/agent.py && uv run ruff format --check luna/cli.py luna/repl/commands.py luna/core/agent.py`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add luna/cli.py luna/repl/commands.py luna/core/agent.py tests/
git commit -m "feat: wire the merged provider registry through cli/repl/agent call sites"
```

---

### Task 7: Documentation + `pyproject.toml` URL fix + final verification

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing (prose + one metadata fix).
- Produces: nothing (terminal task).

- [ ] **Step 1: Update the provider table in `README.md`**

Find the existing provider table (starts `| Провайдер | ... |`) and replace
it with the full 13-row table:

```markdown
| Провайдер | `--provider` | Модель по умолчанию | Ключ | Extra |
| --- | --- | --- | --- | --- |
| Anthropic *(по умолчанию)* | `anthropic` | `claude-sonnet-4-5` | `ANTHROPIC_API_KEY` | (входит в базовую установку) |
| DeepSeek | `deepseek` | `deepseek-chat` | `DEEPSEEK_API_KEY` | `luna-simple[deepseek]` |
| OpenAI | `openai` | `gpt-4.1` | `OPENAI_API_KEY` | `luna-simple[openai]` |
| Google | `google` | `gemini-2.5-pro` | `GOOGLE_API_KEY` | `luna-simple[google]` |
| Ollama | `ollama` | `qwen2.5-coder` | — (локально) | `luna-simple[ollama]` |
| Mistral | `mistral` | `mistral-large-latest` | `MISTRAL_API_KEY` | `luna-simple[mistral]` |
| xAI (Grok) | `xai` | `grok-4` | `XAI_API_KEY` | `luna-simple[xai]` |
| Groq | `groq` | `openai/gpt-oss-120b` | `GROQ_API_KEY` | `luna-simple[groq]` |
| Fireworks | `fireworks` | `accounts/fireworks/models/qwen3p5-397b-a17b` | `FIREWORKS_API_KEY` | `luna-simple[fireworks]` |
| Together | `together` | `Qwen/Qwen2.5-Coder-32B-Instruct` | `TOGETHER_API_KEY` | `luna-simple[together]` |
| OpenRouter | `openrouter` | `anthropic/claude-sonnet-4-6` | `OPENROUTER_API_KEY` | `luna-simple[openrouter]` |
| Perplexity | `perplexity` | `sonar` | `PPLX_API_KEY` | `luna-simple[perplexity]` |
| Cerebras | `cerebras` | `gpt-oss-120b` | `CEREBRAS_API_KEY` | `luna-simple[openai]` |
```

Immediately after the table, add a new subsection:

```markdown
### Свой OpenAI-совместимый провайдер

Любой OpenAI-совместимый эндпоинт (vLLM, LM Studio, корпоративный прокси и
т. п.) можно подключить без изменения кода Luna — через `.luna.toml` или
`~/.config/luna/config.toml`:

```toml
[provider.custom.mylocal]
base_url = "http://localhost:8000/v1"
env_var = "MYLOCAL_API_KEY"
default_model = "local-model"  # необязательно
```

```bash
luna --provider mylocal "..."
luna config set-key mylocal   # спросит ключ скрытым вводом
```

Имя встроенного провайдера (`anthropic`, `openai`, ...) занять таким
способом нельзя — встроенная запись всегда побеждает при совпадении имён.
```

- [ ] **Step 2: Add a `CHANGELOG.md` entry**

Under the existing `## [Unreleased]` / `### Добавлено` section (create the
`### Добавлено` subsection if `[Unreleased]` currently only has
`### Изменено`/`### Исправлено` — check the file's current structure
first), add:

```markdown
- 8 новых провайдеров: Mistral, xAI (Grok), Groq, Fireworks, Together,
  OpenRouter, Perplexity (нативные интеграции LangChain) и Cerebras
  (через новый `base_url`-механизм) — реестр вырос с 5 до 13. Плюс
  пользовательские провайдеры: секция `[provider.custom.<name>]` в
  `.luna.toml`/`config.toml` подключает любой OpenAI-совместимый эндпоинт
  (vLLM, LM Studio, корпоративный прокси) без изменения кода Luna.
```

- [ ] **Step 3: Fix the `pyproject.toml` URLs**

Find:

```toml
[project.urls]
Homepage = "https://github.com/earendil-works/pi"
Repository = "https://github.com/earendil-works/pi"
```

Replace with:

```toml
[project.urls]
Homepage = "https://github.com/Dushe4ka/luna-simple"
Repository = "https://github.com/Dushe4ka/luna-simple"
```

- [ ] **Step 4: Run the full test suite, lint, and format check**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all clean — this is the final gate for the whole provider-expansion
plan across all 7 tasks.

- [ ] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md pyproject.toml
git commit -m "docs: document the 8 new providers and custom-endpoint support"
```
