"""Server process lifecycle: reuse-or-spawn, and the `luna serve` subcommand."""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from luna.server.auth import read_token_file, token_path, write_token_file


def log_path(env=None) -> Path:
    """Path to the background-spawned server's stdout/stderr log.

    Without this, a turn that fails inside the server process (a provider
    error, an unhandled exception) is completely invisible: the process is
    detached with no controlling terminal, so anything it prints just goes
    nowhere. Same directory/naming convention as ``token_path``.
    """
    return token_path(env).parent / "server.log"


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


def _code_fingerprint() -> float:
    """Latest mtime across every ``.py`` file in the installed ``luna`` package.

    Not a version-string check: this project's version number does not
    bump on every local edit made during active development (an editable
    install), so two runs of the same nominal version can still be
    genuinely different code. Real incident this fixes: a background
    server spawned days earlier kept being silently reused across many
    unrelated code changes (new tools, new routes, bugfixes) — it was
    still running whatever it had originally imported, so none of that
    ever took effect until the process was found and killed by hand.
    """
    root = Path(__file__).resolve().parent.parent  # .../luna/
    return max((p.stat().st_mtime for p in root.rglob("*.py")), default=0.0)


def _default_kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def ensure_running(
    workdir: str,
    *,
    on_warn: Callable[[str], None] = print,
    _is_alive: Callable[[int], bool] = _default_is_alive,
    _spawn: Callable[[int, str], None] | None = None,
    _fingerprint: Callable[[], float] | None = None,
    _kill: Callable[[int], None] | None = None,
) -> dict:
    """Return the running server's token-file dict, starting one if needed.

    ``_spawn(port, token)`` is injected for testing; the real default
    launches ``luna serve`` as a detached background subprocess. A live
    server whose recorded ``fingerprint`` (see :func:`_code_fingerprint`)
    doesn't match the code on disk right now is treated the same as a dead
    one — restarted, not reused — so local edits actually take effect
    instead of running invisibly against a stale, already-imported copy of
    the code until someone finds and kills the old process by hand.
    """
    fingerprint = (_fingerprint or _code_fingerprint)()
    existing = read_token_file()
    if existing is not None and _is_alive(existing["pid"]):
        if existing.get("fingerprint") == fingerprint:
            return existing
        on_warn("Luna server code changed since it started — restarting the background server")
        (_kill or _default_kill)(existing["pid"])

    if existing is not None:
        # Stale (dead, or just killed above for being outdated) — remove it
        # so a premature read below can't hand back dead credentials for a
        # server that's no longer running.
        token_path().unlink(missing_ok=True)

    port = _free_port()
    token = secrets.token_hex(16)
    if _spawn is not None:
        _spawn(port, token)
    else:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", buffering=1) as log_file:
            subprocess.Popen(
                [sys.executable, "-m", "luna.server.run", "--port", str(port), "--token", token],
                start_new_session=True,
                stdout=log_file,
                stderr=log_file,
            )

    # The real spawn is asynchronous: the new process writes its own token
    # file only once it is up, so a single read here would race it (and,
    # before the unlink above, could return the dead server's port — a port
    # number some unrelated process may now own). Poll until the file the
    # freshly-spawned server wrote actually appears. An injected `_spawn`
    # writes synchronously, so this succeeds on the first check with no delay.
    deadline = time.monotonic() + 5.0
    data = read_token_file()
    while (data is None or data["token"] != token) and time.monotonic() < deadline:
        time.sleep(0.05)
        data = read_token_file()
    return data or {"port": port, "token": token, "pid": 0}


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


def make_trust_check() -> Callable[[str], bool]:
    """Build the server's trust check against ``luna_projects``.

    Opens a fresh :class:`luna.core.projects.ProjectIndex` per call: trust is
    written by a separate CLI process, possibly long after this server
    started, and must take effect immediately.
    """

    def trust_check(workdir: str) -> bool:
        from luna.core.projects import ProjectIndex

        return ProjectIndex().is_trusted(workdir)

    return trust_check


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
    write_token_file(port=port, token=token, pid=os.getpid(), fingerprint=_code_fingerprint())

    app = create_app(
        agent_factory=make_agent_factory(), token=token, trust_check=make_trust_check()
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(run_serve(sys.argv[1:]))
