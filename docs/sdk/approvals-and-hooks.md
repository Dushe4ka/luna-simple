# Approvals and hooks

## Policy

`Policy` decides for every tool call whether to run it, refuse it, or ask a human:

```python
from luna_core import Agent, Policy, Rules, Workspace

workspace = Workspace(".")
policy = Policy(
    Rules.load([...]),                 # list of TOML paths with a [permissions] table
    workspace=workspace,
    yolo=False,                        # True skips approvals (deny rules still apply)
)
agent = Agent(provider, model, workspace=workspace, policy=policy)
```

- By default `write_file`, `edit_file`, `delete` and `execute` need approval
  (`approval_tools`); reads run freely.
- `Rules` come from `[permissions] allow = [...] / deny = [...]` entries such as
  `"execute:pytest*"` or `"write:.env"`. Deny wins, even over `yolo`.
- `plan_mode=True` (or a callable returning a bool) refuses every mutating call.
- Writes and edits to a file that changed on disk since the agent last read or wrote it are
  refused with a message asking it to read the file again (the stale-anchor check).

## Answering approvals

When a call needs a decision the turn yields `NeedsApproval(request)` and then waits:

```python
from luna_core import Decision, NeedsApproval

async for event in agent.run(session, text):
    if isinstance(event, NeedsApproval):
        r = event.request                          # call_id, name, args, description, suggested_rule
        session.decide(r.call_id, Decision.approve())
        # Decision.approve(always="execute:pytest*")  also remembers the rule
        # Decision.edit({"command": "pytest -q"})      run with changed arguments
        # Decision.reject("not in this repo")          the model sees the message
```

A plain dict works too: `{"type": "approve"}`, `{"type": "edit", "args": {...}}`,
`{"type": "reject", "message": "..."}`, with an optional `"always": "<rule>"`.
Pass `persist_rule=fn` to `Policy` to store "always" rules (for example
`luna_core.policy.append_rule(path, rule)` into `.luna/permissions.toml`).

Pitfalls: **an unanswered approval keeps the turn waiting**; `session.cancel_turn()` (or
closing the generator) abandons the whole batch and runs nothing from it. In
`run_sync` without an `approve` callback approvals are rejected. Approvals from subagents
are routed through the parent session, so answer them on the session you ran.
See `examples/approvals_web.py` for approvals over HTTP.

## Hooks

Hooks are callbacks at five points: `session_start`, `pre_tool`, `post_tool`, `turn_end`,
`pre_compact`.

```python
from luna_core import Agent, HookResult, Hooks

hooks = Hooks()


async def no_force_push(payload: dict) -> HookResult | None:
    # payload: {"event": "pre_tool", "tool": "execute", "args": {...}, "session_id": ...}
    if "push --force" in payload["args"].get("command", ""):
        return HookResult(block=True, message="force-push is not allowed")


hooks.add("pre_tool", no_force_push, match="execute", timeout=5)
agent = Agent(provider, model, hooks=hooks)
```

`HookResult` fields: `block` + `message` (the message goes back to the model), `args`
(replace the tool arguments), `context` (text added to the conversation), `notices`.
Hooks run in registration order; the first one that blocks stops the chain. An exception or
timeout becomes a notice and never blocks the turn.

### Command hooks from config

```toml
[[hooks]]
event = "post_tool"
match = "write_file"      # optional tool-name glob
command = "ruff format ."
timeout = 30
```

`luna_core.hooks.load_command_hooks(entries, cwd)` turns such entries into a `Hooks`;
merge with `hooks.extend(other)`. The command gets the JSON payload on stdin. Exit 0
continues (stdout may add context), exit 2 blocks (stderr goes to the model), anything else
is a warning. A timeout kills the whole process group.

## Cancelling

`session.cancel_turn()` stops the in-flight request and running tools (process groups are
killed), records `Cancelled` and patches dangling tool calls so the next turn works.
