# Luna LSP-Aware Edits (Symbol-Range Lookup) — Design

## Purpose

Roadmap item #4 (of the 1→2→4→3 order agreed during the competitive-analysis
session), explicitly deferred until #2 (anchor-based edits) shipped: "LSP
даёт координаты символа → анкор фиксирует правку." Luna's `edit_file`
matches `old_string` as a plain substring — if that string occurs more than
once in a file, the replacement is ambiguous and may land on the wrong
occurrence. Anchor-based edits (#2) protect against *stale* content; this
item protects against *ambiguous* matches by letting the agent locate a
symbol's exact source range via the Language Server Protocol before editing.

## Existing Foundation (verified directly against the installed package,
not assumed)

- `luna/extensions/lspnav.py` already wraps `multilspy` and exposes three
  read-only tools — `goto_definition`, `find_references`, `hover` — wired
  into `luna/core/agent.py` only when a supported project language is
  detected (`lspnav.detect_language`) and the `lsp` extra is installed
  (`lspnav.available()`). The module's own docstring states these tools are
  "read-only, never gated by approval" — a design invariant this work
  preserves.
- `multilspy.SyncLanguageServer` (checked directly via `inspect` against the
  installed package, not guessed) exposes `request_document_symbols(path)`,
  returning a list of `UnifiedSymbolInformation` dicts. Each has a `name`,
  a `range` ("the range enclosing this symbol not including
  leading/trailing whitespace but everything else like comments" — i.e.
  the symbol's full body extent, not just its name position), and a
  `selectionRange` (just the name). This `range` is exactly the coordinate
  data the roadmap note refers to.
- `edit_file` (deepagents built-in) is already the sole mutation path
  covered by `luna/core/toolguard.py`'s anchor-staleness check (#2),
  undo-journal snapshotting, and (unless `yolo`) human approval. This work
  deliberately does not bypass that pipeline.

## Success Criteria

- The agent can ask, for a named symbol in a file, "what is its exact
  current source range and text" — precise enough that using that text as
  `edit_file`'s `old_string` cannot match the wrong occurrence, even when
  the symbol's name or a substring of its body appears elsewhere in the
  file.
- The new capability is a read-only lookup, not a new mutation path: it
  never writes to disk itself. The actual edit still goes through the
  existing `edit_file` tool, so it inherits anchor-staleness checking,
  undo-journal snapshotting, and approval gating automatically, with zero
  changes to `luna/core/toolguard.py`.
- Available under the exact same conditions as the existing `lspnav` tools
  (language detected, `multilspy` installed) — no new extra, no new
  configuration surface.
- Degrades gracefully: an unresolvable symbol, a missing language server,
  or any LSP error returns a clear string message (matching the existing
  `goto_definition`/`find_references`/`hover` error-message convention),
  never an exception.
- The system prompt nudges the agent to prefer this over a blind
  `edit_file` guess when it is about to modify a named function/class/
  method and the tool is available — mirroring the existing nudge for
  `goto_definition`/`find_references`.

## Architecture

### `luna/extensions/lspnav.py` (modified — one new tool added to
`make_tools`, alongside the existing three; no changes to the module's
read-only/no-approval invariant)

```python
def symbol_range(file: str, symbol: str, line: int | None = None) -> str:
    """Return the exact current source text of `symbol` in `file`, with its
    line range, resolved via the language server's document-symbol index —
    for use as edit_file's old_string when the symbol name might otherwise
    match more than one location.
    """
```

Behavior:
1. Call `srv.request_document_symbols(file)`.
2. Filter to entries whose `name == symbol`. If none match, return
   `"no symbol named '<symbol>' found in <file>"`.
3. If more than one matches and `line` was given, pick the one whose
   `range` contains `line` (1-based, converted to the LSP's 0-based
   lines for comparison); if `line` was not given and there are multiple
   matches, return a message listing each match's line range and asking
   the caller to disambiguate with `line` — never silently guess.
4. Read the file's current text (via `Path(workdir) / file`, matching the
   existing `_find_column` pattern already in this file) and slice out
   the exact lines spanned by the chosen symbol's `range`
   (`start.line`..`end.line`, inclusive, converted to 1-based for
   slicing).
5. Return a string of the form:
   `"<file>:<start_line>-<end_line>\n<exact source text of that range>"`
   so the agent can copy the text portion verbatim as `edit_file`'s
   `old_string`.

Any exception from the LSP call (matching the existing pattern in this
file's other three tools) is caught and turned into a
`"LSP unavailable: <exc>"` string — never raised.

`make_tools` gains this as a fourth `@tool`-decorated closure, appended to
its returned list. No change to `available()`, `detect_language()`, or
`_server()`.

### `luna/config/prompts.py` (modified — one line added)

Alongside the existing line "When a symbol's definition or callers matter,
prefer goto_definition / find_references over grepping, if they're
available," add: a note that before editing a named function/class/method,
prefer `symbol_range` to get an unambiguous `old_string` when the tool is
available.

## Data Flow

```
agent wants to edit function `foo` in a.py
  → symbol_range("a.py", "foo")
      → request_document_symbols("a.py")
      → find entry named "foo", read its `range`
      → slice a.py's current text to that range
      → return "a.py:12-18\ndef foo():\n    ...\n"
  → agent calls edit_file(file_path="a.py",
                           old_string="def foo():\n    ...\n",
                           new_string=<new body>)
      → toolguard.py's existing anchor check + undo snapshot + approval
        gate all apply exactly as they already do — no change there.
```

## Error Handling

Matches the existing three `lspnav` tools' convention exactly: no
exceptions ever escape the tool; every failure mode (LSP unavailable, no
matching symbol, ambiguous match without a disambiguating `line`) returns
a descriptive string. A stale read (the file changed on disk since the
LSP indexed it) is not specially handled here — if the returned text no
longer matches disk by the time `edit_file` runs, the *existing* anchor
check (#2) or deepagents' own `old_string`-not-found check already catches
that; this feature does not duplicate that protection.

## Testing

Extends `tests/test_lspnav.py`'s existing convention (checked before
writing the implementation plan, not assumed) for mocking
`multilspy`/`_server`. Covers: exact single-match lookup returns correct
range and text; no match found; multiple matches without a disambiguating
`line` lists them without guessing; multiple matches with a `line` picks
the containing one; an LSP exception returns the `"LSP unavailable"`
string rather than raising; `multilspy` not installed still returns an
empty tool list from `make_tools` (existing behavior, unaffected).

## Out of Scope

- Any change to `toolguard.py`, `AnchorTracker`, or the edit-approval
  pipeline — this feature is purely a smarter way to compute `old_string`,
  not a new mutation path.
- `rename` (renaming a symbol and updating every call site) — a much
  larger feature (would need `find_references` plus a multi-file edit
  transaction) and not requested; may be a future roadmap item.
- Editing via LSP's own `insert_text_at_position`/
  `delete_text_between_positions` primitives (which `multilspy` does
  expose) — using those would bypass Luna's anchor/undo/approval pipeline
  entirely, which is explicitly not acceptable.
