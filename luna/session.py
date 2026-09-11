"""Streaming REPL / one-shot session loop with approval handling.

Together with :mod:`luna.agent` this is the only module that touches
``deepagents`` / ``langgraph`` directly.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command
from rich.console import Console

from luna import diagnose, fmt, gitinfo, permissions, undo, usercmd
from luna.commands import HELP as SLASH_COMMANDS
from luna.commands import CommandContext, dispatch
from luna.config import LunaConfig
from luna.context import PinnedFiles, expand_mentions, render_pinned
from luna.permissions import load_rules
from luna.persistence import SessionIndex, make_title
from luna.subagents import subagent_summaries
from luna.ui.approve import prompt_decision
from luna.ui.theme import PALETTE
from luna.ui.turn import close_turn, open_turn, tool_line
from luna.usage import SessionUsage, TurnUsage, indicator_line, price
from luna.verify import run_verify

__all__ = [
    "SLASH_COMMANDS",
    "collect_decisions",
    "compact_thread",
    "run_once",
    "run_repl",
]

_RELOAD_MARKER = "Run /reload"

#: Tool names whose use marks a turn as mutating and triggers verification.
_MUTATING = {"write_file", "edit_file", "delete", "execute"}

#: Matches a non-slash line invoking a subagent by name, e.g. ``@researcher do X``.
_AT_AGENT_RE = re.compile(r"^@([\w-]+)\s+(.+)$", re.DOTALL)


def _new_thread_id() -> str:
    return uuid.uuid4().hex


_COMPACT_ASK = (
    "Summarise this whole session as a dense handoff note: the goal, decisions "
    "made, files touched, current state, and open questions. Text only — do not "
    "call any tools. No preamble."
)


def compact_thread(agent, thread_id: str, console: Console) -> None:
    """Replace this thread's message history with a model-written summary, in place."""
    config = {"configurable": {"thread_id": thread_id}}
    pre = agent.get_state(config).values.get("messages", [])
    result = agent.invoke({"messages": [{"role": "user", "content": _COMPACT_ASK}]}, config)
    if isinstance(result, dict) and result.get("__interrupt__"):
        result = agent.invoke(
            Command(resume={"decisions": [{"type": "reject", "message": "summary only"}]}),
            config,
        )
    summary = ""
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for msg in reversed(messages):
        text = getattr(msg, "content", "")
        if getattr(msg, "type", "") == "ai" and isinstance(text, str) and text.strip():
            summary = text.strip()
            break
    if not summary:
        added = agent.get_state(config).values["messages"][len(pre) :]
        agent.update_state(config, {"messages": [RemoveMessage(id=m.id) for m in added]})
        console.print(f"[{PALETTE['mauve']}]/compact: no summary produced[/]")
        return
    agent.update_state(
        config,
        {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                HumanMessage(id=uuid.uuid4().hex, content="[compacted] Handoff note:\n" + summary),
            ]
        },
    )
    console.print(f"[{PALETTE['blue']}]compacted — history replaced with a summary[/]")


def collect_decisions(
    console: Console,
    interrupt_value: dict,
    *,
    input_fn: Callable[[str], str] = input,
    rules=None,
    workdir=None,
) -> dict:
    """Turn an interrupt payload into a ``Command(resume=...)`` argument.

    ``rules`` (a :class:`~luna.permissions.RuleSet`) auto-approves any request
    whose ``(tool, args)`` matches an ``allow`` rule. A decision carrying an
    ``"always"`` key is persisted as a project rule and folded into ``rules``.
    """
    requests = interrupt_value.get("action_requests")
    if requests is None and "action_request" in interrupt_value:
        requests = [interrupt_value["action_request"]]

    decisions: list[dict] = []
    for request in requests or []:
        name = request.get("action") or request.get("name")
        args = request.get("args", {}) or {}
        verdict = rules.match(name, args) if rules is not None else None
        if verdict == "allow":
            console.print(f"[dim]⚙ {name} · auto (rule)[/]")
            decisions.append({"type": "approve"})
        elif verdict == "deny":
            console.print(f"[dim]⚙ {name} · blocked (rule)[/]")
            decisions.append(
                {
                    "type": "reject",
                    "message": f"blocked by a Luna permission rule ({name})",
                }
            )
        else:
            decisions.append(prompt_decision(console, request, input_fn=input_fn))

    for d in decisions:
        if "always" in d:
            if workdir is not None:
                permissions.append_project_rule(workdir, d["always"])
            if rules is not None and d["always"] not in rules.allow:
                rules.allow.append(d["always"])
            d.pop("always", None)

    return {"decisions": decisions}


