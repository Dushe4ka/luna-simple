"""Slash commands over HTTP: one entry point, like opencode's /session/:id/command.

Every command is an async handler of the shared registry (:mod:`luna.commands`),
run over :class:`_ServerEnv`; the REPL runs the very same handlers.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.commands import HISTORY_COMMANDS, command_kind, list_commands, needs_agent, run_line
from luna.repl import usercmd
from luna.server.runtime import runtime_for
from luna.server.trust import trust_error


class _ServerEnv:
    """:class:`luna.commands.CommandEnv` over a :class:`~luna.server.runtime.SessionRuntime`."""

    can_rebuild = True

    def __init__(self, runtime, manager, bundle) -> None:
        self._rt = runtime
        self.session = runtime.session
        self.state = runtime.state
        self.manager = manager
        self.bundle = bundle
        self.workdir = runtime.workdir
        self.pinned = runtime.state.pinned
        self.user_commands = usercmd.load(runtime.workdir)
        self._config = None

    @property
    def config(self):
        if self._config is None:
            self._config = self._rt.config()
        return self._config

    @property
    def agent(self):
        if self.bundle is None:
            raise RuntimeError("the agent could not be built; fix the config and /reload")
        return self.bundle.agent

    def get_plan(self):
        return self._rt.state.plan

    def set_plan(self, on: bool) -> None:
        self._rt.set_plan(on)

    def live_tasks(self) -> bool:
        return self._rt.has_live_tasks

    async def _after_rebuild(self) -> None:
        self._config = None
        self.bundle = await self._rt.bundle()

    async def rebuild(self) -> None:
        await self._rt.rebuild()
        await self._after_rebuild()

    async def switch_model(self, model: str) -> None:
        await self._rt.switch_model(model)
        await self._after_rebuild()

    async def switch_provider(self, provider: str) -> None:
        await self._rt.switch_provider(provider)
        await self._after_rebuild()


async def _env_for(runtime, manager, name: str) -> _ServerEnv:
    """Build the command env; the agent is built only for commands that use it.

    A failing build leaves ``bundle`` ``None``: the command reports it, and
    commands that need no agent (``/plan``, ``/reload``...) keep working
    when the config is broken.
    """
    bundle = None
    if needs_agent(name):
        try:
            bundle = await runtime.bundle()
        except Exception:  # noqa: BLE001 - reported by the command that needs the agent
            bundle = None
    return _ServerEnv(runtime, manager, bundle)


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
    name = line.partition(" ")[0]
    kind = command_kind(name, usercmd.load(runtime.workdir))
    if kind == "ui":
        return JSONResponse({"error": "client_command"}, status_code=400)
    busy = runtime.lock.locked() or request.app.state.runtimes.running(session_id)
    if kind != "read" and busy:
        return JSONResponse({"error": "session_busy"}, status_code=409)
    if name in HISTORY_COMMANDS and runtime.has_live_tasks:
        # a background subagent may still be writing files of this session
        return JSONResponse({"error": "session_busy"}, status_code=409)

    if kind == "read":
        # read-only: may run mid-turn, never writes state
        env = await _env_for(runtime, request.app.state.manager, name)
        return JSONResponse((await run_line(line, env)).to_json())

    # Hold the session lock: /compact, /undo, /verify... must never run
    # alongside a turn on the same session (they rewrite history and files).
    async with runtime.lock:
        env = await _env_for(runtime, request.app.state.manager, name)
        result = await run_line(line, env)
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
