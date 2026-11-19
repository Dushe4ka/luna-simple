"""The team runner: plan once, then members on a board, serial merges, an event-driven lead.

One asyncio loop per run. Each iteration it claims ready tasks for idle
members (an atomic SQL claim), wakes the lead when something changed, and
waits for a member, the lead or a change. A member turn works exactly one
task in its own git worktree; finished code is merged into ``luna/team/<id>``
under one lock and, with a verify command, checked right after.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from luna_core import catalog
from luna_core.assemblies import AssemblyLog
from luna_core.context import build_system_prompt
from luna_core.dynamic import is_writing, resolve_spec
from luna_core.errors import LunaCoreError
from luna_core.events import Cancelled, TurnDone, TurnError
from luna_core.hooks import Hooks
from luna_core.loop import Agent
from luna_core.policy import DEFAULT_APPROVAL, Rules
from luna_core.providers.base import Provider
from luna_core.rategate import RateGate, gated, shared_gate
from luna_core.roles import ROLES, ModelRoles
from luna_core.sandbox import Sandbox
from luna_core.session import EventLog, Session
from luna_core.subagents import (
    CHILD_FORBIDDEN,
    READ_ONLY,
    WRITE_TOOLS,
    DynamicSettings,
    SubagentSpec,
    child_prompt,
)
from luna_core.team.board import LEAD, TeamMember, TeamRun, TeamStore, TeamTask
from luna_core.team.git import MemberTree, TeamGit
from luna_core.team.inhibit import keep_awake as _keep_awake_default
from luna_core.team.messages import Mailbox
from luna_core.team.permissions import Denied, TeamPolicy
from luna_core.team.plan import (
    PlanError,
    PlanMember,
    Planner,
    TeamPlan,
    parse_member,
    parse_plan,
)
from luna_core.team.report import build_report, write_report
from luna_core.team.settings import TeamSettings
from luna_core.team.tools import TeamContext, lead_tools, member_tools, render_board
from luna_core.tools.base import Tool, args_preview
from luna_core.usage import Usage, cost
from luna_core.workspace import Workspace

MEMBER_PROMPT = """\
Team member: {name} (Luna team {run}). You work on exactly one task from the team \
board, then end your turn. Use board_list to see the board, send_message to ask the \
lead or a teammate, read_messages for answers. Commands outside the team's permission \
profile are refused; do not retry them. Finish with board_update(status, note)."""

LEAD_RUN_PROMPT = """\
You are the Team lead of Luna team {run}. Goal: {goal}
You are woken when the board changes or somebody messages you. Keep the team moving: \
add or split tasks, hire (within the limits) or retire members, reassign stuck tasks, \
answer questions. Only you may broadcast. If nothing needs you, end your turn at once \
without calling tools. You never change files yourself."""

STALL_NOTE = (
    "The board has not changed for several wakes. Stop discussing: assign, split, reassign "
    "or retire now, or end your turn and let the run finish."
)


@dataclass
class TeamHost:
    """What the host application gives a team runner."""

    provider: Provider
    model: str
    tools: list[Tool]
    roles: ModelRoles | None = None
    profiles: dict[str, SubagentSpec] = field(default_factory=dict)
    skill_names: Collection[str] = ()
    skill_loader: Callable[[str], str] | None = None
    base_rules: Rules | None = None
    sandbox: Sandbox | None = None
    hooks: Hooks | None = None
    model_overrides: dict | None = None
    journal: AssemblyLog | None = None
    gate: RateGate | None = None
    #: Tools that need approval; the team denies and reports them unless allowed.
    approval_tools: frozenset[str] | None = None
    #: Called once a run ends; returns report lines about subagent profile changes.
    after_run: Callable[[str], Awaitable[list[str]]] | None = None


@dataclass
class _TurnResult:
    ok: bool
    error: str
    text: str
    steps: int
    cost: float
    hit_max_steps: bool
    cancelled: bool


