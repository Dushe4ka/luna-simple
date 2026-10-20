Полное руководство одной страницей: [docs/sdk/README.ru.md](../../docs/sdk/README.ru.md). Страницы в `docs/sdk/` служат справочником по отдельным темам.

# luna-core

Рантайм агента, на котором построена [Luna](https://github.com/Dushe4ka/luna-simple), пригодный как SDK:
асинхронный цикл агента, встроенные инструменты для кода, сессии в виде журнала событий, адаптеры
провайдеров (OpenAI-совместимые, OpenAI Responses, Anthropic, Google, GigaChat), подтверждения,
хуки, песочницы, субагенты, фоновые агенты, MCP и ACP. Без LangChain и вендорских SDK.

## Установка

```bash
pip install luna-core                 # httpx, httpx-sse, pyyaml
pip install "luna-core[mcp]"          # MCP-серверы как инструменты
pip install "luna-core[acp]"          # отдавать по ACP (Zed, JetBrains)
pip install "luna-core[lsp,websearch]"
```

Python 3.11+.

## Минимальный пример

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

Инструменты, которые меняют файлы или запускают команды, запрашивают подтверждение; ответьте на
событие `NeedsApproval` через `session.decide(...)` (см. страницу о подтверждениях), иначе ход
будет ждать.

## Документация

- Руководства по SDK: [`docs/sdk/`](../../docs/sdk/quickstart.md) (быстрый старт, понятия,
  провайдеры, инструменты, сессии, подтверждения и хуки, субагенты и фоновая работа, MCP и ACP)
- Wire-протокол и схема событий: [`docs/protocol.md`](../../docs/protocol.md)
- Запускаемые примеры (офлайн с `--fake`): [`examples/`](../../examples)

Публичный API это набор имён из `luna_core.__all__`; он стабилен по semver начиная с 0.1.0.
