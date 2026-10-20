Полное руководство одной страницей: [README.ru.md](README.ru.md). Эти страницы служат справочником по отдельным темам.

# Быстрый старт

`luna-core` это асинхронный рантайм агента: вы передаёте ему провайдера модели, папку и
несколько инструментов, а он запускает цикл агента (запрос к модели, вызовы инструментов,
подтверждения, повтор) и потоково отдаёт вам типизированные события. Всё, что показано ниже,
экспортируется из `luna_core`.

## Установка

```bash
pip install luna-core                  # httpx, httpx-sse, pyyaml
pip install "luna-core[mcp]"           # MCP-серверы как инструменты
pip install "luna-core[acp]"           # отдавать агента редакторам по ACP
pip install "luna-core[lsp,websearch]" # необязательные extras
```

Python 3.11+. Вендорских SDK нет: провайдеры обращаются к HTTP API напрямую.

## Один ход

```python
import asyncio

from luna_core import (
    Agent, Decision, EventLog, NeedsApproval, Policy, Session,
    TextDelta, ToolStarted, TurnDone, Workspace, providers,
)


async def main() -> None:
    provider, model = providers.resolve("anthropic")  # читает ANTHROPIC_API_KEY
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

Запуск офлайн: `python examples/minimal_agent.py --fake --workdir .` подменяет
провайдера скриптовым `FakeProvider`.

## Синхронный вызов

`agent.run_sync(session, text, approve=callback)` возвращает список событий;
`approve` получает `ApprovalRequest` и возвращает `Decision`. Без колбэка
любое подтверждение отклоняется.

## Что дальше

- [concepts](concepts.md): как устроены части вместе
- [providers](providers.md), [tools](tools.md), [sessions](sessions.md)
- [подтверждения и хуки](approvals-and-hooks.md)
- [субагенты и фоновая работа](subagents-and-background.md)
- [MCP и ACP](mcp-and-acp.md)
- [wire-протокол](../protocol.md), если вы читаете события из другого языка
- запускаемые программы в `examples/`
