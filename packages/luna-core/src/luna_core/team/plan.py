"""The team plan: its schema, its checks and the read-only lead that writes it."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field

from luna_core.context import build_system_prompt
from luna_core.errors import LunaCoreError
from luna_core.events import TurnError
from luna_core.loop import Agent
from luna_core.policy import Policy, UnsafeRule, check_rule_safe
from luna_core.providers.base import Provider
from luna_core.sandbox import Sandbox
from luna_core.session import Session
from luna_core.subagents import READ_ONLY
from luna_core.team.board import LEAD, TASK_KINDS
from luna_core.tools.base import Tool, ToolContext, ToolOutput
from luna_core.workspace import Workspace

_TASK_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
_MEMBER = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
_ESTIMATE_KEYS = ("minutes", "requests", "cost")

LEAD_PLAN_PROMPT = """\
You are the Team lead of a Luna team run. A human gave the team one task and \
approves your plan once; after that the team works without asking anybody.

First explore the repository read-only (ls, glob, grep, read_file) just enough to \
plan. Then call submit_plan once with:
- goal and done criteria;
- tasks T1…Tn: small and independently mergeable, with deps and kind "code" (changes \
files in its own git worktree) or "research" (read-only, ends in a note);
- members: for each a profile name or an assembly (prompt, tools, skills, model), and \
the tasks they own via the task's "member";
- allow: the permission profile as rules like "execute:uv run pytest*". Anything not \
allowed is refused during the run, so list every command the members need;
- estimate: minutes, model requests and cost.
If submit_plan returns an error, fix the plan and call it again. Then end your turn."""

SUBMIT_DESCRIPTION = "Submit the team plan (goal, criteria, tasks, members, allow, estimate)."

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "criteria": {"type": "array", "items": {"type": "string"}},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": list(TASK_KINDS)},
                    "deps": {"type": "array", "items": {"type": "string"}},
                    "detail": {"type": "string"},
                    "member": {"type": "string"},
                },
                "required": ["id", "title", "kind"],
            },
        },
        "members": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "profile": {"type": "string"},
                    "prompt": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "allow": {"type": "array", "items": {"type": "string"}},
        "estimate": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number"},
                "requests": {"type": "number"},
                "cost": {"type": "number"},
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["goal", "criteria", "tasks", "members"],
}


class PlanError(LunaCoreError):
    """A plan is invalid, or the lead did not produce one."""


@dataclass(frozen=True)
class PlanTask:
    """One task of the plan."""

    id: str
    title: str
    kind: str = "code"
    deps: tuple[str, ...] = ()
    detail: str = ""
    member: str | None = None

    def to_dict(self) -> dict:
        """JSON-friendly form."""
        data = {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "deps": list(self.deps),
            "detail": self.detail,
        }
        if self.member:
            data["member"] = self.member
        return data


@dataclass(frozen=True)
class PlanMember:
    """One member: a profile name and/or a dynamic assembly (spec part 1)."""

    name: str
    profile: str | None = None
    prompt: str | None = None
    tools: tuple[str, ...] | None = None
    skills: tuple[str, ...] = ()
    model: str | None = None

    def assembly(self) -> dict:
        """Return the ``resolve_spec`` arguments this member asks for."""
        out: dict = {}
        if self.prompt:
            out["prompt"] = self.prompt
        if self.tools is not None:
            out["tools"] = list(self.tools)
        if self.skills:
            out["skills"] = list(self.skills)
        if self.model:
            out["model"] = self.model
        return out

    def to_dict(self) -> dict:
        """JSON-friendly form (stored as the member's spec on the board)."""
        return {"name": self.name, **({"profile": self.profile} if self.profile else {}),
                **self.assembly()}  # fmt: skip

    @classmethod
    def from_dict(cls, data: Mapping) -> PlanMember:
        """Inverse of :meth:`to_dict`."""
        return parse_member(data)


@dataclass(frozen=True)
class TeamPlan:
    """What the lead proposes and the human accepts once."""

    goal: str
    criteria: tuple[str, ...]
    tasks: tuple[PlanTask, ...]
    members: tuple[PlanMember, ...]
    allow: tuple[str, ...] = ()
    estimate: dict = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        """JSON-friendly form (stored with the run, shown to the human)."""
        return {
            "goal": self.goal,
            "criteria": list(self.criteria),
            "tasks": [t.to_dict() for t in self.tasks],
            "members": [m.to_dict() for m in self.members],
            "allow": list(self.allow),
            "estimate": dict(self.estimate),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> TeamPlan:
        """Inverse of :meth:`to_dict` (checked again, without a member limit)."""
        return parse_plan(data, max_members=max(1, len(data.get("members") or ())))

    def ordered_tasks(self) -> list[PlanTask]:
        """Return the tasks with every dependency before its dependents (stable otherwise)."""
        done: set[str] = set()
        out: list[PlanTask] = []
        left = list(self.tasks)
        while left:
            for task in left:
                if set(task.deps) <= done:
                    out.append(task)
                    done.add(task.id)
                    left.remove(task)
                    break
            else:
                raise PlanError("task dependencies form a cycle")
        return out


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{what} must be a non-empty string")
    return value.strip()


def _strings(value: object, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PlanError(f"{what} must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def parse_member(item: object, profiles: Collection[str] | None = None) -> PlanMember:
    """Check one member entry; ``profiles`` (when given) are the known profile names."""
    if not isinstance(item, Mapping):
        raise PlanError("each member must be an object")
    name = _text(item.get("name"), "member name")
    if name == LEAD or not _MEMBER.match(name):
        raise PlanError(
            f"member name {name!r}: use lowercase letters, digits and dashes, "
            "starting with a letter (not 'lead')"
        )
    profile = item.get("profile") or None
    if profile is not None:
        profile = _text(profile, f"member {name} profile")
        if profiles is not None and profile not in profiles:
            known = ", ".join(sorted(profiles)) or "none"
            raise PlanError(f"member {name}: unknown profile {profile!r}; available: {known}")
    prompt = item.get("prompt") or None
    if prompt is not None:
        prompt = _text(prompt, f"member {name} prompt")
    if profile is None and prompt is None:
        raise PlanError(f"member {name}: give a profile or a prompt")
    tools = item.get("tools")
    tools = None if tools is None else _strings(tools, f"member {name} tools")
    if tools == ():
        raise PlanError(f"member {name}: tools must name at least one tool")
    model = item.get("model") or None
    if model is not None:
        model = _text(model, f"member {name} model")
    skills = _strings(item.get("skills"), f"member {name} skills")
    return PlanMember(name, profile, prompt, tools, skills, model)


def _parse_task(item: object, members: Iterable[str]) -> PlanTask:
    if not isinstance(item, Mapping):
        raise PlanError("each task must be an object")
    tid = _text(item.get("id"), "task id")
    if not _TASK_ID.match(tid):
        raise PlanError(f"task id {tid!r}: use a short name like T1")
    title = _text(item.get("title"), f"task {tid} title")
    kind = item.get("kind") or "code"
    if kind not in TASK_KINDS:
        raise PlanError(f"task {tid}: kind must be one of {', '.join(TASK_KINDS)}, got {kind!r}")
    deps = _strings(item.get("deps"), f"task {tid} deps")
    detail = item.get("detail") or ""
    if not isinstance(detail, str):
        raise PlanError(f"task {tid}: detail must be a string")
    member = item.get("member") or None
    if member is not None and member not in set(members):
        raise PlanError(f"task {tid}: unknown member {member!r}")
    return PlanTask(tid, title, kind, deps, detail.strip(), member)


def _check_acyclic(tasks: Iterable[PlanTask]) -> None:
    deps = {t.id: set(t.deps) for t in tasks}
    done: set[str] = set()
    while deps:
        free = [k for k, v in deps.items() if v <= done]
        if not free:
            raise PlanError(f"task dependencies form a cycle: {', '.join(sorted(deps))}")
        for key in free:
            done.add(key)
            del deps[key]


def parse_plan(
    data: object, *, max_members: int, profiles: Collection[str] | None = None
) -> TeamPlan:
    """Check a plan from the lead (or from storage) and return it."""
    if not isinstance(data, Mapping):
        raise PlanError("the plan must be an object")
    goal = _text(data.get("goal"), "goal")
    criteria = _strings(data.get("criteria"), "criteria")
    if not criteria:
        raise PlanError("criteria must list at least one done criterion")
    raw_members = data.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        raise PlanError("members must list at least one member")
    if len(raw_members) > max_members:
        raise PlanError(f"at most {max_members} members per run, got {len(raw_members)}")
    members = tuple(parse_member(item, profiles) for item in raw_members)
    names = [m.name for m in members]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise PlanError(f"duplicate member names: {', '.join(dupes)}")
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanError("tasks must list at least one task")
    tasks = tuple(_parse_task(item, names) for item in raw_tasks)
    ids = [t.id for t in tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise PlanError(f"duplicate task ids: {', '.join(dupes)}")
    for task in tasks:
        unknown = sorted(set(task.deps) - set(ids))
        if unknown:
            raise PlanError(f"task {task.id}: unknown dependencies {', '.join(unknown)}")
        if task.id in task.deps:
            raise PlanError(f"task {task.id} depends on itself")
    _check_acyclic(tasks)
    allow = _strings(data.get("allow"), "allow")
    for rule in allow:
        tool, sep, pattern = rule.partition(":")
        if not sep or not tool or not pattern:
            raise PlanError(f"permission rule {rule!r}: write <tool>:<pattern>")
        try:
            check_rule_safe(rule)
        except UnsafeRule as exc:
            raise PlanError(str(exc)) from exc
    estimate = data.get("estimate") or {}
    if not isinstance(estimate, Mapping):
        raise PlanError("estimate must be an object")
    clean: dict = {}
    for key in _ESTIMATE_KEYS:
        value = estimate.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise PlanError(f"estimate {key} must be a number >= 0")
        clean[key] = value
    notes = data.get("notes") or ""
    if not isinstance(notes, str):
        raise PlanError("notes must be a string")
    return TeamPlan(goal, criteria, tasks, members, allow, clean, notes.strip())


class Planner:
    """Runs the lead read-only until it submits a valid plan."""

    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        workspace: Workspace,
        tools: Iterable[Tool],
        max_members: int,
        profiles: Collection[str],
        max_steps: int,
        check_member: Callable[[PlanMember], object] | None = None,
        model_overrides: dict | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self.tools = list(tools)
        self.max_members = max_members
        self.profiles = profiles
        self.max_steps = max_steps
        self.check_member = check_member
        self.model_overrides = model_overrides
        self.sandbox = sandbox

    @staticmethod
    def _request(task: str, feedback: str | None, previous: TeamPlan | None) -> str:
        text = f"Plan this task for the team:\n\n{task}"
        if not feedback:
            return text
        before = (
            json.dumps(previous.to_dict(), ensure_ascii=False, indent=1) if previous else "(none)"
        )
        return (
            f"{text}\n\nYour previous plan:\n```json\n{before}\n```\n\n"
            f"The user wants changes:\n{feedback}\n\nSubmit the revised plan with submit_plan."
        )

    async def plan(
        self,
        session: Session,
        task: str,
        *,
        feedback: str | None = None,
        previous: TeamPlan | None = None,
    ) -> TeamPlan:
        """Run one lead turn in ``session`` and return the plan it submitted."""
        submitted: list[TeamPlan] = []

        async def submit(ctx: ToolContext, args: dict) -> ToolOutput:
            try:
                plan = parse_plan(args, max_members=self.max_members, profiles=self.profiles)
                if self.check_member is not None:
                    for member in plan.members:
                        self.check_member(member)
            except LunaCoreError as exc:
                return ToolOutput.error(f"plan rejected: {exc}")
            submitted.append(plan)
            return ToolOutput.text("plan recorded; end your turn now")

        tools = [t for t in self.tools if t.name in READ_ONLY]
        tools.append(Tool("submit_plan", SUBMIT_DESCRIPTION, PLAN_SCHEMA, submit, raw=True))
        agent = Agent(
            self.provider,
            self.model,
            tools=tools,
            system=build_system_prompt(LEAD_PLAN_PROMPT, root=self.workspace.root),
            policy=Policy(workspace=self.workspace, plan_mode=True),
            workspace=self.workspace,
            sandbox=self.sandbox,
            max_steps=self.max_steps,
            model_overrides=self.model_overrides,
            name=LEAD,
        )
        error = ""
        async for event in agent.run(session, self._request(task, feedback, previous)):
            if isinstance(event, TurnError):
                error = event.message
        if submitted:
            return submitted[-1]
        suffix = f": {error}" if error else ""
        raise PlanError(f"the lead finished without submitting a plan{suffix}")
