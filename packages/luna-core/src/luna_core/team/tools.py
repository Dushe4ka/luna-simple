"""Board and message tools for team members and the lead."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from luna_core.errors import LunaCoreError
from luna_core.team.board import LEAD, TASK_KINDS, TeamStore
from luna_core.team.messages import Mailbox
from luna_core.tools.base import Tool, ToolContext, ToolOutput

#: What a member may report with ``board_update``.
REPORT_STATUSES = ("done", "failed", "blocked")
#: Prefixed to every tool result once the run is stopping.
STOP_NOTE = (
    "STOP: the run is stopping — save your work, report with board_update and end your turn.\n\n"
)
_OPEN = ("pending", "blocked", "failed")


@dataclass
class TeamContext:
    """What the board tools of one member (or the lead) work on."""

    store: TeamStore
    mailbox: Mailbox
    run_id: str
    member: str
    task_id: str | None = None
    stopping: Callable[[], bool] = lambda: False
    hire: Callable[[dict], str] | None = None
    on_change: Callable[[], None] = lambda: None
    reports: dict[str, tuple[str, str]] = field(default_factory=dict)

    def decision(self, text: str) -> None:
        """Record a lead decision for the report."""
        self.store.log_event(self.run_id, "decision", self.member, text=text)


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required)}


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _ids(args: dict, key: str) -> list[str]:
    value = args.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} must be a list of task ids")
    return value


def render_board(store: TeamStore, run_id: str) -> str:
    """Render the board as text: one line per task, then the members."""
    lines = []
    for t in store.tasks(run_id):
        deps = f" deps {', '.join(t.deps)}" if t.deps else ""
        owner = f" → {t.assignee}" if t.assignee else ""
        note = f": {' '.join(t.note.split())[:200]}" if t.note else ""
        lines.append(f"{t.id} [{t.status}] {t.title} ({t.kind}){deps}{owner}{note}")
    members = [f"{m.name} ({m.status})" for m in store.members(run_id) if m.name != LEAD]
    lines.append("members: " + (", ".join(members) or "none"))
    return "\n".join(lines)


def _tool(
    ctx: TeamContext, name: str, description: str, schema: dict, fn: Callable[[dict], str]
) -> Tool:
    async def run(tool_ctx: ToolContext, args: dict) -> ToolOutput:
        try:
            text = fn(args)
        except KeyError as exc:
            return ToolOutput.error(f"{name}: {exc.args[0] if exc.args else exc}")
        except (ValueError, LunaCoreError) as exc:
            return ToolOutput.error(f"{name}: {exc}")
        return ToolOutput.text((STOP_NOTE if ctx.stopping() else "") + text)

    return Tool(name, description, schema, run, raw=True)


def _common(ctx: TeamContext) -> list[Tool]:
    def board_list(args: dict) -> str:
        return render_board(ctx.store, ctx.run_id)

    def send_message(args: dict) -> str:
        to = _text(args, "to")
        ctx.mailbox.send(ctx.run_id, ctx.member, to, _text(args, "text"))
        ctx.on_change()
        return f"sent to {to}"

    def read_messages(args: dict) -> str:
        messages = ctx.mailbox.unread(ctx.run_id, ctx.member)
        return "\n".join(f"from {m.sender}: {m.text}" for m in messages) or "no new messages"

    return [
        _tool(ctx, "board_list", "Show the team board: tasks, statuses, owners, members.",
              _schema({}), board_list),
        _tool(ctx, "send_message",
              "Send a message to one teammate or to 'lead'. Only the lead can message everybody.",
              _schema({"to": {"type": "string"}, "text": {"type": "string"}}, ("to", "text")),
              send_message),
        _tool(ctx, "read_messages", "Read your unread messages.", _schema({}), read_messages),
    ]  # fmt: skip


def member_tools(ctx: TeamContext) -> list[Tool]:
    """Build the tools of a member working one task."""

    def board_update(args: dict) -> str:
        status = args.get("status")
        if status not in REPORT_STATUSES:
            raise ValueError(f"status must be one of {', '.join(REPORT_STATUSES)}")
        if ctx.task_id is None:
            raise ValueError("you have no task to report on")
        note = str(args.get("note") or "").strip()
        ctx.reports[ctx.task_id] = (status, note)
        ctx.on_change()
        return f"{ctx.task_id} reported as {status}; end your turn now"

    update = _tool(
        ctx,
        "board_update",
        "Report your task's result: done, failed or blocked, with a short note.",
        _schema(
            {
                "status": {"type": "string", "enum": list(REPORT_STATUSES)},
                "note": {"type": "string"},
            },
            ("status", "note"),
        ),  # fmt: skip
        board_update,
    )
    common = _common(ctx)
    return [common[0], update, *common[1:]]


def lead_tools(ctx: TeamContext) -> list[Tool]:
    """Build the lead's tools: the common ones plus board and team changes."""
    store, run = ctx.store, ctx.run_id

    def active(name: str) -> None:
        member = store.member(run, name)
        if member is None or name == LEAD or member.status == "retired":
            raise ValueError(f"no active member {name!r}")

    def board_add(args: dict) -> str:
        kind = args.get("kind") or "code"
        member = args.get("member") or None
        if member is not None:
            active(member)
        task = store.add_task(
            run,
            title=_text(args, "title"),
            kind=kind,
            detail=str(args.get("detail") or ""),
            deps=_ids(args, "deps"),
            assignee=member,
        )
        store.log_event(run, "team_task", LEAD, task=task.id, status=task.status)
        ctx.decision(f"added {task.id}: {task.title}")
        ctx.on_change()
        return f"added {task.id}"

    def board_split(args: dict) -> str:
        task_id = _text(args, "task_id")
        task = store.task(run, task_id)
        if task is None:
            raise ValueError(f"no task {task_id}")
        if task.status not in _OPEN:
            raise ValueError(
                f"{task_id} is {task.status}; only pending, blocked or failed tasks split"
            )
        parts = args.get("parts")
        if (
            not isinstance(parts, list)
            or len(parts) < 2
            or not all(isinstance(p, dict) for p in parts)
        ):
            raise ValueError("parts must list at least two tasks, each an object with a title")
        titles = [_text(p, "title") for p in parts]
        made = [
            store.add_task(
                run,
                title=title,
                kind=part.get("kind") or task.kind,
                detail=str(part.get("detail") or ""),
                deps=task.deps,
            ).id
            for title, part in zip(titles, parts, strict=True)
        ]
        store.remove_task(run, task_id, replace_with=made)
        ctx.decision(f"split {task_id} into {', '.join(made)}")
        ctx.on_change()
        return f"{task_id} split into {', '.join(made)}"

    def hire(args: dict) -> str:
        if ctx.hire is None:
            raise ValueError("hiring is not available here")
        text = ctx.hire({k: v for k, v in args.items() if v is not None})
        ctx.on_change()
        return text

    def retire(args: dict) -> str:
        name = _text(args, "member")
        member = store.member(run, name)
        if member is None or name == LEAD:
            raise ValueError(f"no member {name!r}")
        if member.status == "working":
            raise ValueError(f"{name} is working; retire it once it is idle")
        if member.status == "retired":
            return f"{name} is already retired"
        store.update_member(run, name, status="retired")
        for t in store.tasks(run):
            if t.assignee == name and t.status in _OPEN:
                store.update_task(run, t.id, assignee=None)
        store.log_event(run, "team_member", name, state="retired")
        ctx.decision(f"retired {name}")
        ctx.on_change()
        return f"{name} retired"

    def reassign(args: dict) -> str:
        task_id = _text(args, "task_id")
        member = args.get("member") or None
        task = store.task(run, task_id)
        if task is None:
            raise ValueError(f"no task {task_id}")
        if task.status not in _OPEN:
            raise ValueError(
                f"{task_id} is {task.status}; only pending, blocked or failed tasks move"
            )
        if member is not None:
            active(member)
        store.update_task(run, task_id, assignee=member, status="pending")
        store.log_event(run, "team_task", LEAD, task=task_id, status="pending", assignee=member)
        ctx.decision(f"{task_id} → {member or 'anyone'}")
        ctx.on_change()
        return f"{task_id} is pending for {member or 'anyone'}"

    def broadcast(args: dict) -> str:
        count = ctx.mailbox.broadcast(run, LEAD, _text(args, "text"))
        return f"sent to {count} members"

    string, ids = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
    return [
        *_common(ctx),
        _tool(ctx, "board_add", "Add a task to the board (kind code or research).",
              _schema({"title": string, "kind": {"type": "string", "enum": list(TASK_KINDS)},
                       "detail": string, "deps": ids, "member": string}, ("title",)),
              board_add),
        _tool(ctx, "board_split", "Replace a pending, blocked or failed task by smaller tasks.",
              _schema({"task_id": string,
                       "parts": {"type": "array", "items": _schema(
                           {"title": string, "kind": string, "detail": string}, ("title",))}},
                      ("task_id", "parts")),
              board_split),
        _tool(ctx, "hire", "Hire a member: a profile name or a prompt, tools, skills, model.",
              _schema({"name": string, "profile": string, "prompt": string, "tools": ids,
                       "skills": ids, "model": string}, ("name",)),
              hire),
        _tool(ctx, "retire", "Retire an idle member; its open tasks become unassigned.",
              _schema({"member": string}, ("member",)), retire),
        _tool(ctx, "reassign", "Give a pending, blocked or failed task to a member (or anyone).",
              _schema({"task_id": string, "member": string}, ("task_id",)), reassign),
        _tool(ctx, "broadcast", "Message every member at once (lead only).",
              _schema({"text": string}, ("text",)), broadcast),
    ]  # fmt: skip
