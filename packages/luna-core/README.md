# luna-core

The agent runtime behind [Luna](https://github.com/Dushe4ka/luna-simple), usable as an SDK:
an async agent loop, built-in coding tools, event-log sessions, provider adapters
(OpenAI-compatible, OpenAI Responses, Anthropic, Google, GigaChat), approvals,
hooks, sandboxes, subagents, background agents, MCP and ACP. No LangChain, no vendor SDKs.

## Install

```bash
pip install luna-core                 # httpx, httpx-sse, pyyaml
pip install "luna-core[mcp]"          # MCP servers as tools
pip install "luna-core[acp]"          # serve over ACP (Zed, JetBrains)
pip install "luna-core[lsp,websearch]"
```

Python 3.11+.

## Minimal example

```python
import asyncio

from luna_core import (
    Agent,
    EventLog,
    Policy,
    Session,
    TextDelta,
    Workspace,
    providers,
)


async def main() -> None:
    provider, model = providers.resolve("anthropic")  # ANTHROPIC_API_KEY
    workspace = Workspace(".")
    agent = Agent(provider, model, workspace=workspace, policy=Policy(workspace=workspace))
    session = Session.create(EventLog(".luna/sessions.db"), str(workspace.root))
    async for event in agent.run(session, "What is this project?"):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)


asyncio.run(main())
```

Tools that change files or run commands ask for approval; answer a `NeedsApproval`
event with `session.decide(...)` (see the approvals page) or the turn waits.

## Documentation

- SDK guides: [`docs/sdk/`](../../docs/sdk/quickstart.md) (quickstart, concepts, providers,
  tools, sessions, approvals and hooks, subagents and background work, MCP and ACP)
- Wire protocol and event schema: [`docs/protocol.md`](../../docs/protocol.md)
- Runnable examples (offline with `--fake`): [`examples/`](../../examples)

The public API is the set of names in `luna_core.__all__`; it is semver-stable from 0.1.0.
