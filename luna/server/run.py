"""Server process lifecycle: reuse-or-spawn, and the `luna serve` subcommand."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
from collections.abc import Callable

from luna.server.auth import read_token_file, write_token_file


def _default_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_running(
    workdir: str,
    *,
    on_warn: Callable[[str], None] = print,
    _is_alive: Callable[[int], bool] = _default_is_alive,
    _spawn: Callable[[int, str], None] | None = None,
) -> dict:
    """Return the running server's token-file dict, starting one if needed.

    ``_spawn(port, token)`` is injected for testing; the real default
    launches ``luna serve`` as a detached background subprocess.
    """
    existing = read_token_file()
    if existing is not None and _is_alive(existing["pid"]):
        return existing

    port = _free_port()
    token = secrets.token_hex(16)
    if _spawn is not None:
        _spawn(port, token)
    else:
        subprocess.Popen(
            [sys.executable, "-m", "luna.server.run", "--port", str(port), "--token", token],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return read_token_file() or {"port": port, "token": token, "pid": 0}


def run_serve(argv: list[str]) -> int:
    """`luna serve` subcommand — runs uvicorn in the foreground."""
    import argparse

    import uvicorn

    from luna.server.app import create_app

    parser = argparse.ArgumentParser(prog="luna serve")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--token", default=None)
    args = parser.parse_args(argv)

    port = args.port or _free_port()
    token = args.token or secrets.token_hex(16)
    write_token_file(port=port, token=token, pid=os.getpid())

    agent_holder: dict = {}

    def agent_factory():
        if "agent" not in agent_holder:
            from luna.config.config import load_config
            from luna.core.agent import build_agent

            cfg = load_config({})
            agent_holder["agent"] = build_agent(cfg)
        return agent_holder["agent"]

    app = create_app(agent_factory=agent_factory, token=token)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(run_serve(sys.argv[1:]))
