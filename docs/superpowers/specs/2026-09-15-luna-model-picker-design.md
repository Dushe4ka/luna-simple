# Luna Model Picker — Design

## Purpose

Two related problems in `luna setup` / `/model`, both surfaced by a real
user session on Windows:

1. **Bug:** in the setup wizard, leaving the "model" field blank is meant
   to keep the provider's default model. Instead it silently keeps
   whatever value was typed there in an *earlier* run — including a
   garbage value (the user once pasted their API key into the model
   field by mistake). There is no way to recover except hand-editing
   `config.toml` or running `luna config set model.name <default>`
   manually. Root cause: `set_config_values` only ever adds/overwrites
   keys it's given; a blank model field simply omits `model.name` from
   the update, so a previously-set bad value is never cleared.
2. **Feature:** the user currently free-types a model id from memory.
   Instead, offer a pick-from-list UI (the same `arrow_pick` already used
   for provider selection), populated **live** from the provider's own
   API when possible, falling back to a small built-in list when it
   isn't (offline, closed network, provider has no such endpoint, or the
   request simply fails) — so the wizard never hangs or breaks in an
   air-gapped environment.

## Success Criteria

- Leaving the model field blank at any point always means "use the
  provider's default," full stop — overwriting whatever was there
  before, not just skipping the update.
- When a live model list is available, the user picks with arrow keys
  instead of typing a name from memory.
- When it isn't (no network, provider has no listing endpoint, request
  errors, or times out), the wizard falls back to a built-in static list
  for that provider — and if even that doesn't exist (an unknown/custom
  provider), falls back to today's free-text prompt, with the blank-means-
  default fix applied there too.
- None of this ever blocks longer than ~3 seconds waiting on a dead
  network — critical for the closed-network use case that prompted the
  request.
- `/model` with no argument in the REPL gets the same picker (the API
  key is already available in that context, no extra step needed).
- No new dependency — `httpx` is already an unconditional transitive
  dependency of the `anthropic` SDK, which is itself in Luna's
  unconditional base `dependencies` (not behind any extra), so it is
  always present regardless of which provider extras a user installed.

## Architecture

Two new pieces in `luna/config/`, plus changes to the two places that
prompt for a model.

### `luna/config/known_models.toml` (new)

Static, offline fallback list of model ids per built-in provider —
exactly the same shape and spirit as the existing `models.toml` pricing
registry, but listing *which ids exist* rather than *what they cost*:

```toml
[anthropic]
models = ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"]

[openai]
models = ["gpt-4.1", "gpt-4o", "gpt-5"]

# ... one [provider_key] table per built-in provider ...
```

No entry exists (or is possible) for user-defined `[provider.custom.*]`
providers — there is nothing to know offline about an arbitrary
endpoint. Exact model ids per provider are populated and verified against
each provider's own docs at implementation time (the same discipline the
provider-expansion plan used for default model ids — not guessed), and
will drift over time the same way `models.toml`'s prices do; that's an
accepted, inherent limitation of any hard-coded catalog and is exactly
why the live path is the primary source, not the only one.

### `luna/config/model_discovery.py` (new)

```python
def list_models(
    provider: str,
    spec: ProviderSpec,
    *,
    api_key: str | None,
    timeout: float = 3.0,
) -> list[str] | None:
    """Live model ids for `provider`, or None on any failure.

    Never raises — network errors, timeouts, non-200 responses, and
    malformed bodies all collapse to None, matching the existing
    best-effort convention in luna/config/usage.py. `api_key` may be
    None only for providers whose spec.env_var is already None
    (Ollama, or a keyless custom endpoint).
    """
```

