"""Built-in web search: a provider-agnostic tool with automatic fallback.

Ported in *spirit*, not literally — see below — from Hermes Agent's
(``NousResearch/hermes-agent``, MIT-licensed, checked directly) pluggable
web search: try whichever backend has a configured API key, in priority
order, and fall back to a free, keyless multi-engine search when none
does, so ``web_search`` works with zero configuration on a fresh install —
the same reasoning OpenClaw and Hermes both use for their own default.

Hermes's actual provider implementations were not importable as-is: they
are built against its own multi-tenant *gateway* internals — a plugin
registry, per-request "secret scope" isolation so one shared process can
serve many users' credentials without cross-contamination, and a
disposable-subprocess search worker specifically to defend against
``ddgs``'s underlying HTTP client blocking the interpreter's GIL badly
enough that a plain thread-pool timeout can't fire. None of that applies
to Luna, a single-user local CLI/TUI with no multi-tenant concept at all.
What's reused here is the *design* (provider list + auto-detect + keyless
fallback) plus one concrete lesson taken from their code: bound every
``ddgs`` call with its own timeout, since the library's retry loop has no
overall cap of its own.
"""

from __future__ import annotations

import os

import httpx
from langchain_core.tools import tool

_TIMEOUT_SECONDS = 15.0
_DDGS_TIMEOUT_SECONDS = 10.0


def _search_tavily(query: str, max_results: int) -> list[dict[str, str]] | None:
    """Tavily (TAVILY_API_KEY) — the same backend OpenCode points users at."""
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return None
    resp = httpx.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"query": query, "max_results": max_results},
        timeout=_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in results[:max_results]
    ]


def _search_brave(query: str, max_results: int) -> list[dict[str, str]] | None:
    """Brave Search (BRAVE_API_KEY)."""
    api_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not api_key:
        return None
    resp = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        params={"q": query, "count": max_results},
        timeout=_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    results = resp.json().get("web", {}).get("results", [])
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
        for r in results[:max_results]
    ]


def _search_ddgs(query: str, max_results: int) -> list[dict[str, str]] | None:
    """Free, keyless fallback (optional ``ddgs`` package).

    The last resort, so ``web_search`` works out of the box with no API
    key at all.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return None
    with DDGS(timeout=_DDGS_TIMEOUT_SECONDS) as client:
        hits = list(client.text(query, max_results=max_results))
    return [
        {
            "title": h.get("title", ""),
            "url": h.get("href") or h.get("url", ""),
            "snippet": h.get("body", ""),
        }
        for h in hits[:max_results]
    ]


def _format_results(query: str, source: str, results: list[dict[str, str]]) -> str:
    if not results:
        return f"No results for {query!r} (via {source})."
    lines = [f"Results for {query!r} (via {source}):"]
    for r in results:
        lines.append(f"- {r['title']} — {r['url']}\n  {r['snippet']}")
    return "\n".join(lines)


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web and return the top results (title, URL, snippet).

    Automatically uses whichever backend is configured — Tavily
    (``TAVILY_API_KEY``) or Brave (``BRAVE_API_KEY``), tried in that order
    — and falls back to a free, keyless search needing no setup at all if
    neither is configured. Use this for anything beyond your training
    knowledge: current events, library/API versions, recent releases,
    live documentation.
    """
    max_results = max(1, min(int(max_results), 10))
    errors: list[str] = []
    # Built fresh on every call (not a module-level constant) so tests can
    # monkeypatch `_search_tavily`/`_search_brave`/`_search_ddgs` on this
    # module and have that override actually take effect — a constant
    # tuple built once at import time would keep pointing at the original
    # function objects forever, since a tuple element doesn't do a fresh
    # by-name lookup the way a bare name reference in a function body does.
    # Priority: paid/keyed backends first (better result quality), the
    # free keyless one last — mirrors Hermes's own auto-detect-from-
    # available-key ordering.
    providers = (
        ("tavily", _search_tavily),
        ("brave", _search_brave),
        ("duckduckgo", _search_ddgs),
    )
    for name, backend in providers:
        try:
            results = backend(query, max_results)
        except Exception as exc:  # noqa: BLE001 - try the next backend instead of failing the turn
            errors.append(f"{name}: {exc}")
            continue
        if results is None:
            continue  # not configured (no key) or not installed (ddgs)
        return _format_results(query, name, results)
    if errors:
        return "Web search failed on every configured backend: " + "; ".join(errors)
    return (
        "No web search backend is available: set TAVILY_API_KEY or BRAVE_API_KEY, "
        'or run `pip install ddgs` (or `pip install "luna-simple[websearch]"`) '
        "for a free, keyless fallback."
    )
