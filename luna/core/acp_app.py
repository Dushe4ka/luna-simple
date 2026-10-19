"""``luna acp``: the Luna agent for ACP editors (Zed, JetBrains, ...)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _stdin_is_readable_stream() -> bool:
    """Return False for a stdin the loop cannot poll (``/dev/null``, a file): nothing to serve."""
    import selectors

    try:
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
    except (OSError, ValueError):
        return False
    return True


def _stdout_is_a_stream() -> bool:
    """Return whether stdout is a pipe, socket or character device (the loop can write to it)."""
    import stat

    try:
        mode = os.fstat(sys.stdout.fileno()).st_mode
    except (OSError, ValueError):
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode)


def run(cwd: Path) -> int:
    """Serve ACP over stdio until the editor disconnects."""
    try:
        from luna_core.acp import run_acp
    except ImportError:
        print('luna: ACP support is not installed: pip install "luna-simple[acp]"', file=sys.stderr)
        return 2

    if not _stdout_is_a_stream():
        print(
            "luna acp: stdin and stdout must be pipes; this command is launched by an editor",
            file=sys.stderr,
        )
        return 2
    if not _stdin_is_readable_stream():  # EOF right away: the editor is gone, exit quietly
        return 0

    from luna.config.config import load_config
    from luna.core.agent import build_agent
    from luna.core.store import open_log

    bundles = []

    async def make_agent(folder: str):
        config = load_config({"workdir": folder}, cwd=folder)
        bundle = await build_agent(config, on_warn=lambda text: print(text, file=sys.stderr))
        bundles.append(bundle)
        return bundle.agent

    async def serve() -> None:
        try:
            await run_acp(make_agent, open_log())
        finally:
            for bundle in bundles:
                await bundle.aclose()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        return 130
    return 0
