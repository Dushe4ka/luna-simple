# Sessions

A session is a conversation stored as an append-only event log in SQLite (WAL mode).
`EventLog(path)` is the store shared by all sessions of a process; `Session` is a handle
on one conversation.

```python
from luna_core import EventLog, Session

log = EventLog("/var/lib/myapp/sessions.db")           # file and folders created on demand
session = Session.create(log, "/path/to/project", title="refactor")
same = Session.open(log, session.id)                 # later, or in another process
for info in log.list(workdir="/path/to/project"):    # SessionInfo, newest first
    print(info.id, info.title)
```

## Reading and rebuilding

| Call | Result |
|---|---|
| `session.messages()` | the model context rebuilt from events (compactions applied, dangling tool calls patched) |
| `session.entries()` / `session.records()` | transcript entries / raw log records |
| `session.usage()`, `session.cost()` | accumulated tokens and cost |
| `session.todos()` | the agent's current plan |
| `session.title` | first line of the first user message unless set |

## Control

| Call | Effect |
|---|---|
| `session.decide(call_id, decision)` | answer an approval (`Decision` or its dict form) |
| `session.pending_approvals` | call ids waiting for a decision (property) |
| `session.queue(text)` | deliver user text at the next step boundary (steering) |
| `session.cancel_turn()` | ask the running turn to stop; returns `False` when idle |
| `session.busy` | `True` while a turn runs (property) |
| `session.undo()` / `session.redo()` | revert or restore the last turn, conversation and files |
| `session.rewind(to_seq)` | drop the conversation after a sequence number |
| `session.fork(at_seq=None)` | a new session with the history up to `at_seq` |
| `session.export("md" \| "html" \| "json")` | the transcript as text |
| `await agent.side_question(session, text)` | answer over the context without tools and without logging (`/btw`) |

Undo uses a git tree snapshot per turn in git repositories and a file journal elsewhere.
`Session.create(..., undo=False)` disables it.

Undo and redo touch only what the turn changed. Paths changed by the file tools
(`write_file`, `edit_file`, `delete`) are restored; a path the user changed after the
turn is skipped and named in the result. When the turn ran `execute` (or another
mutating tool that is not a file tool), the whole tree diff of the turn is restored,
because a command can change any file, and every path outside the file tools is named
("also restored (changed by commands or outside Luna): …"). Known limitation: in such
a turn a file the user edited during the turn is restored too, but it is named. A turn
whose end was never recorded (a crash) restores only the file-tool paths. The SDK does not
check for background tasks: `Session.undo/redo/rewind` run regardless. The Luna app's
`/undo`, `/redo` and `/rewind` commands refuse while background tasks of the session run;
in your own code check `background.list(session.id)` for `queued`/`running` tasks first.

## Compaction

```python
from luna_core import Compactor

agent = Agent(provider, model, compactor=Compactor(provider=provider, model="a-cheap-model"))
```

At 80% of the context window the oldest messages are summarised by the model, the last four
user turns stay verbatim and the full history stays in the log. A `Compacted` event is
emitted. `Compactor(threshold=0.8, keep_turns=4)` are the defaults.

## Pitfalls

- One turn per session at a time: a second `agent.run` on a busy session raises
  `LunaCoreError`.
- An approval nobody answers keeps the turn (and the session) busy; cancel with
  `session.cancel_turn()`.
- `EventLog.list()` also returns child sessions (subagents, background runs, forks); filter
  on `parent_id` when you build a session picker.
- Close generators you stop reading early: `await events.aclose()` so the turn is cancelled
  and tools are stopped.
- Consumers share one `EventLog` per process; open separate logs per process only via the
  same file (SQLite handles the locking).
