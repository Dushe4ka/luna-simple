"""``luna acp``: the Luna agent for ACP editors (Zed, JetBrains, ...)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def run(cwd: Path) -> int:
    """Serve ACP over stdio until the editor disconnects."""
    try:
        from luna_core.acp import run_acp
    except ImportError:
        print('luna: ACP support is not installed: pip install "luna-simple[acp]"', file=sys.stderr)
        return 2

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
