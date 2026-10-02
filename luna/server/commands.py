"""Slash commands over HTTP: one entry point, like opencode's /session/:id/command."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.commands import command_kind, list_commands, run_line
from luna.config.usage import indicator_line
from luna.repl import usercmd
from luna.server.runtime import runtime_for
from luna.server.trust import trust_error


class _ServerEnv:
    """:class:`luna.commands.CommandEnv` over a :class:`SessionRuntime`."""

    def __init__(self, runtime) -> None:
        self._rt = runtime
        self.workdir = runtime.workdir
        self.thread_id = runtime.thread_id
        self.session_id = runtime.thread_id
        from luna.core.persistence import SessionIndex

        self.index = SessionIndex()
        self.user_commands = usercmd.load(runtime.workdir)
        self.pinned = runtime.state.pinned
        self.usage = runtime.state.usage
        self._config = None

    @property
    def config(self):
        if self._config is None:
            self._config = self._rt.config()
        return self._config

    @property
    def agent(self):
        return self._rt.agent

    can_rebuild = True

    def get_plan(self):
        return self._rt.state.plan

    def set_plan(self, on: bool) -> None:
        self._rt.set_plan(on)

    def rebuild(self) -> None:
        self._rt.rebuild()

    def switch_model(self, model: str) -> None:
        self._rt.switch_model(model)
        self._config = None

    def switch_provider(self, provider: str) -> None:
        self._rt.switch_provider(provider)
        self._config = None

    def after_turn_reload(self) -> None:
        self._rt.reload_after_turn = True


async def post_command(request: Request) -> JSONResponse:
    """Run one slash command for a session and return its structured result."""
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    runtime = runtime_for(request, thread_id, body.get("workdir", "."))
    if isinstance(runtime, JSONResponse):
        return runtime
    line = body["line"].strip()
    name = line.partition(" ")[0]
    kind = command_kind(name, usercmd.load(runtime.workdir))
    if kind == "ui":
        return JSONResponse({"error": "client_command"}, status_code=400)
    if kind != "read" and runtime.lock.locked():
        return JSONResponse({"error": "session_busy"}, status_code=409)

    if kind == "read":
        # read-only: may run mid-turn, never writes state (the turn thread does)
        result = await run_in_threadpool(run_line, line, _ServerEnv(runtime))
        return JSONResponse(result.to_json())

    def _run():
        result = run_line(line, _ServerEnv(runtime))
        runtime.save()
        return result

    # Hold the session lock: /compact, /undo, /verify... must never run
    # alongside a turn on the same thread (they rewrite history and files).
    async with runtime.lock:
        result = await run_in_threadpool(_run)
    return JSONResponse(result.to_json())


async def get_commands(request: Request) -> JSONResponse:
    """Built-in + project commands, for the TUI's /help and autocomplete."""
    workdir = request.query_params.get("workdir")
    if (error := trust_error(request, workdir)) is not None:
        return error
    return JSONResponse({"commands": list_commands(usercmd.load(workdir or "."))})


async def get_state(request: Request) -> JSONResponse:
    """Return the session's settings for the status bar."""
    thread_id = request.path_params["thread_id"]
    workdir = request.query_params.get("workdir")
    if (error := trust_error(request, workdir)) is not None:
        return error
    runtime = runtime_for(request, thread_id, workdir or ".")
    if isinstance(runtime, JSONResponse):
        return runtime
    cfg = await run_in_threadpool(runtime.config)
    state = runtime.state
    summary = (
        indicator_line(state.usage, cfg.provider, cfg.model, cfg.pricing)
        if state.usage.turns
        else ""
    )
    return JSONResponse(
        {
            "provider": cfg.provider,
            "model": cfg.model,
            "plan": state.plan,
            "pinned": state.pinned.paths,
            "usage_summary": summary,
        }
    )
