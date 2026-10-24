"""Filesystem tools confined to the workspace."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Literal

from luna_core.messages import Image
from luna_core.tools.base import ToolContext, ToolOutput, tool
from luna_core.workspace import IGNORED_DIRS

_RG = shutil.which("rg")
READ_LIMIT = 100
READ_BUDGET = 40_000
LINE_CLIP = 2_000
MAX_RESULTS = 200
_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _seen(ctx: ToolContext) -> set[str]:
    return ctx.state.setdefault("seen", set())


@tool()
async def ls(ctx: ToolContext, path: str = ".") -> ToolOutput:
    """List a directory (directories end with /).

    Args:
        ctx: Tool context (hidden from the schema).
        path: Directory relative to the project root.

    """
    target = ctx.workspace.resolve(path)
    if not target.is_dir():
        return ToolOutput.error(f"not a directory: {path}")

    def run() -> list[str]:
        rows = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            rel = ctx.workspace.rel(child)
            rows.append(rel + "/" if child.is_dir() else rel)
        return rows

    rows = await asyncio.to_thread(run)
    return ToolOutput.text("\n".join(rows) or "(empty directory)", f"{len(rows)} entries")


@tool()
async def read_file(
    ctx: ToolContext, file_path: str, offset: int = 0, limit: int = READ_LIMIT
) -> ToolOutput:
    """Read a text file with line numbers, or an image.

    Args:
        ctx: Tool context (hidden from the schema).
        file_path: File relative to the project root.
        offset: First line to read (0-based).
        limit: Maximum number of lines.

    """
    target = ctx.workspace.resolve(file_path)
    rel = ctx.workspace.rel(target)
    if not target.is_file():
        return ToolOutput.error(f"file not found: {rel}")
    media = _IMAGE_TYPES.get(target.suffix.lower())
    data = await asyncio.to_thread(target.read_bytes)
    if media:
        _seen(ctx).add(rel)
        return ToolOutput((Image(media, base64.b64encode(data).decode()),), False, f"image {rel}")
    if b"\x00" in data[:8192]:
        return ToolOutput.error(f"{rel} is a binary file")
    lines = data.decode("utf-8", errors="replace").splitlines()
    start = max(0, offset)
    out: list[str] = []
    used = 0
    end = start
    for number in range(start, min(len(lines), start + max(1, limit))):
        line = lines[number]
        if len(line) > LINE_CLIP:
            line = line[:LINE_CLIP] + " … [line clipped]"
        row = f"{number + 1:>6}\t{line}"
        if used + len(row) > READ_BUDGET and out:
            break
        out.append(row)
        used += len(row) + 1
        end = number + 1
    _seen(ctx).add(rel)
    if not lines:
        return ToolOutput.text("(empty file)", rel)
    if start >= len(lines):
        return ToolOutput.text(
            f"(offset {start} is past the end: the file has only {len(lines)} lines)",
            f"{rel} ({len(lines)} lines)",
        )
    if end < len(lines):
        out.append(f"… {len(lines) - end} more lines; read again with offset={end}")
    return ToolOutput.text("\n".join(out), f"{rel} {start + 1}-{end}")


@tool(mutating=True)
async def write_file(ctx: ToolContext, file_path: str, content: str) -> ToolOutput:
    """Create a file, or overwrite one you have already read.

    Args:
        ctx: Tool context (hidden from the schema).
        file_path: File relative to the project root.
        content: Full new content.

    """
    target = ctx.workspace.resolve(file_path)
    rel = ctx.workspace.rel(target)
    if target.is_dir():
        return ToolOutput.error(f"{rel} is a directory")
    if target.exists() and rel not in _seen(ctx):
        return ToolOutput.error(
            f"{rel} already exists — read it first, then edit_file or write_file"
        )

    def run() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    await asyncio.to_thread(run)
    _seen(ctx).add(rel)
    return ToolOutput.text(f"wrote {rel} ({len(content.splitlines())} lines)", f"wrote {rel}")


@tool(mutating=True)
async def edit_file(
    ctx: ToolContext, file_path: str, old_string: str, new_string: str, replace_all: bool = False
) -> ToolOutput:
    """Replace exact text in a file.

    Args:
        ctx: Tool context (hidden from the schema).
        file_path: File relative to the project root.
        old_string: Exact text to replace; must be unique unless replace_all.
        new_string: Replacement text.
        replace_all: Replace every occurrence.

    """
    target = ctx.workspace.resolve(file_path)
    rel = ctx.workspace.rel(target)
    if not target.is_file():
        return ToolOutput.error(f"file not found: {rel}")
    if old_string == new_string:
        return ToolOutput.error("old_string and new_string are identical")
    text = await asyncio.to_thread(target.read_text, encoding="utf-8")
    count = text.count(old_string) if old_string else 0
    if count == 0:
        return ToolOutput.error(f"old_string not found in {rel}")
    if count > 1 and not replace_all:
        return ToolOutput.error(
            f"old_string occurs {count} times in {rel}; "
            "add context to make it unique or set replace_all"
        )
    updated = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    await asyncio.to_thread(target.write_text, updated, encoding="utf-8")
    _seen(ctx).add(rel)
    n = count if replace_all else 1
    return ToolOutput.text(f"edited {rel} ({n} replacement{'s' if n > 1 else ''})", f"edited {rel}")


@tool(mutating=True)
async def delete(ctx: ToolContext, file_path: str) -> ToolOutput:
    """Delete a file or directory.

    Args:
        ctx: Tool context (hidden from the schema).
        file_path: Path relative to the project root.

    """
    target = ctx.workspace.resolve(file_path)
    if target == ctx.workspace.root:
        return ToolOutput.error("refusing to delete the project root")
    rel = ctx.workspace.rel(target)
    if not target.exists():
        return ToolOutput.error(f"not found: {rel}")
    if target.is_dir() and not target.is_symlink():
        await asyncio.to_thread(shutil.rmtree, target)
    else:
        await asyncio.to_thread(target.unlink)
    _seen(ctx).discard(rel)
    return ToolOutput.text(f"deleted {rel}", f"deleted {rel}")


@tool()
async def glob(ctx: ToolContext, pattern: str, path: str = ".") -> ToolOutput:
    """Find files by glob pattern, newest first.

    Args:
        ctx: Tool context (hidden from the schema).
        pattern: Glob such as "**/*.py".
        path: Directory to search from.

    """
    base = ctx.workspace.resolve(path)

    def run() -> list[str]:
        hits = []
        for p in base.glob(pattern):
            if not p.is_file():
                continue
            try:
                rel = ctx.workspace.rel(p)
            except ValueError:
                continue  # outside the workspace (e.g. a ".." pattern) — silently skip
            if not ctx.workspace.ignored(rel):
                hits.append((p.stat().st_mtime, rel))
        hits.sort(key=lambda t: (-t[0], t[1]))
        return [rel for _, rel in hits]

    hits = await asyncio.to_thread(run)
    more = f"\n… {len(hits) - MAX_RESULTS} more" if len(hits) > MAX_RESULTS else ""
    return ToolOutput.text(
        "\n".join(hits[:MAX_RESULTS]) + more if hits else "no matches", f"{len(hits)} files"
    )


@tool()
async def grep(
    ctx: ToolContext,
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches",
    max_count: int | None = None,
) -> ToolOutput:
    """Search file contents with a regular expression.

    Args:
        ctx: Tool context (hidden from the schema).
        pattern: Regular expression.
        path: File or directory to search.
        glob: Only files matching this glob (e.g. "*.py").
        output_mode: files_with_matches, content (path:line:text) or count.
        max_count: Maximum matches per file.

    """
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolOutput.error(f"invalid regex: {exc}")
    base = ctx.workspace.resolve(path)
    if _RG:
        lines = await _rg(ctx, pattern, base, glob, output_mode, max_count)
    else:
        lines = await asyncio.to_thread(_py_grep, ctx, regex, base, glob, output_mode, max_count)
    more = f"\n… {len(lines) - MAX_RESULTS} more" if len(lines) > MAX_RESULTS else ""
    return ToolOutput.text(
        "\n".join(lines[:MAX_RESULTS]) + more if lines else "no matches", f"{len(lines)} results"
    )


async def _rg(
    ctx: ToolContext, pattern: str, base: Path, glob_: str | None, mode: str, max_count: int | None
) -> list[str]:
    argv = [_RG, "--no-heading", "--with-filename", "--color", "never", "--hidden"]
    argv += {"files_with_matches": ["-l"], "content": ["-n"], "count": ["-c"]}[mode]
    for d in sorted(IGNORED_DIRS):
        argv += ["-g", f"!{d}/"]
    if glob_:
        argv += ["-g", glob_]
    if max_count:
        argv += ["-m", str(max_count)]
    argv += ["-e", pattern, str(base)]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    rows = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        head, sep, rest = line.partition(":")
        rel = ctx.workspace.rel(Path(head)) if Path(head).is_absolute() else head
        rows.append(f"{rel}{sep}{rest}")
    return sorted(rows, key=_row_key)


def _row_key(row: str) -> tuple:
    parts = row.split(":")
    return (parts[0], int(parts[1]) if len(parts) > 2 and parts[1].isdigit() else 0)


def _py_grep(
    ctx: ToolContext,
    regex: re.Pattern,
    base: Path,
    glob_: str | None,
    mode: str,
    max_count: int | None,
) -> list[str]:
    # An explicitly named path (file or directory) is always searched; IGNORED_DIRS only
    # filters entries discovered while walking *below* it, so a directory named e.g.
    # "node_modules" is still searched when it is the path the caller asked for.
    base_parts = PurePosixPath(ctx.workspace.rel(base)).parts
    files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
    rows: list[str] = []
    for p in files:
        rel = ctx.workspace.rel(p)
        below = PurePosixPath(rel).parts[len(base_parts) :]
        ignored = any(part in IGNORED_DIRS for part in below)
        if ignored or (glob_ and not fnmatch.fnmatch(p.name, glob_)):
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue
        hits = 0
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                hits += 1
                if mode == "content":
                    rows.append(f"{rel}:{number}:{line}")
                if max_count and hits >= max_count:
                    break
        if hits and mode == "files_with_matches":
            rows.append(rel)
        elif hits and mode == "count":
            rows.append(f"{rel}:{hits}")
    return rows


FILE_TOOLS = [ls, read_file, write_file, edit_file, delete, glob, grep]
