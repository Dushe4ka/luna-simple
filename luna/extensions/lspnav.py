"""Optional LSP-backed navigation tools: goto_definition, find_references, hover.

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
    except (OSError, IndexError):
        return 0


def make_tools(workdir: str, language: str) -> list:
    """Build the three navigation tools bound to ``workdir``/``language``."""
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

    return [goto_definition, find_references, hover]
