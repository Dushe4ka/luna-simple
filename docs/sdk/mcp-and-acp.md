# MCP и ACP

## MCP-серверы как инструменты (extra `mcp`)

`pip install "luna-core[mcp]"` (официальный SDK `mcp`, `>=1.12,<2`). `luna_core.mcp` не входит
в публичный API, импортируемый сразу, поэтому импортируйте его явно:

```python
from pathlib import Path

from luna_core import Agent, builtin_tools
from luna_core.mcp import MCPManager, load_config

servers = load_config([Path(".luna/mcp.json")])        # {"mcpServers": {...}}, ${ENV} из os.environ
manager = MCPManager(servers, on_warn=print)
mcp_tools = await manager.start()                      # подключается параллельно, возвращает Tools
try:
    agent = Agent(provider, model, tools=[*builtin_tools(), *mcp_tools])
    ...
finally:
    await manager.close()
```

Формат файла обычный: `{"mcpServers": {"name": {"command": ..., "args": [...]}}}`
(stdio) или `{"type": "http", "url": ...}` (streamable HTTP; `"type": "sse"` для SSE), OAuth-токены
хранятся в `~/.config/luna/mcp-tokens` (права 0600). Сервер, к которому не удалось подключиться,
пропускается с предупреждением; `manager.status()` сообщает по каждому серверу `connected` или
`failed: ...`. Stdio-серверы не наследуют ваше окружение, только то, что задано в `env` конфигурации.

Подводный камень: если extra не установлен, `MCPManager.start()` не бросает исключение; он
предупреждает через `on_warn` с подсказкой по установке и не возвращает инструментов.

## ACP для редакторов (extra `acp`)

`pip install "luna-core[acp]"` (`agent-client-protocol>=0.4`). `run_acp` отдаёт агента по
stdio для Zed, JetBrains и других ACP-клиентов:

```python
import asyncio
from luna_core import Agent, EventLog, Policy, Workspace, providers
from luna_core.acp import run_acp


def make_agent(cwd: str) -> Agent:                    # один агент на папку сессии редактора
    provider, model = providers.resolve("anthropic")
    workspace = Workspace(cwd)
    return Agent(provider, model, workspace=workspace, policy=Policy(workspace=workspace))


asyncio.run(run_acp(make_agent, EventLog("acp.db")))
```

Что сопоставляется: `new_session` / `load_session` сессиям, `prompt` ходу, `cancel`
`session.cancel_turn()`; текст и рассуждения чанкам сообщений, вызовы инструментов
`tool_call` / `tool_call_update`, todos `plan`, расход `usage_update`; подтверждения
`session/request_permission` (разрешить один раз, разрешить всегда, отклонить).

Подводные камни:

- Stdout это канал протокола: никогда не печатайте в него через `print`, и это должен быть pipe
  (CLI отказывается работать с файлом и объясняет почему). Пишите логи в stderr.
- `luna acp` в приложении Luna оборачивает это конфигурацией приложения; `examples/acp_agent.py`
  это минимальная встроенная версия, а `--self-test` играет против неё роль редактора.
- Конфигурация редактора (Zed): `"agent_servers": {"Luna": {"command": "luna", "args": ["acp"]}}`.

## Удалённые клиенты

Для работающего `luna serve` используйте `luna_core.client.LunaClient(base_url, token)`; wire-формат
описан в [protocol.md](../protocol.md), а `examples/remote_client.py` показывает полную сессию.