Four response shapes, dispatched by provider identity / spec shape (not
guessed — verified against each provider's current docs):

| Case | Endpoint | Auth | Response shape |
|---|---|---|---|
| Anthropic | `GET https://api.anthropic.com/v1/models` | header `x-api-key: <key>`, `anthropic-version: 2023-06-01` | `{"data": [{"id": ...}], "has_more": ...}` |
| Google | `GET https://generativelanguage.googleapis.com/v1beta/models` | header `x-goog-api-key: <key>` | `{"models": [{"name": "models/gemini-..."}]}` |
| Ollama | `GET {OLLAMA_HOST or http://localhost:11434}/api/tags` | none | `{"models": [{"name": ...}]}` |
| Everyone else (OpenAI-compatible: openai, deepseek, groq, fireworks, together, openrouter, mistral, xai, perplexity, cerebras, any `[provider.custom.*]`) | `GET {base}/models`, where `base` is `spec.base_url` when set (Cerebras, custom) or a per-provider constant otherwise | header `Authorization: Bearer <key>` | `{"data": [{"id": ...}]}` |

The per-provider base-URL constants for the "everyone else" row (for the
providers that don't already carry `spec.base_url`) are verified against
each provider's own API docs at implementation time, the same way the
provider-expansion plan verified default model ids — not guessed here.

```python
def known_models(provider: str) -> list[str]:
    """Static fallback ids for `provider` from known_models.toml, or []
    when the provider has no entry (always true for custom providers)."""
```

Loaded and cached the same way `usage.py`'s `_load_registry()` caches
`models.toml` — read once per process, never re-read.

### `luna/repl/setup_wizard.py` (changed)

`run_setup`'s step order changes: **provider → API key → model** (today
it's provider → model → key). Almost every live-listing path needs the
key already in hand, so asking for the model first makes the live path
unusable on a first-ever run — which is exactly the run where a picker
helps the most.

New `_choose_model(console, input_fn, provider, spec, *, env=None) ->
str` replaces the current bare `input_fn(f"model [{default}]: ")` call:

1. Try `list_models(provider, spec, api_key=<the key just entered or
   already stored>, timeout=3.0)`.
2. `None` → try `known_models(provider)`.
3. Still empty → fall back to today's free-text prompt — **but now a
   blank answer always means "use `spec.default_model`," overwriting any
   previous value**, closing bug #1 regardless of which path a user ends
   up on.
4. Non-empty list (from either 1 or 2) → `arrow_pick` with the same
   value/label shape `_choose_provider` already uses, defaulting to
   `spec.default_model` when it's in the list (else the first entry).
   `arrow_pick` returning `None` (no real terminal) falls back to the
   existing numbered plain-text list, same pattern `_choose_provider`
   already has for its own non-interactive fallback.

The actual write path (`set_config_values({"model.name": ...})`) is
called unconditionally once `_choose_model` returns — including when it
returns the default, so a stale bad value is always overwritten, never
just left alone. This is the direct fix for bug #1: today's code only
calls `set_config_values` with `model.name` present when the typed value
differs from the default; the fix always includes it.

### `luna/repl/commands.py` (changed)

`/model` with no argument currently only prints the current value. It
gains the same picker: the session's provider/spec and stored/env API
key are already available (no key-entry step needed, unlike first-run
setup), so it goes straight to `_choose_model`'s live→known→text chain,
then rebuilds the agent with the picked model — mirroring how `/provider`
already rebuilds on a successful pick. `/model <name>` with an explicit
argument is unchanged (still a direct free-text override, useful for
scripting/muscle-memory).

## Data Flow

```
luna setup:
  pick provider → enter/confirm API key → _choose_model:
    list_models() [≤3s] --ok--> arrow_pick(live ids) → written
                  --fail--> known_models() --nonempty--> arrow_pick(static ids) → written
                                          --empty--> free-text (blank=default) → written

/model (REPL, no arg):
  _choose_model() using the session's already-active provider+key → same chain → agent rebuilt
```

## Error Handling

`list_models` never raises — every failure mode (timeout, connection
error, non-2xx status, JSON that doesn't match the expected shape)
collapses to `None`. No new exception type; this matches
`luna/config/usage.py`'s existing "best-effort, never raises" convention
verbatim, and it's the property that makes the whole feature safe to run
in a closed network by default with no configuration.

## Testing

Every test in this project runs with no network access, and this
feature must not be the exception. All four `list_models` adapters get
tests against a monkeypatched HTTP layer (the httpx call is made through
a small internal function so it's a single patch point) covering: a
realistic success response per shape, a timeout, a non-200 status, and a
malformed/unexpected body — all four asserting `None` except the success
case. `known_models()` gets a test against a small temp TOML fixture (or
the real file, read-only). `_choose_model`'s three-tier fallback gets
tests for each tier being reached (live ok / live fails+known ok /
both fail+text fallback), plus a dedicated regression test asserting a
blank text-fallback answer overwrites a previously-set bad `model.name`
in `config.toml` — the exact scenario that shipped the original bug.
`/model` with no argument gets a REPL-level test mirroring the existing
`_provider`/`_model` tests in `tests/test_commands.py`.

## Open Items for Implementation

- Per-provider base URLs for the "everyone else" OpenAI-compatible row
  (openai, deepseek, groq, fireworks, together, openrouter, mistral,
  xai, perplexity) — verify each against current provider docs before
  hard-coding, do not carry a guessed value from this design doc into
  code.
- Initial `known_models.toml` contents per provider — same verification
  discipline as the provider-expansion plan's default-model-id research.
- Some OpenAI-compatible providers may not actually implement a
  `/models` listing endpoint (not universal even though the request
  shape is standardized) — this is not a special case to design around;
  it's just another way `list_models` returns `None`, already covered by
  the existing fallback chain.