@dataclass
class _RunState:
    """What the loop of one running team keeps in memory."""

    run_id: str
    git: TeamGit
    session: Session
    deadline: float
    budget: float | None
    workers: dict[str, asyncio.Task] = field(default_factory=dict)
    turns: dict[str, tuple[Agent, Session, int]] = field(default_factory=dict)
    lead: asyncio.Task | None = None
    inbox: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    stopping: str | None = None
    stalled: bool = False
    idle_wakes: int = 0
    last_print: str | None = None
    lead_steps: int = 0
    lead_quiet: bool = False
    orphans: set[str] = field(default_factory=set)
    merge_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TeamRunner:
    """Plans, runs and reports the team runs of one project."""

    def __init__(
        self,
        store: TeamStore,
        log: EventLog,
        host: TeamHost,
        root: str | Path,
        settings: TeamSettings | None = None,
        *,
        clock: Callable[[], float] = time.time,
        tick: float = 0.5,
        keep_awake: Callable[[bool], contextlib.AbstractContextManager] = _keep_awake_default,
    ) -> None:
        self.store = store
        self.mailbox = Mailbox(store)
        self.log = log
        self.host = host
        self.root = Path(root).resolve()
        self.settings = settings or TeamSettings()
        self.clock = clock
        self.tick = tick
        self._keep_awake = keep_awake
        self.gate = host.gate or shared_gate(self.settings.rpm)
        self.gate.listeners.append(self._on_rate)
        self._pool = {t.name: t for t in host.tools}
        self._states: dict[str, _RunState] = {}
        self._seed: dict[str, list[str]] = {}

    def close(self) -> None:
        """Stop listening to the rate gate."""
        with contextlib.suppress(ValueError):
            self.gate.listeners.remove(self._on_rate)

    def _on_rate(self, rpm: float) -> None:
        for run_id in list(self._states):
            self.store.log_event(run_id, "team_rate", rpm=rpm)

    def _run(self, run_id: str) -> TeamRun:
        run = self.store.run(run_id)
        if run is None:
            raise LunaCoreError(f"no team run {run_id}")
        return run

    # --- models and members ----------------------------------------------

    def _model(self, name: str | None) -> tuple[Provider, str]:
        """Resolve a role name, a model id or the default to a gated provider and model."""
        host = self.host
        if name in ROLES:
            if host.roles is not None:
                provider, model = host.roles.get(name, host.provider, host.model)
            else:
                provider, model = host.provider, host.model
        elif name:
            provider, model = host.provider, name
        else:
            provider, model = host.provider, host.model
        return gated(provider, self.gate), model

    def member_spec(self, member: PlanMember) -> SubagentSpec:
        """Resolve a member against the host's tools, skills, profiles and limits (raises)."""
        if member.profile:
            base = self.host.profiles.get(member.profile)
            if base is None:
                raise PlanError(f"member {member.name}: unknown profile {member.profile!r}")
        else:
            base = SubagentSpec(
                member.name, "team member", member.prompt or "", tools=(*READ_ONLY, *WRITE_TOOLS)
            )
        limits = DynamicSettings(
            enabled=True,
            allow_write=True,
            models=self.settings.models,
            max_steps=self.settings.member_steps,
        )
        spec, _dynamic = resolve_spec(
            base,
            member.assembly(),
            parent_tools=self._pool,
            skills=self.host.skill_names,
            settings=limits,
        )
        return dataclasses.replace(
            spec, name=member.name, max_steps=min(spec.max_steps, self.settings.member_steps)
        )

    # --- lifecycle --------------------------------------------------------

    def create(self, task: str, *, budget: float | None = None) -> str:
        """Start a run in ``planning`` and return its id."""
        session_id = self.log.create(
            str(self.root), title=f"team: {task[:60]}", meta={"kind": "team"}
        )
        run_id = self.store.create_run(
            workdir=str(self.root),
            task=task,
            session_id=session_id,
            settings=self.settings.to_dict(),
            budget=self.settings.budget if budget is None else budget,
        )
        self.log.set_meta(session_id, team=run_id)
        self.store.add_member(run_id, LEAD, {"role": "lead"})
        return run_id

    async def plan(self, run_id: str, *, feedback: str | None = None) -> TeamPlan:
        """Let the lead (role ``strong``) plan read-only; the run then awaits approval."""
        run = self._run(run_id)
        if run.status not in ("planning", "awaiting_plan"):
            raise LunaCoreError(f"team {run_id} is {run.status}; only a new run can be planned")
        session = Session.open(self.log, run.session_id, undo=False)
        provider, model = self._model("strong")
        planner = Planner(
            provider,
            model,
            workspace=Workspace(self.root),
            tools=self.host.tools,
            max_members=self.settings.max_members,
            profiles=self.host.profiles,
            max_steps=self.settings.lead_steps,
            check_member=self.member_spec,
            model_overrides=self.host.model_overrides,
            sandbox=self.host.sandbox,
        )
        previous = TeamPlan.from_dict(run.plan) if run.plan else None
        start = len(session.records())
        before = len(session.messages())
        try:
            plan = await planner.plan(session, run.task, feedback=feedback, previous=previous)
        finally:
            steps = sum(1 for m in session.messages()[before:] if m.role == "assistant")
            spent = self._turn_cost(session, start, model)
            self.store.update_member(run_id, LEAD, add_steps=steps, add_cost=spent)
        self.store.update_run(run_id, status="awaiting_plan", plan=plan.to_dict())
        self.store.log_event(run_id, "team_plan", LEAD, plan=plan.to_dict())
        return plan

    def accept(
        self,
        run_id: str,
        *,
        plan: TeamPlan | None = None,
        allow: Iterable[str] | None = None,
        budget: float | None = None,
    ) -> TeamPlan:
        """Accept the waiting (or an edited) plan: tasks and members go on the board."""
        run = self._run(run_id)
        if run.status != "awaiting_plan" or (plan is None and not run.plan):
            raise LunaCoreError(f"team {run_id} has no plan waiting for approval")
        data = (plan or TeamPlan.from_dict(run.plan)).to_dict()
        if allow is not None:
            data["allow"] = list(allow)
        checked = parse_plan(
            data, max_members=self.settings.max_members, profiles=self.host.profiles
        )
        for member in checked.members:
            self.member_spec(member)
        for task in checked.ordered_tasks():
            self.store.add_task(
                run_id,
                title=task.title,
                kind=task.kind,
                detail=task.detail,
                deps=task.deps,
                assignee=task.member,
                task_id=task.id,
            )
        for member in checked.members:
            self.store.add_member(run_id, member.name, member.to_dict())
            self.store.log_event(run_id, "team_member", member.name, state="hired")
        values: dict = {"status": "running", "plan": checked.to_dict()}
        if budget is not None:
            values["budget"] = budget
        self.store.update_run(run_id, **values)
        return checked

    def decline(self, run_id: str) -> None:
        """Cancel a run before it started; it ends «ОСТАНОВЛЕНО (отмена)»."""
        run = self._run(run_id)
        if run.status not in ("planning", "awaiting_plan"):
            raise LunaCoreError(f"team {run_id} is {run.status}; only a new run can be declined")
        self.store.update_run(
            run_id,
            status="finished",
            outcome="stopped",
            stop_reason="cancel",
            finished=self.clock(),
        )
        report = build_report(self.store, run_id)
        write_report(self.root, run_id, report)
        self.store.log_event(
            run_id, "team_done", outcome="stopped", headline=report.data["headline"]
        )

    def cancel(self, run_id: str) -> bool:
        """Stop a running team softly, or decline one that has not started."""
        st = self._states.get(run_id)
        if st is not None:
            self._stop(st, "cancel")
            return True
        run = self.store.run(run_id)
        if run is not None and run.status in ("planning", "awaiting_plan"):
            self.decline(run_id)
            return True
        return False

    async def run(self, run_id: str) -> str:
        """Run an accepted team to the end and return its outcome."""
        run = self._run(run_id)
        if run.status != "running":
            raise LunaCoreError(f"team {run_id} is {run.status}, not running")
        if run_id in self._states:
            raise LunaCoreError(f"team {run_id} is already running here")
        git = TeamGit(self.root, run_id)
        session = Session.open(self.log, run.session_id, undo=False)
        if run.branch is None:
            base = await asyncio.to_thread(git.start)
            self.store.update_run(run_id, branch=git.branch, base=base, started=self.clock())
        else:
            await asyncio.to_thread(git.ensure)
        st = self._new_state(self._run(run_id), git, session)
        st.inbox.extend(self._seed.pop(run_id, []))
        self._states[run_id] = st
        outcome = "error"
        try:
            with self._keep_awake(not self.settings.allow_sleep) as awake:
                self.store.log_event(run_id, "awake", held=bool(awake))
                outcome = await self._loop(st)
        except asyncio.CancelledError:
            # The process is going away: leave the run "running" for the next start.
            await self._abort(st)
            raise
        except Exception as exc:  # noqa: BLE001 - a crash still ends with a report
            self.store.log_event(run_id, "error", message=f"{type(exc).__name__}: {exc}")
            await self._abort(st)
        finally:
            self._states.pop(run_id, None)
        await self._finish(st, outcome)
        return outcome

    def _new_state(self, run: TeamRun, git: TeamGit, session: Session) -> _RunState:
        started = run.started if run.started is not None else self.clock()
        return _RunState(
            run.id, git, session, deadline=started + self.settings.wall_clock, budget=run.budget
        )

    # --- the loop ---------------------------------------------------------

    async def _loop(self, st: _RunState) -> str:
        while True:
            st.changed.clear()
            self._check_limits(st)
            if not st.stopping:
                self._dispatch(st)
                self._maybe_wake_lead(st)
            if not st.workers and st.lead is None:
                outcome = self._settled(st)
                if outcome is not None:
                    return outcome
            await self._wait(st)

    async def _wait(self, st: _RunState) -> None:
        waiters = [*st.workers.values(), *([st.lead] if st.lead else [])]
        changed = asyncio.ensure_future(st.changed.wait())
        try:
            await asyncio.wait(
                [changed, *waiters], timeout=self.tick, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            changed.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await changed

    def _settled(self, st: _RunState) -> str | None:
        """Return the outcome once nothing runs, or None while there is still something to do."""
        tasks = self.store.tasks(st.run_id)
        if tasks and all(t.status == "done" for t in tasks):
            return "done"
        if st.stalled:
            return "stalled"
        if st.stopping:
            return "stopped"
        if self._lead_has_work(st):
            return None
        takers = {
            m.name for m in self.store.members(st.run_id, include_retired=False) if m.name != LEAD
        }
        ready = self.store.ready(st.run_id)
        if any(t.assignee is None or t.assignee in takers for t in ready) and takers:
            return None  # the next dispatch takes them
        stuck = [t for t in ready if t.id not in st.orphans]
        if stuck and not st.lead_quiet:
            st.orphans.update(t.id for t in stuck)
            ids = ", ".join(t.id for t in stuck)
            self._note(st, f"nobody can take {ids} (owner retired or no members): reassign or hire")
            return None
        return "partial"

    def _dispatch(self, st: _RunState) -> None:
        for member in self.store.members(st.run_id, include_retired=False):
            if len(st.workers) >= self.settings.max_concurrent:
                return
            if member.name == LEAD or member.name in st.workers:
                continue
            task = self.store.claim_next(st.run_id, member.name)
            if task is None:
                continue
            self._task_event(st, task, member.name)
            st.workers[member.name] = asyncio.create_task(
                self._work(st, member, task), name=f"team-{st.run_id}-{member.name}"
            )

    # --- members ----------------------------------------------------------

    async def _work(self, st: _RunState, member: TeamMember, task: TeamTask) -> None:
        started = time.monotonic()
        self.store.update_member(st.run_id, member.name, status="working")
        self.store.log_event(st.run_id, "team_member", member.name, state="working", task=task.id)
        try:
            await self._attempt(st, member, task)
        except Exception as exc:  # noqa: BLE001 - a crashed attempt fails its task, not the run
            message = f"{type(exc).__name__}: {exc}"
            self._set_task(st, task.id, "failed", f"crashed: {message}", member.name)
            self.store.log_event(st.run_id, "error", member.name, message=f"{task.id}: {message}")
        finally:
            st.workers.pop(member.name, None)
            self.store.update_member(
                st.run_id, member.name, status="idle", add_busy=time.monotonic() - started
            )
            totals = self.store.member(st.run_id, member.name)
            self.store.log_event(
                st.run_id, "team_member", member.name, state="idle",
                steps=totals.steps, cost=totals.cost,
            )  # fmt: skip
            st.changed.set()

    async def _attempt(self, st: _RunState, member: TeamMember, task: TeamTask) -> None:
        plan_member = PlanMember.from_dict(member.spec)
        spec = self.member_spec(plan_member)
        code = task.kind == "code"
        tree: MemberTree | None = None
        if code:
            merge = None
            if task.merge_of:
                source = self.store.task(st.run_id, task.merge_of)
                merge = source.branch if source else None
            # Under the merge lock: never fork from a merge that verify may still roll back.
            async with st.merge_lock:
                tree = await asyncio.to_thread(
                    st.git.member_tree, task.id, task.attempts, branch=task.branch, merge=merge
                )
        finished = False
        branch = None
        try:
            if tree is not None:
                self.store.update_task(
                    st.run_id, task.id, branch=tree.branch, worktree=str(tree.path)
                )
                workspace = Workspace(tree.path)
            else:
                workspace = Workspace(st.git.integration)
            ctx = TeamContext(
                self.store,
                self.mailbox,
                st.run_id,
                member.name,
                task_id=task.id,
                stopping=lambda: st.stopping is not None,
                on_change=st.changed.set,
            )
            session = self._member_session(st, member.name, task, workspace)
            agent = self._member_agent(st, spec, workspace, ctx, session, code=code)
            result = await self._drive(st, member.name, agent, session, self._task_text(task))
            self.store.update_member(
                st.run_id, member.name, add_steps=result.steps, add_cost=result.cost
            )
            self._journal(st, plan_member, spec, session, task, result)
            self._check_limits(st)
            status, note = self._outcome(ctx, task, spec, result)
            if tree is not None:
                stat, commits = await asyncio.to_thread(
                    st.git.finish, tree, f"luna team {st.run_id} {task.id}: {task.title[:60]}"
                )
                finished = True
                branch = tree.branch if stat else None
                task = self.store.update_task(
                    st.run_id,
                    task.id,
                    worktree=None,
                    branch=branch,
                    commits=[*task.commits, *commits],
                )
        except Exception:
            # A cancel (process going away) is not caught: its worktree stays for recovery.
            if tree is not None and not finished:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(st.git.salvage, tree.path, tree.branch)
                self.store.update_task(st.run_id, task.id, worktree=None)
            raise
        if st.stopping and status != "done":
            kept = f"; work kept on {branch}" if branch else ""
            self._set_task(st, task.id, "pending", f"stopped ({st.stopping}){kept}", member.name)
            return
        if status != "done":
            self._set_task(st, task.id, status, note, member.name)
            return
        if not code:
            self._write_note(st, task, note or result.text)
            self._set_task(st, task.id, "done", note or result.text[:300], member.name)
            return
        if branch is None:
            if task.merge_of:
                self._retry_or_fail(
                    st, task, "the conflict was not resolved (no changes)", member.name
                )
                return
            self._set_task(st, task.id, "done", note or "no changes", member.name)
            return
        await self._integrate(st, task, branch, note, member.name)

    @staticmethod
    def _outcome(
        ctx: TeamContext, task: TeamTask, spec: SubagentSpec, result: _TurnResult
    ) -> tuple[str, str]:
        reported = ctx.reports.get(task.id)
        if reported:
            return reported
        if result.hit_max_steps:
            return "failed", f"ran out of steps ({spec.max_steps})"
        if not result.ok:
            return "failed", result.error or "the member stopped without finishing"
        return "done", result.text[-500:]

    async def _integrate(
        self, st: _RunState, task: TeamTask, branch: str, note: str, member: str
    ) -> None:
        """Merge a finished task branch (one at a time), verify, and settle the task."""
        async with st.merge_lock:
            message = f"luna team {st.run_id}: {task.id} {task.title[:60]}"
            result = await asyncio.to_thread(st.git.merge, branch, message)
            if result.status == "markers":
                self.store.log_event(
                    st.run_id, "markers", member, task=task.id, files=result.detail
                )
                self._retry_or_fail(st, task, f"conflict markers left in {result.detail}", member)
                return
            if result.status == "conflict":
                fix = self.store.add_task(
                    st.run_id,
                    title=f"resolve merge conflict of {task.id}",
                    kind="code",
                    detail=(
                        f"Merging {branch} into the team branch conflicts. Your worktree has "
                        "that merge started: resolve every conflicted file (no <<<<<<< or "
                        ">>>>>>> lines left) keeping both sides' intent.\n\n"
                        f"{result.detail[-1500:]}"
                    ),
                    merge_of=task.id,
                )
                self.store.log_event(st.run_id, "conflict", member, task=task.id, fix=fix.id)
                self._task_event(st, fix, "")
                self._set_task(st, task.id, "blocked", f"merge conflict → {fix.id}", member)
                return
            if result.status == "merged" and self.settings.verify_command:
                ok, output = await asyncio.to_thread(
                    st.git.verify, self.settings.verify_command, self.settings.verify_timeout
                )
                if not ok:
                    await asyncio.to_thread(st.git.undo_merge, result.before)
                    self.store.log_event(
                        st.run_id, "verify_failed", member, task=task.id,
                        attempt=task.attempts, output=output[-1500:],
                    )  # fmt: skip
                    self._retry_or_fail(
                        st, task, f"verify failed after merging:\n{output[-1500:]}", member
                    )
                    return
        await asyncio.to_thread(st.git.delete_branch, branch)
        self.store.update_task(st.run_id, task.id, branch=None)
        self.store.log_event(st.run_id, "merge", member, task=task.id, commit=result.commit)
        self._set_task(st, task.id, "done", note, member)
        if task.merge_of:
            source = self.store.task(st.run_id, task.merge_of)
            if source is not None and source.status == "blocked":
                if source.branch:
                    await asyncio.to_thread(st.git.delete_branch, source.branch)
                self.store.update_task(st.run_id, source.id, branch=None)
                self._set_task(st, source.id, "done", f"merged via {task.id}", "")

    def _retry_or_fail(self, st: _RunState, task: TeamTask, note: str, member: str) -> None:
        status = "failed" if task.attempts >= self.settings.max_attempts else "pending"
        self._set_task(st, task.id, status, note, member)

    def _set_task(self, st: _RunState, task_id: str, status: str, note: str, member: str) -> None:
        task = self.store.update_task(st.run_id, task_id, status=status, note=note)
        self._task_event(st, task, member)
        who = f" ({member})" if member else ""
        short = " ".join(note.split())[:300]
        self._note(st, f"{task_id} {status}{who}: {short}" if short else f"{task_id} {status}{who}")

    def _task_event(self, st: _RunState, task: TeamTask, member: str) -> None:
        self.store.log_event(
            st.run_id, "team_task", member, task=task.id, status=task.status,
            note=task.note[:300], assignee=task.assignee,
        )  # fmt: skip

    def _note(self, st: _RunState, line: str) -> None:
        """Tell the lead about something at its next wake."""
        st.inbox.append(line)
        st.changed.set()

    @staticmethod
    def _task_text(task: TeamTask) -> str:
        lines = [f"Task {task.id}: {task.title}"]
        if task.detail:
            lines += ["", task.detail]
        if task.kind == "code":
            lines += [
                "",
                "Your working directory is your own git worktree of the team branch. "
                "Change files there; Luna commits your work when your turn ends.",
            ]
        else:
            lines += ["", "This is a research task: read, do not change files. Your final "
                      "note becomes the task's notes file."]  # fmt: skip
        if task.attempts > 1 and task.note:
            lines += ["", f"Earlier attempt: {task.note}"]
        lines += [
            "",
            'When you are done call board_update(status="done", note="<what you did>"); '
            'if you cannot finish, report "blocked" or "failed" with the reason.',
        ]
        return "\n".join(lines)

    def _member_session(
        self, st: _RunState, name: str, task: TeamTask, workspace: Workspace
    ) -> Session:
        sid = self.log.create(
            str(workspace.root),
            title=f"{name}: {task.id} {task.title[:50]}",
            parent_id=st.session.id,
            meta={"kind": "team-member", "team": st.run_id, "member": name, "task": task.id,
                  "attempt": task.attempts},
        )  # fmt: skip
        return Session(self.log, sid, workdir=str(workspace.root), undo=False)

    def _member_agent(
        self,
        st: _RunState,
        spec: SubagentSpec,
        workspace: Workspace,
        ctx: TeamContext,
        session: Session,
        *,
        code: bool,
    ) -> Agent:
        names = [n for n in spec.tools if n in self._pool and n not in CHILD_FORBIDDEN]
        if not code:
            names = [n for n in names if not is_writing(n, self._pool)]
        skills: list[tuple[str, str]] = []
        if spec.skills and self.host.skill_loader is not None:
            skills = [(n, self.host.skill_loader(n)) for n in spec.skills]
        provider, model = self._model(spec.model)
        run = self._run(st.run_id)
        prompt = MEMBER_PROMPT.format(name=spec.name, run=st.run_id)
        policy = TeamPolicy(
            (run.plan or {}).get("allow", ()),
            deny=self._deny_rules(),
            workspace=workspace,
            member=spec.name,
            on_denied=self._denied(st),
            file_writes=code,
            approval_tools=self._approval_tools(),
        )
        return Agent(
            provider,
            model,
            tools=[*(self._pool[n] for n in names), *member_tools(ctx)],
            system=build_system_prompt(
                f"{prompt}\n\n{child_prompt(spec, skills)}", root=workspace.root, memory=[]
            ),
            policy=policy,
            workspace=workspace,
            sandbox=self.host.sandbox,
            hooks=self.host.hooks,
            max_steps=spec.max_steps,
            max_cost=self._max_cost(st, session),
            model_overrides=self.host.model_overrides,
            name=spec.name,
        )

    def _turn_cost(self, session: Session, start: int, model: str) -> float:
        """USD of the model calls recorded after record ``start`` (also of failed turns)."""
        total = Usage()
        for rec in session.records()[start:]:
            if rec.type == "assistant" and rec.payload.get("usage"):
                total = total + Usage.from_dict(rec.payload["usage"])
        return cost(total, catalog.lookup(model, self.host.model_overrides)) or 0.0

    async def _drive(
        self, st: _RunState, name: str, agent: Agent, session: Session, text: str
    ) -> _TurnResult:
        """Run one turn to its end; ``st.turns`` lets a soft stop shorten it."""
        start = len(session.records())
        before = len(session.messages())
        ok, error, hit, cancelled = True, "", False, False
        st.turns[name] = (agent, session, before)
        try:
            async with contextlib.aclosing(agent.run(session, text)) as events:
                async for event in events:
                    if isinstance(event, TurnDone):
                        hit = event.stop_reason == "max_steps"
                    elif isinstance(event, TurnError):
                        ok, error = False, event.message
                        if event.kind != "budget":
                            self.store.log_event(
                                st.run_id, "provider_error", name, message=event.message[:300]
                            )
                    elif isinstance(event, Cancelled):
                        ok, error, cancelled = False, "cancelled", True
        finally:
            st.turns.pop(name, None)
        added = session.messages()[before:]
        answers = [m for m in added if m.role == "assistant" and m.text]
        return _TurnResult(
            ok,
            error,
            answers[-1].text if answers else "",
            sum(1 for m in added if m.role == "assistant"),
            self._turn_cost(session, start, agent.model),
            hit,
            cancelled,
        )

    def _journal(
        self,
        st: _RunState,
        member: PlanMember,
        spec: SubagentSpec,
        session: Session,
        task: TeamTask,
        result: _TurnResult,
    ) -> None:
        """Feed the assembly journal (spec part 1b) so good members become profiles."""
        if self.host.journal is None:
            return
        with contextlib.suppress(Exception):
            self.host.journal.record(
                workdir=str(self.root),
                parent_session=st.session.id,
                child_session=session.id,
                base_profile=member.profile or "team",
                dynamic=bool(member.assembly()),
                spec=spec.to_dict(),
                task=f"{task.title}\n{task.detail}".strip(),
                ok=result.ok,
                cancelled=result.cancelled,
                hit_max_steps=result.hit_max_steps,
                steps=result.steps,
                cost=result.cost,
                mutating=[n for n in spec.tools if is_writing(n, self._pool)],
            )

    def _write_note(self, st: _RunState, task: TeamTask, text: str) -> None:
        path = self.root / ".luna" / "teams" / st.run_id / "notes" / f"{task.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {task.id}: {task.title}\n\n{text.strip()}\n", encoding="utf-8")

    def _deny_rules(self) -> list[str]:
        return list(self.host.base_rules.deny) if self.host.base_rules is not None else []

    def _approval_tools(self) -> frozenset[str]:
        return self.host.approval_tools or DEFAULT_APPROVAL

    def _denied(self, st: _RunState) -> Denied:
        def record(member: str, tool: str, args: dict, reason: str) -> None:
            preview = str(args.get("command") or "") or args_preview(args, 120)
            self.store.log_event(
                st.run_id, "team_denied", member, tool=tool, preview=preview, reason=reason
            )

        return record

    def _spent(self, st: _RunState) -> float:
        """Everything the run's members and lead have cost so far."""
        return sum(m.cost for m in self.store.members(st.run_id))

    def _max_cost(self, st: _RunState, session: Session) -> float | None:
        if st.budget is None:
            return None
        return session.cost() + max(0.0, st.budget - self._spent(st))

    # --- limits -----------------------------------------------------------

    def _check_limits(self, st: _RunState) -> None:
        """Soft-stop the run once the wall clock or the budget runs out."""
        if st.stopping:
            return
        if self.clock() >= st.deadline:
            self._stop(st, "time")
        elif st.budget is not None and self._spent(st) >= st.budget:
            self._stop(st, "budget")

    def _stop(self, st: _RunState, reason: str) -> None:
        """No new tasks; running turns get the step in flight plus one more to save work."""
        if st.stopping:
            return
        st.stopping = reason
        self.store.log_event(st.run_id, "soft_stop", reason=reason)
        for agent, session, before in list(st.turns.values()):
            done = sum(1 for m in session.messages()[before:] if m.role == "assistant")
            agent.max_steps = min(agent.max_steps, done + 2)
        st.changed.set()

    def _stall_check(self, st: _RunState) -> str:
        """Return ``""``, ``"warn"`` or ``"stop"`` at every lead wake (a stop stalls the run)."""
        mark = self.store.fingerprint(st.run_id)
        st.idle_wakes = st.idle_wakes + 1 if mark == st.last_print else 0
        st.last_print = mark
        limit = self.settings.stall_wakes
        if st.idle_wakes >= 2 * limit:
            st.stalled = True
            self.store.log_event(st.run_id, "stalled", LEAD)
            self._stop(st, "stall")
            return "stop"
        if st.idle_wakes == limit:
            self.store.log_event(st.run_id, "stall_warn", LEAD)
            return "warn"
        return ""

    # --- the lead ---------------------------------------------------------

    def _lead_has_work(self, st: _RunState) -> bool:
        if st.lead_quiet or st.stalled:
            return False
        return bool(st.inbox) or self.mailbox.count_unread(st.run_id, LEAD) > 0

    def _maybe_wake_lead(self, st: _RunState) -> None:
        if st.lead is not None or not self._lead_has_work(st):
            return
        verdict = self._stall_check(st)
        if verdict == "stop":
            return
        lines, st.inbox = st.inbox, []
        if verdict == "warn":
            lines.append(STALL_NOTE)
        st.lead = asyncio.create_task(self._wake_lead(st, lines), name=f"team-{st.run_id}-lead")

    async def _wake_lead(self, st: _RunState, lines: list[str]) -> None:
        try:
            ctx = TeamContext(
                self.store,
                self.mailbox,
                st.run_id,
                LEAD,
                stopping=lambda: st.stopping is not None,
                hire=lambda args: self._hire(st.run_id, args),
                on_change=st.changed.set,
            )
            agent = self._lead_agent(st, ctx, self.settings.lead_steps - st.lead_steps)
            result = await self._drive(st, LEAD, agent, st.session, self._wake_text(st, lines))
            st.lead_steps += result.steps
            self.store.update_member(st.run_id, LEAD, add_steps=result.steps, add_cost=result.cost)
            if st.lead_steps >= self.settings.lead_steps:
                st.lead_quiet = True
                self.store.log_event(st.run_id, "lead_out_of_steps", LEAD)
        except Exception as exc:  # noqa: BLE001 - a broken lead must not take the run down
            st.lead_quiet = True
            self.store.log_event(
                st.run_id, "error", LEAD, message=f"lead: {type(exc).__name__}: {exc}"
            )
        finally:
            st.lead = None
            st.changed.set()

    def _wake_text(self, st: _RunState, lines: list[str]) -> str:
        run = self._run(st.run_id)
        goal = (run.plan or {}).get("goal", run.task)
        parts = [
            f"Team {st.run_id}. Goal: {goal}",
            "",
            "Board:",
            render_board(self.store, st.run_id),
        ]
        if lines:
            parts += ["", "Since you last looked:", *(f"- {line}" for line in lines)]
        messages = self.mailbox.unread(st.run_id, LEAD)
        if messages:
            parts += ["", "Messages to you:", *(f"- from {m.sender}: {m.text}" for m in messages)]
        parts += ["", "Act with your tools if the team needs you; otherwise end your turn now."]
        return "\n".join(parts)

    def _lead_agent(self, st: _RunState, ctx: TeamContext, max_steps: int) -> Agent:
        run = self._run(st.run_id)
        workspace = Workspace(st.git.integration)
        provider, model = self._model("strong")
        read_only = [self._pool[n] for n in READ_ONLY if n in self._pool]
        prompt = LEAD_RUN_PROMPT.format(run=st.run_id, goal=(run.plan or {}).get("goal", run.task))
        return Agent(
            provider,
            model,
            tools=[*read_only, *lead_tools(ctx)],
            system=build_system_prompt(prompt, root=workspace.root, memory=[]),
            policy=TeamPolicy(
                (),
                deny=self._deny_rules(),
                workspace=workspace,
                member=LEAD,
                on_denied=self._denied(st),
                approval_tools=self._approval_tools(),
            ),  # fmt: skip
            workspace=workspace,
            sandbox=self.host.sandbox,
            max_steps=max(1, max_steps),
            max_cost=self._max_cost(st, st.session),
            model_overrides=self.host.model_overrides,
            name=LEAD,
        )

    def _hire(self, run_id: str, args: dict) -> str:
        """Hire for the lead, within ``max_members`` and only a valid assembly."""
        hired = [m for m in self.store.members(run_id) if m.name != LEAD]
        if len(hired) >= self.settings.max_members:
            raise ValueError(f"member limit reached ({self.settings.max_members} per run)")
        member = parse_member(args, self.host.profiles)
        self.member_spec(member)
        self.store.add_member(run_id, member.name, member.to_dict())
        self.store.log_event(run_id, "team_member", member.name, state="hired")
        self.store.log_event(run_id, "decision", LEAD, text=f"hired {member.name}")
        return f"hired {member.name}"

    # --- the end ----------------------------------------------------------

    @staticmethod
    async def _abort(st: _RunState) -> None:
        tasks = [*st.workers.values(), *([st.lead] if st.lead else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _finish(self, st: _RunState, outcome: str) -> None:
        run = self._run(st.run_id)
        self.store.update_run(
            st.run_id,
            status="finished",
            outcome=outcome,
            stop_reason=st.stopping if outcome == "stopped" else None,
            finished=self.clock(),
        )
        diffstat = ""
        if run.base:
            with contextlib.suppress(Exception):
                diffstat = await asyncio.to_thread(st.git.diffstat, run.base)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(st.git.close)
        changes: list[str] = []
        if self.host.after_run is not None:
            with contextlib.suppress(Exception):
                changes = list(await self.host.after_run(st.run_id))
        report = build_report(self.store, st.run_id, diffstat=diffstat, profile_changes=changes)
        path = write_report(self.root, st.run_id, report)
        self.store.log_event(
            st.run_id, "team_done", outcome=outcome, headline=report.data["headline"],
            report=str(path),
        )  # fmt: skip
