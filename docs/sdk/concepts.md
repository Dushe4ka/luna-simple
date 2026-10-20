# Concepts

| Concept | Class | Role |
|---|---|---|
| Provider | any object with `name` and `async stream(request)` | Talks to a model API and yields model events |
| Agent | `Agent` | Configuration (model, tools, policy, hooks, sandbox) plus `run(session, text)` |
| Session | `Session` over `EventLog` | A conversation stored as an append-only SQLite event log |
| Tool | `Tool`, built with `@tool` | A function the model may call, with a JSON schema |
| Policy | `Policy`, `Rules` | Decides per call: run, refuse, or ask a human |
| Workspace | `Workspace` | The project root and the rules for resolving agent paths |
| Sandbox | `LocalSandbox`, `DockerSandbox` | Where `execute` runs shell commands |
| Hooks | `Hooks`, `HookResult` | Callbacks around the loop (`pre_tool`, `turn_end`, ...) |

## The loop

`agent.run(session, text)` is an async generator. One call is one turn:

1. `TurnStarted` is yielded and the undo snapshot is taken; on the first turn of a session
   the `session_start` hooks run; then the user message is appended to the log.
2. Compaction runs if the context is over 80% of the model window.
3. The request is streamed from the provider; `TextDelta` / `ThinkingDelta` events are yielded.
4. Each tool call is checked by the policy. Calls that need approval yield
   `NeedsApproval` and the turn waits for `session.decide(call_id, decision)`.
5. Allowed calls run (reads concurrently, writes to one path serialised); the results
   are appended and the loop repeats until the model stops or `max_steps` is reached.
6. The turn ends with exactly one of `TurnDone`, `TurnError` or `Cancelled`.

A failing tool never raises out of `run`: it becomes an error result the model sees. A
`ProviderError` becomes `TurnError`; any other exception from a provider (a bug, or an
exhausted `FakeProvider`) propagates out of `run`.

## Events

Turn events are the public protocol: `TurnStarted`, `TextDelta`, `ThinkingDelta`,
`ToolStarted`, `ToolFinished`, `NeedsApproval`, `TodosUpdated`, `Notice`, `UsageUpdate`,
`Compacted`, `BackgroundDone`, `ReloadRequested`, `TurnDone`, `TurnError`, `Cancelled`.
`to_wire(event)` turns any of them into the JSON form described in
[protocol.md](../protocol.md).

## Messages

`Message` holds blocks: `Text`, `Thinking` (with the provider signature needed to
round-trip reasoning), `Image`, `ToolCall`, `ToolResult`. `image_from_file(path)` builds an
`Image`; pass images to a turn with `agent.run(session, text, images=[...])`.

## Errors

`LunaCoreError` is the base class. `ProviderError` has a `kind` (`auth`, `rate_limit`,
`context_length`, `bad_request`, `server`, `network`), `status` and `retry_after`.
`ToolError` (and its subclasses such as `OutsideWorkspace`) is what tool code raises to
produce an error result.

## Usage and cost

`Usage(input, output, cache_read, cache_write, reasoning)` is attached to `UsageUpdate`
and `TurnDone`. `session.usage()` and `session.cost()` accumulate over the session; cost comes
from the packaged `models.toml` catalogue (override with `Agent(model_overrides=...)`) and
is `None` for a model without a price. `Agent(max_cost=...)` stops the loop at a budget.
