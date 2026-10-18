"""Slash commands over HTTP: one entry point, like opencode's /session/:id/command.

``/undo``, ``/redo``, ``/diff`` and ``/compact`` act on the luna-core session
(the REPL's core handlers); the other shared handlers are still synchronous
and run in a worker thread over :class:`_ServerEnv` (Task 31 ports them).
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.commands import CommandResult, command_kind, list_commands, run_line
from luna.repl import usercmd
from luna.repl.commands import CORE_COMMANDS, usage_of
from luna.server.runtime import runtime_for
from luna.server.trust import trust_error
from luna.turn.engine import Notice


class _ServerEnv:
    """:class:`luna.commands.CommandEnv` over a :class:`~luna.server.runtime.SessionRuntime`.

    The shared handlers run in a worker thread; the async runtime calls
    (build, rebuild, model switch) are scheduled on the server's loop.
    """

    can_rebuild = True

    def __init__(self, runtime, manager, loop: asyncio.AbstractEventLoop) -> None:
        self._rt = runtime
        self._loop = loop
        self.session = runtime.session
        self.state = runtime.state
        self.manager = manager
        self.workdir = runtime.workdir
        self.pinned = runtime.state.pinned
        self.user_commands = usercmd.load(runtime.workdir)
        # what the not-yet-ported shared handlers still read
        self.thread_id = self.session_id = runtime.id
        self.index = None
        self.usage = usage_of(runtime.session)
        self._config = None
        self._bundle = None

    def _wait(self, coro):
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:  # waiting on our own loop would deadlock
            coro.close()
            raise RuntimeError("must be called from a worker thread, not the event loop")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    @property
    def config(self):
        if self._config is None:
            self._config = self._rt.config()
        return self._config

    @property
    def bundle(self):
        if self._bundle is None:
            self._bundle = self._wait(self._rt.bundle())
        return self._bundle

    @property
    def agent(self):
        return self.bundle.agent

    def get_plan(self):
        return self._rt.state.plan

    def set_plan(self, on: bool) -> None:
        self._rt.set_plan(on)

    def _after_rebuild(self) -> None:
        self._config = None
        self._bundle = None

    def rebuild(self) -> None:
        self._wait(self._rt.rebuild())
        self._after_rebuild()

    def switch_model(self, model: str) -> None:
        self._wait(self._rt.switch_model(model))
        self._after_rebuild()

    def switch_provider(self, provider: str) -> None:
        self._wait(self._rt.switch_provider(provider))
        self._after_rebuild()


async def _run_core(name: str, arg: str, env: _ServerEnv, runtime) -> CommandResult:
    if name == "/compact":
        try:
            env._bundle = await runtime.bundle()
        except Exception as exc:  # noqa: BLE001 - a bad config must not kill the session
            return CommandResult(notices=[Notice("error", f"/compact failed: {exc}")])
    return await CORE_COMMANDS[name](env, arg)


async def post_command(request: Request) -> JSONResponse:
    """Run one slash command for a session and return its structured result."""
    session_id = request.path_params["session_id"]
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    runtime = await runtime_for(request, session_id, body.get("workdir", "."))
    if isinstance(runtime, JSONResponse):
        return runtime
    line = body["line"].strip()
    name, _, arg = line.partition(" ")
    arg = arg.strip()
    kind = command_kind(name, usercmd.load(runtime.workdir))
    if kind == "ui":
        return JSONResponse({"error": "client_command"}, status_code=400)
    busy = runtime.lock.locked() or request.app.state.runtimes.running(session_id)
    if kind != "read" and busy:
        return JSONResponse({"error": "session_busy"}, status_code=409)
    env = _ServerEnv(runtime, request.app.state.manager, asyncio.get_running_loop())

    if kind == "read":
        # read-only: may run mid-turn, never writes state
        if name in CORE_COMMANDS:
            result = await _run_core(name, arg, env, runtime)
        else:
            result = await asyncio.to_thread(run_line, line, env)
        return JSONResponse(result.to_json())

    # Hold the session lock: /compact, /undo, /verify... must never run
    # alongside a turn on the same session (they rewrite history and files).
    async with runtime.lock:
        if name in CORE_COMMANDS:
            result = await _run_core(name, arg, env, runtime)
        else:
            result = await asyncio.to_thread(run_line, line, env)
        runtime.save()
    return JSONResponse(result.to_json())


async def get_commands(request: Request) -> JSONResponse:
    """Built-in + project commands, for the TUI's /help and autocomplete."""
    workdir = request.query_params.get("workdir")
    if (error := trust_error(request, workdir)) is not None:
        return error
    return JSONResponse({"commands": list_commands(usercmd.load(workdir or "."))})


def usage_summary(session) -> str:
    """Return the status-bar usage line, e.g. ``"12k in · 3k out · $0.04"`` (or "")."""
    usage = session.usage()
    if not (usage.input or usage.output):
        return ""
    cost = session.cost()
    line = f"{usage.input // 1000}k in · {usage.output // 1000}k out"
    return line + (f" · ${cost:.2f}" if cost else "")


async def get_state(request: Request) -> JSONResponse:
    """Return the session's settings for the status bar."""
    session_id = request.path_params["session_id"]
    workdir = request.query_params.get("workdir")
    if (error := trust_error(request, workdir)) is not None:
        return error
    runtime = await runtime_for(request, session_id, workdir or ".")
    if isinstance(runtime, JSONResponse):
        return runtime
    cfg = await asyncio.to_thread(runtime.config)
    summary = await asyncio.to_thread(usage_summary, runtime.session)
    return JSONResponse(
        {
            "provider": cfg.provider,
            "model": cfg.model,
            "plan": runtime.state.plan,
            "pinned": runtime.state.pinned.paths,
            "usage_summary": summary,
            "undo_depth": runtime.session.undo_depth,
            "running": request.app.state.runtimes.running(session_id),
        }
    )
