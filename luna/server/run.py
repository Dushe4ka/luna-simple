"""Server process lifecycle: reuse-or-spawn, and the `luna serve` subcommand."""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from luna_core import shell as _shell
from luna_core.fsperm import restrict

from luna.server.auth import read_token_file, token_path, write_token_file

_log = logging.getLogger(__name__)

#: Environment variable that hands the token to a spawned ``luna serve``.
#: Command-line arguments are world-readable (``ps``); the environment is not.
TOKEN_ENV = "LUNA_SERVER_TOKEN"


def log_path(env=None) -> Path:
    """Path to the background-spawned server's stdout/stderr log.

    Without this, a turn that fails inside the server process (a provider
    error, an unhandled exception) is completely invisible: the process is
    detached with no controlling terminal, so anything it prints just goes
    nowhere. Same directory/naming convention as ``token_path``.
    """
    return token_path(env).parent / "server.log"


_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _windows_is_alive(pid: int) -> bool:
    """Liveness via WinAPI: ``os.kill(pid, 0)`` would terminate the process."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Full signatures: without them a 64-bit HANDLE would be truncated to int.
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        # A process that itself exits with code 259 reads as alive: accepted.
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _default_is_alive(pid: int) -> bool:
    if _shell.IS_WINDOWS:
        return _windows_is_alive(pid)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _spawn_kwargs() -> dict:
    """Detach the server so closing the launching terminal does not stop it."""
    if _shell.IS_WINDOWS:
        return {"creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW}
    return {"start_new_session": True}


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
    _shell.kill_tree(pid)


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
        # The log may hold request details: private from creation on.
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        restrict(fd)
        with os.fdopen(fd, "a", buffering=1) as log_file:
            subprocess.Popen(
                [sys.executable, "-m", "luna.server.run", "--port", str(port)],
                # never in argv: anyone on the machine can read it with ``ps``
                env={**os.environ, TOKEN_ENV: token},
                **_spawn_kwargs(),
                stdin=subprocess.DEVNULL,
                # One server serves every project: never tie its cwd to the
                # launching folder, which may be deleted while it runs.
                cwd=str(Path.home()),
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


def make_session_build(*, provider_factory: Callable[[object], object] | None = None):
    """One agent bundle per session: its config, plan flag, pipeline hooks and background tasks.

    ``provider_factory(runtime)`` injects a provider (tests pass a
    ``FakeProvider``); by default the session's configured provider is used.
    """

    async def build(runtime):
        from luna.core.agent import build_agent
        from luna.turn import engine

        provider = provider_factory(runtime) if provider_factory else None
        return await build_agent(
            runtime.config(),
            provider=provider,
            on_warn=lambda text: _log.warning("%s: %s", runtime.id, text),
            plan_flag=lambda: runtime.state.plan,
            hooks=engine.make_hooks(lambda: runtime.state, runtime.config),
            background=runtime.background,
        )

    return build


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
    parser.add_argument(
        "--token", default=None, help=f"bearer token (default: ${TOKEN_ENV}, else random)"
    )
    args = parser.parse_args(argv)

    port = args.port or _free_port()
    # Consume the variable so nothing this server runs (execute, hooks) inherits it.
    env_token = os.environ.pop(TOKEN_ENV, None)
    token = args.token or env_token or secrets.token_hex(16)
    write_token_file(port=port, token=token, pid=os.getpid(), fingerprint=_code_fingerprint())

    app = create_app(
        token=token,
        build=make_session_build(),
        trust_check=make_trust_check(),
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(run_serve(sys.argv[1:]))
