# MCP and ACP

## MCP servers as tools (extra `mcp`)

`pip install "luna-core[mcp]"` (the official `mcp` SDK, `>=1.12,<2`). `luna_core.mcp` is not
part of the eager public API, so import it explicitly:

```python
from pathlib import Path

from luna_core import Agent, builtin_tools
from luna_core.mcp import MCPManager, load_config

servers = load_config([Path(".luna/mcp.json")])        # {"mcpServers": {...}}, ${ENV} expanded
manager = MCPManager(servers, on_warn=print)
mcp_tools = await manager.start()                      # connects concurrently, returns Tools
try:
    agent = Agent(provider, model, tools=[*builtin_tools(), *mcp_tools])
    ...
finally:
    await manager.close()
```

The file format is the usual `{"mcpServers": {"name": {"command": ..., "args": [...]}}}`
(stdio) or `{"type": "http", "url": ...}` (streamable HTTP; `"type": "sse"` for SSE), with
OAuth tokens kept in `~/.config/luna/mcp-tokens` (mode 0600). A server that fails to connect is skipped with a
warning; `manager.status()` reports `connected` or `failed: ...` per server. Stdio servers do
not inherit your environment, only what the config's `env` sets.

Pitfall: without the extra installed, `MCPManager.start()` does not raise; it warns through
`on_warn` with the install hint and returns no tools.

## ACP for editors (extra `acp`)

`pip install "luna-core[acp]"` (`agent-client-protocol>=0.4`). `run_acp` serves an agent over
stdio for Zed, JetBrains and other ACP clients:

```python
import asyncio
from luna_core import Agent, EventLog, Policy, Workspace, providers
from luna_core.acp import run_acp


def make_agent(cwd: str) -> Agent:                    # one agent per editor session folder
    provider, model = providers.resolve("anthropic")
    workspace = Workspace(cwd)
    return Agent(provider, model, workspace=workspace, policy=Policy(workspace=workspace))


asyncio.run(run_acp(make_agent, EventLog("acp.db")))
```

What is mapped: `new_session` / `load_session` to sessions, `prompt` to a turn, `cancel` to
`session.cancel_turn()`; text and thinking to message chunks, tool calls to `tool_call` /
`tool_call_update`, todos to `plan`, usage to `usage_update`; approvals to
`session/request_permission` (allow once, allow always, reject).

Pitfalls:

- Stdout is the protocol channel: never `print` to it, and it must be a pipe (the CLI refuses
  a file with an explanation). Log to stderr.
- `luna acp` in the Luna app wraps this with the app's configuration; `examples/acp_agent.py`
  is the minimal embedded version, with `--self-test` playing an editor against it.
- Editor config (Zed): `"agent_servers": {"Luna": {"command": "luna", "args": ["acp"]}}`.

## Remote clients

For a running `luna serve` use `luna_core.client.LunaClient(base_url, token)`; the wire format
is in [protocol.md](../protocol.md) and `examples/remote_client.py` shows a complete session.
