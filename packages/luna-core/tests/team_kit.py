"""Shared helpers for the team runner tests (imported as ``team_kit``)."""

from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
import time
from pathlib import Path

from luna_core.messages import ToolResult
from luna_core.providers.fake import FakeProvider, _events_for
from luna_core.rategate import RateGate
from luna_core.roles import ModelRoles
from luna_core.session import EventLog
from luna_core.team.board import TeamStore
from luna_core.team.runner import TeamHost, TeamRunner
from luna_core.team.settings import TeamSettings
from luna_core.tools import builtin_tools

_MEMBER = re.compile(r"Team member: ([a-z0-9-]+)")
call, text = FakeProvider.call, FakeProvider.text


class TeamProvider:
    """Routes each request to the script of whoever asks: ``lead`` or a member name.

    An exhausted script answers "nothing to do" (the lead is woken a variable
    number of times). ``before(who, request)`` runs first and may await;
    ``delay`` slows every request so member turns overlap.
    """

    name = "fake"

    def __init__(self, routes: dict, *, delay: float = 0.0, before=None) -> None:
        self.routes = {k: list(v) for k, v in routes.items()}
        self.requests: dict[str, list] = {}
        self.delay = delay
        self.before = before

    @staticmethod
    def who(request) -> str:
        if "Team lead" in request.system:
            return "lead"
        found = _MEMBER.search(request.system)
        return found.group(1) if found else "?"

    async def stream(self, request):
        who = self.who(request)
        self.requests.setdefault(who, []).append(request)
        if self.before is not None:
            await self.before(who, request)
        if self.delay:
            await asyncio.sleep(self.delay)
        script = self.routes.get(who) or []
        item = script.pop(0) if script else FakeProvider.text("nothing to do")
        for event in item if isinstance(item, list) else _events_for(item):
            if isinstance(event, BaseException):
                raise event
            yield event


def done(note: str):
    """A member reporting its task done."""
    return call("board_update", {"status": "done", "note": note}, "u")


def git(root, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    return root


def plan_dict(tasks, members=None, allow=()) -> dict:
    return {
        "goal": "g",
        "criteria": ["c"],
        "tasks": list(tasks),
        "members": list(members or [{"name": "dev", "prompt": "You write code."}]),
        "allow": list(allow),
    }


def make_runner(
    tmp_path,
    provider,
    *,
    settings=None,
    clock=None,
    overrides=None,
    base_rules=None,
    journal=None,
    profiles=None,
    keep_awake=None,
):
    db = tmp_path / "db" / "s.db"
    log, store = EventLog(db), TeamStore(db)
    host = TeamHost(
        provider=provider,
        model="main-model",
        tools=builtin_tools(),
        roles=ModelRoles(strong="lead-model"),
        profiles=profiles or {},
        base_rules=base_rules,
        model_overrides=overrides,
        journal=journal,
        gate=RateGate(1_000_000),
    )
    runner = TeamRunner(
        store,
        log,
        host,
        tmp_path / "repo",
        settings or TeamSettings(),
        clock=clock or time.time,
        tick=0.01,
        keep_awake=keep_awake or (lambda enabled: contextlib.nullcontext(False)),
    )
    return runner, store, log


def ready_run(runner, store, plan) -> str:
    """A run with ``plan`` accepted, ready for ``runner.run``."""
    rid = runner.create("do the thing")
    store.update_run(rid, status="awaiting_plan", plan=plan)
    runner.accept(rid)
    return rid


def result_of(request, call_id: str) -> ToolResult | None:
    for message in request.messages:
        for block in message.content:
            if isinstance(block, ToolResult) and block.call_id == call_id:
                return block
    return None


def report_of(repo: Path, rid: str) -> str:
    return (repo / ".luna" / "teams" / rid / "report.md").read_text(encoding="utf-8")
