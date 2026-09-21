"""Optional LSP-backed navigation tools: goto_definition, find_references, hover, symbol_range.

Requires the ``lsp`` extra (``multilspy``). Every function degrades to a plain
string message instead of raising when the extra is missing or the server
fails to start — these are read-only tools, never gated by approval.
"""

from __future__ import annotations

import atexit
from pathlib import Path

from langchain_core.tools import tool

try:
    from multilspy import SyncLanguageServer
    from multilspy.multilspy_config import MultilspyConfig
    from multilspy.multilspy_logger import MultilspyLogger

    _HAVE_MULTILSPY = True
except ImportError:  # pragma: no cover - exercised when the extra is absent
    _HAVE_MULTILSPY = False

_LANGUAGE_MARKERS: dict[str, str] = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "tsconfig.json": "typescript",
    "package.json": "javascript",
    "go.mod": "go",
    "Cargo.toml": "rust",
}

_SERVERS: dict[tuple[str, str], object] = {}


def available() -> bool:
    """Whether the ``multilspy`` extra is installed."""
    return _HAVE_MULTILSPY


def detect_language(workdir: str) -> str | None:
    """Guess the project's language from common marker files."""
    root = Path(workdir)
    for marker, language in _LANGUAGE_MARKERS.items():
        if (root / marker).is_file():
            return language
    return None


def _server(workdir: str, language: str):
    key = (str(Path(workdir).resolve()), language)
    if key in _SERVERS:
        return _SERVERS[key]
    if not _HAVE_MULTILSPY:
        _SERVERS[key] = None
        return None
    try:
        config = MultilspyConfig.from_dict({"code_language": language})
        srv = SyncLanguageServer.create(config, MultilspyLogger(), key[0])
        ctx = srv.start_server()
        ctx.__enter__()
        atexit.register(lambda: ctx.__exit__(None, None, None))
    except Exception:  # noqa: BLE001 - a broken LSP server must not break Luna
        srv = None
    _SERVERS[key] = srv
    return srv


def _find_column(workdir: str, file: str, line: int, symbol: str) -> int:
    try:
        text = (Path(workdir) / file).read_text()
        target = text.splitlines()[line - 1]
        return max(0, target.find(symbol))
    except (OSError, IndexError, UnicodeDecodeError):
        return 0


def _span(symbol: dict) -> tuple[int, int] | None:
    """Return a symbol's 0-based (start_line, end_line), or None if absent."""
    rng = symbol.get("range") or (symbol.get("location") or {}).get("range")
    if not rng:
        return None
    return rng["start"]["line"], rng["end"]["line"]


def make_tools(workdir: str, language: str) -> list:
    """Build the four navigation tools bound to ``workdir``/``language``."""
    if not available():
        return []

    @tool
    def goto_definition(file: str, line: int, symbol: str) -> str:
        """Find where ``symbol`` (on 1-based ``line`` of ``file``) is defined."""
        srv = _server(workdir, language)
        if srv is None:
            return "LSP unavailable"
        col = _find_column(workdir, file, line, symbol)
        try:
            results = srv.request_definition(file, line - 1, col)
        except Exception as exc:  # noqa: BLE001 - a tool must report, not crash
            return f"LSP unavailable: {exc}"
        if not results:
            return "no definition found"
        return "\n".join(
            f"{r.get('relativePath', '?')}:{r['range']['start']['line'] + 1}" for r in results
        )

    @tool
    def find_references(file: str, line: int, symbol: str) -> str:
        """Find callers/usages of ``symbol`` (on 1-based ``line`` of ``file``)."""
        srv = _server(workdir, language)
        if srv is None:
            return "LSP unavailable"
        col = _find_column(workdir, file, line, symbol)
        try:
            results = srv.request_references(file, line - 1, col)
        except Exception as exc:  # noqa: BLE001
            return f"LSP unavailable: {exc}"
        if not results:
            return "no references found"
        return "\n".join(
            f"{r.get('relativePath', '?')}:{r['range']['start']['line'] + 1}" for r in results
        )

    @tool
    def hover(file: str, line: int, symbol: str) -> str:
        """Show type/doc info for ``symbol`` (on 1-based ``line`` of ``file``)."""
        srv = _server(workdir, language)
        if srv is None:
            return "LSP unavailable"
        col = _find_column(workdir, file, line, symbol)
        try:
            result = srv.request_hover(file, line - 1, col)
        except Exception as exc:  # noqa: BLE001
            return f"LSP unavailable: {exc}"
        if not result:
            return "no hover info"
        contents = result.get("contents", "")
        return contents.get("value", str(contents)) if isinstance(contents, dict) else str(contents)

    @tool
    def symbol_range(file: str, symbol: str, line: int | None = None) -> str:
        """Return the exact current source text of `symbol` in `file`.

        Resolved via the language server's document-symbol index — use
        this instead of guessing at old_string for edit_file when the
        symbol name might otherwise match more than one location. Pass
        `line` (1-based) to pick a specific match when the name is
        ambiguous.

        The first line of the response is a `file:start-end` header, not
        source text — pass only the lines after it as old_string. If two
        symbols have identical bodies, the returned text may still match
        more than one place in the file; if edit_file reports multiple
        occurrences, widen old_string with a line of surrounding context
        instead of reaching for replace_all, which would change both.
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
        except (OSError, UnicodeDecodeError) as exc:
            return f"LSP unavailable: {exc}"
        text = "\n".join(lines[start_line : end_line + 1])
        return f"{file}:{start_line + 1}-{end_line + 1}\n{text}"

    return [goto_definition, find_references, hover, symbol_range]
