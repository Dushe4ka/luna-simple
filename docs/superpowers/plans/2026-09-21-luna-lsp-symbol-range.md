# Luna LSP-Aware Edits (Symbol-Range Lookup) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fourth, read-only `lspnav` tool, `symbol_range`, that returns
a named symbol's exact current source text (via the language server's
document-symbol index) so the agent can use it as an unambiguous
`old_string` for `edit_file` — eliminating the "same substring appears
twice" failure mode, without touching the anchor/undo/approval pipeline
that already protects `edit_file` (roadmap item #2).

**Architecture:** One new tool function added to `luna/extensions/lspnav.py`
alongside the existing `goto_definition`/`find_references`/`hover`, reusing
the same `_server()`/error-message conventions. It calls
`multilspy`'s `request_document_symbols`, matches by name (and `line` when
ambiguous), and slices the requested lines out of the file's current text.
One line added to `LUNA_SYSTEM_PROMPT` nudging the agent to use it before
editing a named function/class/method. No new dependency, no interception
logic change — `edit_file` remains the only mutation path and is already
fully covered by `toolguard.py`.

**Tech Stack:** Python 3.11+, the already-installed `multilspy` (the `lsp`
extra, already a project dependency). Tests use `tests/test_lspnav.py`'s
established pattern: fast tests mock `_server`/`available`; one integration
test runs against a real Python file and is `pytest.mark.skipif`-guarded
on `multilspy` actually being installed, matching the existing
`test_goto_definition_runs_against_a_real_python_file`.

**Spec:** `docs/superpowers/specs/2026-09-21-luna-lsp-symbol-range-design.md`

## Global Constraints

- Python 3.11+, PEP 8 / PEP 257, clean `uv run ruff check .` /
  `uv run ruff format --check .`. D401 imperative-mood docstrings.
- `symbol_range` must never raise — every failure mode (LSP unavailable,
  no match, ambiguous match) returns a descriptive string, matching the
  existing three `lspnav` tools' convention exactly (see their `except
  Exception as exc: return f"LSP unavailable: {exc}"` pattern).
- No change to `luna/core/toolguard.py`, `AnchorTracker`, or
  `INTERRUPT_TOOLS`/`EXTENSION_INTERRUPTS` — this tool is read-only and
  must NOT be added to either interrupt dict, matching `goto_definition`/
  `find_references`/`hover`'s existing un-gated status.
- Line numbers in the tool's public signature are 1-based (matching
  `goto_definition`'s existing `line` parameter and `_find_column`'s
  convention in this same file); `multilspy`'s LSP responses are 0-based —
  convert deliberately and test the boundary.
- The actual JSON shape `multilspy.SyncLanguageServer.request_document_symbols`
  returns for a real Python file (via pyright) is NOT fully certain from
  reading the library's type stubs alone (`UnifiedSymbolInformation` has
  both a top-level `range` field, used for the hierarchical
  `DocumentSymbol` response shape, and a `location.range` field, used for
  the older flat `SymbolInformation` shape — which one pyright actually
  returns must be confirmed empirically in Task 1, not assumed).

---

### Task 1: `symbol_range` tool + tests

**Files:**
- Modify: `luna/extensions/lspnav.py`
- Test: `tests/test_lspnav.py` (extend the existing file)

**Interfaces:**
- Produces: a new `symbol_range` tool included in `make_tools(workdir,
  language)`'s returned list (alongside the existing three). Public
  signature: `symbol_range(file: str, symbol: str, line: int | None =
  None) -> str`.
- Consumes: nothing new from other tasks — this task is self-contained
  against the existing `lspnav.py` (`_server`, `available`,
  `detect_language` are unchanged and reused as-is).

- [ ] **Step 1: Confirm the real response shape empirically (not assumed)**

Before writing the implementation, run a scratch script from the repo
root (this mirrors how every prior plan in this project's `docs/superpowers/`
history validated framework behavior before writing code against it — do
not skip this):

```python
# scratchpad script, not part of the codebase
from multilspy import SyncLanguageServer
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger
import tempfile, os, textwrap

d = tempfile.mkdtemp()
with open(os.path.join(d, "a.py"), "w") as f:
    f.write(textwrap.dedent("""
        def foo():
            return 1


        def bar():
            return foo()
    """))
config = MultilspyConfig.from_dict({"code_language": "python"})
srv = SyncLanguageServer.create(config, MultilspyLogger(), d)
with srv.start_server():
    result = srv.request_document_symbols("a.py")
    print(result)
