# Subagents and background work

## Subagents

The `task(description, subagent_type, background, isolation)` tool runs a child `Agent` in a
fresh session branch with its own tool subset and model.

```python
from luna_core import Agent, BackgroundTasks, SubagentRunner, builtin_tools, task_tool
from luna_core.subagents import BUILTIN_SUBAGENTS, SubagentSpec
from luna_core.tools.tasks import task_cancel_tool, task_status_tool

specs = dict(BUILTIN_SUBAGENTS)          # researcher, reviewer (read-only), general-purpose
specs["tester"] = SubagentSpec(
    "tester", "Run the test suite and report failures.", "You run tests and report.",
    tools=("ls", "read_file", "grep", "execute"), unsafe=True,
)
runner = SubagentRunner(specs, max_parallel=4)
tools = [*builtin_tools(), task_tool(runner), task_status_tool(), task_cancel_tool()]
agent = Agent(provider, model, tools=tools, services={"background": BackgroundTasks()})
```

- `SubagentSpec(name, description, prompt, tools, model, unsafe, isolation, max_steps, max_cost)`.
  A subagent is read-only by default (`ls`, `read_file`, `glob`, `grep`). Mutating tools
  (`write_file`, `edit_file`, `delete`, `execute`) in a `subagents.toml` entry require
  `unsafe = true`, otherwise loading fails with `SubagentConfigError`.
- `load_subagents(files)` merges `[subagent.<name>]` tables from TOML files over the built-ins.
- Several `task` calls in one assistant message run concurrently, up to `max_parallel`.
- The child inherits policy, sandbox and hooks; its approvals are answered through the
  parent session. `max_cost` and the parent's remaining budget are checked before spawning.

### Worktree isolation

A writing subagent runs in its own git worktree on a `luna/sub/*` branch by default
(`isolation="auto"`), so parallel edits never collide. The parent gets a summary plus the
branch and diff to merge. Pitfalls:

- Worktrees need git, and the project root must be the git toplevel; otherwise `auto` falls
  back to the shared workspace (writes to one path are then serialised) and an explicit
  `isolation="worktree"` is an error.
- The snapshot taken for the worktree includes untracked, non-ignored files.
- A child that changed nothing leaves no branch behind.

## Background tasks

`task(..., background=true)` returns a handle at once and the parent continues. When the
child finishes, its result is appended to the log, a `BackgroundDone` event is emitted and the
text is delivered to the parent **at its next step boundary** (not instantly). `task_status`
and `task_cancel` inspect and stop a task; they only see tasks of their own session.

```python
from luna_core import BackgroundTasks

background = BackgroundTasks(max_parallel=4)
agent = Agent(..., services={"background": background})
handle = background.start("label", lambda: some_coroutine(), session=session)
info = await background.wait(handle)        # TaskInfo(status, summary, ...)
```

Finished entries are kept (the most recent 200) for status queries.

## Background sessions

`AgentManager` runs whole turns detached from any consumer, which is how a server keeps a run
alive after the UI closes:

```python
from luna_core import AgentManager

manager = AgentManager(max_parallel=4)
run = manager.start(agent, session, "refactor the parser", label="parser")
unsubscribe = manager.subscribe(session.id, print)     # wire-event dicts; replay with events()
await manager.wait(session.id)
```

`manager.events(session_id, after=-1)` replays buffered events for late attachers,
`manager.cancel(session_id)` stops a run, `manager.list()` gives `RunInfo` per run. The Luna
app exposes this as `luna run --background`, `/agents`, Ctrl+G in the TUI and
`POST /sessions/{id}/background`.

## Model roles

`ModelRoles(fast=..., plan=..., vision=..., summarize=...)` maps roles to model ids (or
`(provider, model)` pairs). Subagents default to `fast`, plan mode uses `plan`, image input
falls back to `vision` when the main model cannot see, and compaction uses `summarize`.
Pass it as `Agent(roles=...)`.
