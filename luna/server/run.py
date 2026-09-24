"""Server process lifecycle: reuse-or-spawn, and the `luna serve` subcommand."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

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


def make_agent_factory(*, model=None) -> Callable[[str], object]:
    """Build the server's per-workdir agent registry.

    One agent per project directory, not one per server process: a single
    server is reused across projects (see :func:`ensure_running`), and
    ``build_agent`` pins ``LocalShellBackend(root_dir=...)`` to
    ``config.workdir`` permanently — one shared agent would keep reading and
    writing the *first* project's files no matter which project asked.

    ``load_config`` needs both arguments: ``{"workdir": ...}`` is the only
    thing that sets ``LunaConfig.workdir``, while ``cwd=`` is what makes that
    project's own ``.luna.toml`` apply. Agents are built with a real
    :func:`luna.core.persistence.checkpointer` (the SqliteSaver on the shared
    ``sessions.db`` the CLI already uses), not ``build_agent``'s in-memory
    default — otherwise every session's history would die with the server.

    ``model`` is forwarded to ``build_agent`` to inject a fake model in
    tests, exactly like ``build_agent``'s own parameter of that name.
    """
    agents: dict[str, object] = {}

    def agent_factory(workdir: str):
        from luna.config.config import load_config
        from luna.core.agent import build_agent
        from luna.core.persistence import checkpointer

        resolved = str(Path(workdir).resolve())
        if resolved not in agents:
            cfg = load_config({"workdir": resolved}, cwd=resolved)
            agents[resolved] = build_agent(cfg, model=model, checkpointer=checkpointer())
        return agents[resolved]

    return agent_factory


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

    app = create_app(agent_factory=make_agent_factory(), token=token)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(run_serve(sys.argv[1:]))
