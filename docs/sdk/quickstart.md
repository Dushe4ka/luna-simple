Русское руководство по SDK: [README.ru.md](README.ru.md)

# Quickstart

`luna-core` is an async agent runtime: you give it a model provider, a folder and
a few tools, and it runs the agent loop (model call, tool calls, approvals, repeat)
while streaming typed events to you. Everything below is exported from `luna_core`.

## Install

```bash
pip install luna-core                  # httpx, httpx-sse, pyyaml
pip install "luna-core[mcp]"           # MCP servers as tools
pip install "luna-core[acp]"           # serve the agent to editors over ACP
pip install "luna-core[lsp,websearch]" # optional extras
```

Python 3.11+. There are no vendor SDKs: providers talk to the HTTP APIs directly.

## One turn

```python
import asyncio

from luna_core import (
    Agent, Decision, EventLog, NeedsApproval, Policy, Session,
    TextDelta, ToolStarted, TurnDone, Workspace, providers,
)


async def main() -> None:
    provider, model = providers.resolve("anthropic")  # reads ANTHROPIC_API_KEY
    workspace = Workspace(".")
    agent = Agent(provider, model, workspace=workspace, policy=Policy(workspace=workspace))
    log = EventLog(".luna/example.db")
    session = Session.create(log, str(workspace.root))
    async for event in agent.run(session, "List the files and say what this project is."):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ToolStarted):
            print(f"\n[{event.name} {event.preview}]")
        elif isinstance(event, NeedsApproval):
            session.decide(event.request.call_id, Decision.reject("read-only demo"))
        elif isinstance(event, TurnDone):
            print(f"\n{event.usage.input + event.usage.output} tokens")


asyncio.run(main())
```

Run it offline: `python examples/minimal_agent.py --fake --workdir .` replaces the
provider with a scripted `FakeProvider`.

## Non-async callers

`agent.run_sync(session, text, approve=callback)` returns the list of events;
`approve` receives an `ApprovalRequest` and returns a `Decision`. Without a callback
any approval is rejected.

## Where next

- [concepts](concepts.md) for how the pieces fit together
- [providers](providers.md), [tools](tools.md), [sessions](sessions.md)
- [approvals and hooks](approvals-and-hooks.md)
- [subagents and background work](subagents-and-background.md)
- [MCP and ACP](mcp-and-acp.md)
- [wire protocol](../protocol.md) when you consume events from another language
- runnable programs in `examples/`
