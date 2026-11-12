"""Text I/O in Luna must name its encoding: Windows defaults to the ANSI code page."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [ROOT / "luna", ROOT / "packages" / "luna-core" / "src" / "luna_core"]
TEXT_METHODS = {"read_text", "write_text"}
SUBPROCESS = {"run", "Popen", "check_output", "check_call", "call"}


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        has_encoding = _kw(node, "encoding") is not None
        where = f"{path.relative_to(ROOT)}:{node.lineno}"
        if name in TEXT_METHODS and not has_encoding:
            positional = node.args[1:] if name == "write_text" else node.args
            if not positional:
                out.append(f"{where} {name} without encoding")
        if name == "open" and isinstance(func, ast.Name):
            mode = node.args[1] if len(node.args) > 1 else _kw(node, "mode")
            binary = isinstance(mode, ast.Constant) and "b" in str(mode.value)
            if not binary and not has_encoding:
                out.append(f"{where} open() without encoding")
        if name in SUBPROCESS and not has_encoding:
            if _is_true(_kw(node, "text")) or _is_true(_kw(node, "universal_newlines")):
                out.append(f"{where} subprocess text=True without encoding")
    return out


def test_no_text_io_without_encoding():
    offences = [o for src in SOURCES for p in sorted(src.rglob("*.py")) for o in _offences(p)]
    assert offences == []


def _spawns_without_stdin(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        func = node.func if isinstance(node, ast.Call) else None
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        spawn = (func.value.id == "subprocess" and func.attr in SUBPROCESS) or (
            func.value.id == "asyncio" and func.attr.startswith("create_subprocess_")
        )
        if spawn and _kw(node, "stdin") is None:
            out.append(f"{path.relative_to(ROOT)}:{node.lineno} {func.attr} without stdin")
    return out


def test_no_child_process_inherits_stdin():
    """A child inheriting Luna's stdin hangs on Windows and can eat ACP input.

    Under ``luna acp`` stdin is the editor's JSON-RPC pipe, read by a thread with a
    blocking synchronous read. On Windows a child that inherits that pipe blocks at
    startup until the read completes (``git`` in the undo journal hung the ACP
    session); on every OS a child that reads stdin would consume the editor's
    messages. Every spawn must say what its stdin is.
    """
    offences = [
        o for src in SOURCES for p in sorted(src.rglob("*.py")) for o in _spawns_without_stdin(p)
    ]
    assert offences == []