```

Run it (`uv run python scratch_lsp_probe.py` from a scratchpad location,
not committed to the repo) and inspect the printed structure: does each
symbol dict have a top-level `range` key with `start`/`end`/`line`, or is
it nested under `location.range`? Note the exact answer in your report —
the implementation in Step 3 below assumes a top-level `range` is present
(matching the hierarchical `DocumentSymbol` shape `request_document_symbols`
builds via `UnifiedSymbolInformation(**tree)` from a `children`-bearing
response, which is what pyright emits by default), with a fallback to
`location.range` if that assumption is wrong. If reality differs from
both assumptions, adjust the extraction code and clearly document the
actual shape you found — do not silently work around it without noting
it in the report.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_lspnav.py`:

```python
def test_symbol_range_without_multilspy_reports_unavailable(tmp_path, monkeypatch):
    import luna.extensions.lspnav as lspnav

    monkeypatch.setattr(lspnav, "available", lambda: False)
    tools = lspnav.make_tools(str(tmp_path), "python")
    assert tools == []


@pytest.mark.skipif(
    not __import__("luna.extensions.lspnav", fromlist=["available"]).available(),
    reason="multilspy not installed",
)
def test_symbol_range_returns_exact_source_text(tmp_path):
    from luna.extensions.lspnav import make_tools

    (tmp_path / "a.py").write_text(
        "def foo():\n    return 1\n\n\ndef bar():\n    return foo()\n"
    )
    tools = make_tools(str(tmp_path), "python")
    by_name = {t.name: t for t in tools}
    result = by_name["symbol_range"].invoke({"file": "a.py", "symbol": "bar"})
    assert "a.py" in result
    assert "def bar():" in result
    assert "return foo()" in result
    assert "def foo():" not in result


@pytest.mark.skipif(
    not __import__("luna.extensions.lspnav", fromlist=["available"]).available(),
    reason="multilspy not installed",
)
def test_symbol_range_reports_no_match(tmp_path):
    from luna.extensions.lspnav import make_tools

    (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
    tools = make_tools(str(tmp_path), "python")
    by_name = {t.name: t for t in tools}
    result = by_name["symbol_range"].invoke({"file": "a.py", "symbol": "does_not_exist"})
    assert "no symbol named" in result


@pytest.mark.skipif(
    not __import__("luna.extensions.lspnav", fromlist=["available"]).available(),
    reason="multilspy not installed",
)
def test_symbol_range_disambiguates_with_line(tmp_path):
    from luna.extensions.lspnav import make_tools

    (tmp_path / "a.py").write_text(
        "class A:\n"
        "    def foo(self):\n"
        "        return 1\n\n\n"
        "class B:\n"
        "    def foo(self):\n"
        "        return 2\n"
    )
    tools = make_tools(str(tmp_path), "python")
    by_name = {t.name: t for t in tools}
    result = by_name["symbol_range"].invoke({"file": "a.py", "symbol": "foo", "line": 7})
    assert "return 2" in result
    assert "return 1" not in result
```