def _iter_interrupts(chunk: object):
    if isinstance(chunk, dict) and "__interrupt__" in chunk:
        yield from chunk["__interrupt__"]


def _report_tools(chunk: dict, console: Console, seen: set[str], names: set[str]) -> bool:
    """Print tool lines; return True if a tool asked for /reload.

    Every reported tool's name is added to ``names``.
    """
    reload_requested = False
    for update in chunk.values():
        if not isinstance(update, dict):
            continue
        for msg in update.get("messages", []) or []:
            if isinstance(msg, ToolMessage) and msg.tool_call_id not in seen:
                seen.add(msg.tool_call_id)
                if msg.name:
                    names.add(msg.name)
                body = str(msg.content) if msg.content else ""
                tool_line(console, msg.name or "tool", body.splitlines()[0][:120])
                if msg.name in ("manage_mcp", "manage_skills") and _RELOAD_MARKER in body:
                    reload_requested = True
    return reload_requested


def _stream_turn(
    agent,
    payload,
    config: dict,
    console: Console,
    input_fn: Callable[[str], str],
    *,
    rules=None,
    workdir: str = ".",
) -> tuple[str, bool, TurnUsage, set[str]]:
    """Run one user turn.

    Returns ``(final_text, reload_requested, turn_usage, tool_names_seen)``.
    """
    parts: list[str] = []
    seen_tools: set[str] = set()
    tool_names_seen: set[str] = set()
    reload_requested = False
    turn_usage = TurnUsage()

    open_turn(console)
    while True:
        interrupts: list = []
        for mode, chunk in agent.stream(
            payload, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                msg, meta = chunk
                if meta.get("langgraph_node") == "model" and isinstance(
                    msg, (AIMessage, AIMessageChunk)
                ):
                    turn_usage.merge(getattr(msg, "usage_metadata", None))
                    text = msg.content if isinstance(msg.content, str) else ""
                    if text:
                        parts.append(text)
                        console.print(text, end="", soft_wrap=True)
            elif mode == "updates":
                interrupts.extend(_iter_interrupts(chunk))
                reload_requested |= _report_tools(chunk, console, seen_tools, tool_names_seen)

        if not interrupts:
            state = agent.get_state(config)
            interrupts = list(getattr(state, "interrupts", ()) or [])
        if not interrupts:
            break

        console.print()
        resume = collect_decisions(
            console,
            interrupts[0].value,
            input_fn=input_fn,
            rules=rules,
            workdir=workdir,
        )
        payload = Command(resume=resume)

    close_turn(console)
    return "".join(parts).strip(), reload_requested, turn_usage, tool_names_seen


def _format_and_diagnose(console: Console, cfg: LunaConfig, before: list[str] | None = None) -> str:
    """Format then diagnose the files this turn changed. Returns diagnose text.

    ``before`` is the ``dirty_paths`` snapshot captured before the turn ran; only
    paths that became newly dirty during the turn are passed to the format/
    diagnose commands, so a file the user had already changed before this turn
    started is left alone. ``None`` falls back to the old whole-repo behavior
    (used only by direct callers/tests that don't have a "before" snapshot).
    """
    before_set = set(before) if before is not None else None
    is_repo = gitinfo.is_git_repo(cfg.workdir)

    def _touched_now() -> list[str]:
        if not is_repo:
            return []
        current = gitinfo.dirty_paths(cfg.workdir)
        return current if before_set is None else [p for p in current if p not in before_set]

    changed = _touched_now()
    if is_repo and before_set is not None and not changed:
        # This IS a scoped (git) call and this turn didn't newly dirty anything —
        # nothing to format/diagnose. Outside a git repo, `changed` is always []
        # regardless of `before`, and the format/diagnose commands legitimately
        # run bare there (there's no dirty-path scoping without git) — so this
        # early return must not fire for that case, only for a genuine empty delta.
        return ""
    fmt_cmd = cfg.format_command
    if fmt_cmd == "auto":
        fmt_cmd = fmt.detect(cfg.workdir)
    if fmt_cmd:
        touched = fmt.run(fmt_cmd, cfg.workdir, changed)
        if touched:
            console.print(f"[dim]⌁ formatted {len(touched)} file(s)[/]")
    diag_cmd = cfg.diagnose_command
    if diag_cmd == "auto":
        diag_cmd = diagnose.detect(cfg.workdir)
    if not diag_cmd:
        return ""
    changed = _touched_now()
    if is_repo and before_set is not None and not changed:
        # Same guard as above: formatting can normalize a turn's edit back to
        # exactly the committed content, making this recomputed `changed` empty
        # even though the first check above passed — must not fall through to
        # diagnose.run's "no paths -> whole project" convention either.
        return ""
    text = diagnose.run(diag_cmd, cfg.workdir, changed)
    if text:
        console.print(f"[dim]{text}[/]")
    return text


def _run_verification(
    agent, turn_config: dict, console: Console, cfg, input_fn, rules=None
) -> None:
    """Run the verify command; on failure, take exactly one fix-up turn.

    ``turn_config`` is the ``{"configurable": {"thread_id": ...}}`` dict for the
    active thread. Does nothing when no verify command is configured.
    """
    if not cfg.verify_command:
        return
    ok, tail = run_verify(cfg.verify_command, cfg.workdir)
    if ok:
        console.print("[dim]✓ verify ok[/]")
        return
    console.print(f"[yellow]verify failed[/]\n{tail}")
    payload = {
        "messages": [
            {
                "role": "user",
                "content": (
                    f"The verify command `{cfg.verify_command}` failed. Output:\n{tail}\nFix it."
                ),
            }
        ]
    }
    _stream_turn(agent, payload, turn_config, console, input_fn, rules=rules, workdir=cfg.workdir)
    ok, tail = run_verify(cfg.verify_command, cfg.workdir)
    console.print(
        "[dim]✓ verify ok[/]" if ok else f"[yellow]⚠ verify still failing after 1 retry[/]\n{tail}"
    )


def run_once(
    agent,
    prompt: str,
    *,
    thread_id: str | None = None,
    console: Console,
    input_fn: Callable[[str], str] = input,
    index: SessionIndex | None = None,
    workdir: str = ".",
    session_id: str = "",  # accepted for API symmetry; snapshots run in the middleware
    cfg: LunaConfig | None = None,
    output_format: str = "text",
) -> str:
    """Run a single prompt and return the final assistant text (or "" in json mode)."""
    thread_id = thread_id or _new_thread_id()
    config = {"configurable": {"thread_id": thread_id}}
    payload = {"messages": [{"role": "user", "content": prompt}]}
    rules = load_rules(workdir)
    quiet = Console(file=open(os.devnull, "w")) if output_format == "json" else console
    try:
        current_messages = agent.get_state(config).values.get("messages", [])
    except Exception:  # noqa: BLE001 - a stub/broken agent must not block the turn
        current_messages = []
    undo.begin_turn(workdir, session_id, len(current_messages))
    # captured *after* begin_turn so its own journal writes don't register as
    # "newly dirty" when .luna/ isn't gitignored
    dirty_before_turn = gitinfo.dirty_paths(workdir) if gitinfo.is_git_repo(workdir) else []
    text, _, turn_usage, tool_names = _stream_turn(
        agent,
        payload,
        config,
        quiet,
        input_fn,
        rules=rules,
        workdir=workdir,
    )
    if cfg is not None and tool_names & _MUTATING:
        _format_and_diagnose(quiet, cfg, before=dirty_before_turn)
        _run_verification(agent, config, quiet, cfg, input_fn, rules=rules)
    if index is not None:
        index.record(thread_id, workdir, make_title(prompt))
        index.touch(thread_id)
    if output_format == "json":
        provider = cfg.provider if cfg is not None else ""
        model = cfg.model if cfg is not None else None
        overrides = cfg.pricing if cfg is not None else None
        p = price(provider, model, overrides)
        cost_usd = (
            None
            if p is None
            else turn_usage.input_tokens / 1_000_000 * p[0]
            + turn_usage.output_tokens / 1_000_000 * p[1]
        )
        payload_out = {
            "text": text,
            "tools_used": sorted(tool_names),
            "usage": {
                "input": turn_usage.input_tokens,
                "output": turn_usage.output_tokens,
                "total": turn_usage.total_tokens,
            },
            "cost_usd": cost_usd,
            "thread_id": thread_id,
        }
        print(json.dumps(payload_out))
        return ""
    return text


def _print_recap(agent, config, console, keep=6):
    """Print the tail of a resumed thread's history (best effort)."""
    try:
        msgs = agent.get_state(config).values.get("messages", [])
    except Exception:  # noqa: BLE001 - recap is cosmetic
        return
    if not msgs:
        return
    console.print(f"[{PALETTE['blue']}]— resuming, last {min(keep, len(msgs))} messages —[/]")
    for m in msgs[-keep:]:
        role = getattr(m, "type", "?")
        text = (getattr(m, "content", "") or "")[:200]
        if text:
            console.print(f"[dim]{role}:[/] {text}")


def run_repl(
    agent,
    *,
    console: Console,
    input_fn: Callable[[str], str] = input,
    rebuild: Callable[[], object] | None = None,
    index: SessionIndex | None = None,
    thread_id: str | None = None,
    workdir: str = ".",
    config: LunaConfig | None = None,
    session_id: str = "",
    plan_state: list | None = None,
) -> int:
    """Interactive loop. Returns a process exit code."""
    thread_id = thread_id or _new_thread_id()
    config = config or LunaConfig()
    plan_state = plan_state if plan_state is not None else [False]
    console.print(f"[{PALETTE['peri']}]Luna is ready. Type /help for commands.[/]\n")

    session_usage = SessionUsage()
    pinned = PinnedFiles()
    rules = load_rules(workdir)
    user_commands = usercmd.load(workdir)
    subagent_names = {n for n, _ in subagent_summaries(workdir)}
    ctx = CommandContext(
        console=console,
        config=config,
        agent=agent,
        rebuild=rebuild,
        thread_id=thread_id,
        workdir=workdir,
        index=index,
        session_id=session_id,
        usage=session_usage,
        pinned=pinned,
        permissions=rules,
        input_fn=input_fn,
        user_commands=user_commands,
        plan_state=plan_state,
    )

    if index is not None:
        _print_recap(agent, {"configurable": {"thread_id": thread_id}}, console)

    pending_diagnostics = ""
    while True:
        try:
            prompt_label = "luna (plan) › " if plan_state[0] else "luna › "
            line = input_fn(prompt_label).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0

        if not line:
            continue

        if line.startswith("/"):
            ctx.agent = agent
            ctx.thread_id = thread_id
            res = dispatch(line, ctx)
            if res.exit:
                return 0
            if res.prompt is not None:
                line = res.prompt  # fall through to the normal turn-building code below
            elif res.handled:
                if res.agent is not None:
                    agent = res.agent
                    rules = load_rules(workdir)
                    ctx.permissions = rules
                    user_commands = usercmd.load(workdir)
                    ctx.user_commands = user_commands
                    subagent_names = {n for n, _ in subagent_summaries(workdir)}
                if res.thread_id is not None:
                    thread_id = res.thread_id
                continue

        at_match = _AT_AGENT_RE.match(line)
        if at_match and at_match.group(1) in subagent_names:
            line = (
                f"Delegate this to the '{at_match.group(1)}' subagent using the "
                f"task tool: {at_match.group(2)}"
            )

        turn_config = {"configurable": {"thread_id": thread_id}}
        try:
            current_messages = agent.get_state(turn_config).values.get("messages", [])
        except Exception:  # noqa: BLE001 - a stub/broken agent must not block the turn
            current_messages = []
        undo.begin_turn(workdir, session_id, len(current_messages))
        # captured *after* begin_turn so its own journal writes don't register as
        # "newly dirty" when .luna/ isn't gitignored
        dirty_before_turn = gitinfo.dirty_paths(workdir) if gitinfo.is_git_repo(workdir) else []
        diag_block = (
            f"<diagnostics>\n{pending_diagnostics}\n</diagnostics>\n\n"
            if pending_diagnostics
            else ""
        )
        pending_diagnostics = ""
        pinned_block = render_pinned(pinned, workdir)
        expanded = expand_mentions(line, workdir)
        content = diag_block + (pinned_block + "\n\n" if pinned_block else "") + expanded
        payload = {"messages": [{"role": "user", "content": content}]}
        try:
            _, reload_requested, turn_usage, tool_names = _stream_turn(
                agent, payload, turn_config, console, input_fn, rules=rules, workdir=workdir
            )
        except KeyboardInterrupt:
            console.print(f"\n[{PALETTE['mauve']}]turn cancelled[/]")
            continue
        except Exception as exc:  # noqa: BLE001 - a provider/network/persistence error must not kill the session
            console.print(f"[{PALETTE['mauve']}]turn failed: {exc}[/]")
            continue
        before = len(session_usage.turns)
        session_usage.add_turn(turn_usage)
        if len(session_usage.turns) > before:
            indicator = indicator_line(session_usage, config.provider, config.model, config.pricing)
            console.print(f"[dim]{indicator}[/]")
        if tool_names & _MUTATING:
            pending_diagnostics = _format_and_diagnose(console, config, before=dirty_before_turn)
            try:
                _run_verification(agent, turn_config, console, config, input_fn, rules=rules)
            except KeyboardInterrupt:
                console.print(f"\n[{PALETTE['mauve']}]verify fix-up cancelled[/]")
        if index is not None:
            index.record(thread_id, workdir, make_title(line))
            index.touch(thread_id)
        if reload_requested and rebuild is not None:
            try:
                agent = rebuild()
            except Exception as exc:  # noqa: BLE001 - a bad config must not kill the session
                console.print(f"[{PALETTE['mauve']}]auto-reload failed: {exc}[/]")
            else:
                console.print(f"[{PALETTE['blue']}]auto-reloaded — new capabilities are live[/]")