(If Step 1's probe finds `request_document_symbols` doesn't return two
separate entries named `foo` the way this test expects — e.g. pyright
scopes method names differently — adjust this test to match the real,
observed behavior and note the adjustment in your report. The scenario
being tested — disambiguating identically-named symbols by line — is the
requirement; the exact fixture may need adjusting to actually produce
that condition against the real language server.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lspnav.py -v -k symbol_range`
Expected: FAIL — `AttributeError` or `KeyError`, no `symbol_range` tool exists yet.

- [ ] **Step 4: Implement `symbol_range` in `luna/extensions/lspnav.py`**

Add inside `make_tools`, alongside the existing three `@tool` closures
(exact extraction logic depends on Step 1's findings — this is the
baseline implementation assuming a top-level `range` field; adjust per
your empirical finding):

```python
    @tool
    def symbol_range(file: str, symbol: str, line: int | None = None) -> str:
        """Return the exact current source text of `symbol` in `file`.

        Resolved via the language server's document-symbol index — use
        this instead of guessing at old_string for edit_file when the
        symbol name might otherwise match more than one location. Pass
        `line` (1-based) to pick a specific match when the name is
        ambiguous.
        """
        srv = _server(workdir, language)
        if srv is None:
            return "LSP unavailable"
        try:
            symbols, _tree = srv.request_document_symbols(file)
        except Exception as exc:  # noqa: BLE001
            return f"LSP unavailable: {exc}"
        matches = [s for s in symbols if s.get("name") == symbol]
        if not matches:
            return f"no symbol named '{symbol}' found in {file}"
        if len(matches) > 1:
            if line is None:
                spans = ", ".join(
                    f"{_span(s)[0] + 1}-{_span(s)[1] + 1}" for s in matches if _span(s)
                )
                return (
                    f"multiple symbols named '{symbol}' found in {file} "
                    f"(lines {spans}) — pass line= to disambiguate"
                )
            chosen = None
            for s in matches:
                span = _span(s)
                if span and span[0] <= (line - 1) <= span[1]:
                    chosen = s
                    break
            if chosen is None:
                return f"no symbol named '{symbol}' found containing line {line} in {file}"
        else:
            chosen = matches[0]
        span = _span(chosen)
        if span is None:
            return f"'{symbol}' in {file} has no range information"
        start_line, end_line = span
        try:
            lines = (Path(workdir) / file).read_text().splitlines()
        except OSError as exc:
            return f"LSP unavailable: {exc}"
        text = "\n".join(lines[start_line : end_line + 1])
        return f"{file}:{start_line + 1}-{end_line + 1}\n{text}"
```

Add a small module-level helper above `make_tools` (used by the closure
above):

```python
def _span(symbol: dict) -> tuple[int, int] | None:
    """Return a symbol's 0-based (start_line, end_line), or None if absent."""
    rng = symbol.get("range") or (symbol.get("location") or {}).get("range")
    if not rng:
        return None
    return rng["start"]["line"], rng["end"]["line"]
```

Add `symbol_range` to `make_tools`'s returned list:
`return [goto_definition, find_references, hover, symbol_range]`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_lspnav.py -v`
Expected: all pass, including every pre-existing test in the file.

- [ ] **Step 6: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check luna/extensions/lspnav.py tests/test_lspnav.py && uv run ruff format --check luna/extensions/lspnav.py tests/test_lspnav.py`
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add luna/extensions/lspnav.py tests/test_lspnav.py
git commit -m "feat: symbol_range LSP tool for unambiguous edit_file old_string lookup"
```

---

### Task 2: System prompt nudge + docs + final verification

**Files:**
- Modify: `luna/config/prompts.py`
- Modify: `AGENTS.md` (the `luna/extensions/` listing, if `lspnav.py` is
  described there — check the actual current text before editing; add a
  clause about `symbol_range` to whatever line already describes
  `lspnav.py`'s tools, or add one if none exists yet)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 1's `symbol_range` tool (referenced by name only, no
  code dependency).
- Produces: nothing (terminal task).

- [ ] **Step 1: Add the system-prompt nudge**

In `luna/config/prompts.py`, the existing line (last line of the `Rules:`
section):

```
- When a symbol's definition or callers matter, prefer goto_definition /
  find_references over grepping, if they're available.
```

Replace with:

```
- When a symbol's definition or callers matter, prefer goto_definition /
  find_references over grepping, if they're available. Before editing a
  named function, class, or method, prefer symbol_range to get its exact
  current text as old_string, if available — safer than guessing at a
  substring that might match more than once.
```

- [ ] **Step 2: Check and update `AGENTS.md`**

Read `AGENTS.md`'s description of `luna/extensions/lspnav.py` (search for
`lspnav` in the file). Update whatever line documents its tool list to
mention `symbol_range` alongside `goto_definition`/`find_references`/
`hover`. Use the file's actual current wording as the basis for the
edit — do not assume the exact line text without reading it first.

- [ ] **Step 3: Add a CHANGELOG entry**

Add to `CHANGELOG.md`'s `## [Unreleased]` → `### Добавлено` subsection
(read the file first to confirm this subsection still exists and find its
current last bullet — add immediately after it, same convention as prior
entries this cycle):

```markdown
- Новый LSP-инструмент `symbol_range`: возвращает точный текущий текст
  символа (функции/класса/метода) по данным language server'а — чтобы
  `edit_file` можно было безопасно нацелить на конкретное вхождение,
  когда имя или часть тела символа встречается в файле более одного раза.
  Доступен на тех же условиях, что и goto_definition/find_references/hover.
```

- [ ] **Step 4: Run the full verification gate**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all clean — final gate for the whole feature.

- [ ] **Step 5: Commit**

```bash
git add luna/config/prompts.py AGENTS.md CHANGELOG.md
git commit -m "docs: document symbol_range LSP tool"
```
