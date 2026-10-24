# luna-core: руководство по SDK

Это практическое руководство для Python-разработчиков, которые хотят собрать
собственного агента на `luna-core`. Предполагается, что вы знаете `asyncio`, но
не знаете этот репозиторий. Все примеры самодостаточны; там, где нужна модель,
используется `FakeProvider`, поэтому они запускаются офлайн. Блоки с пометкой
`# [нужна сеть]` и т. п. показывают код, который нельзя выполнить без ключей,
Docker или внешних серверов.

Страницы по отдельным темам (`quickstart.md`, `concepts.md`, `providers.md`, `tools.md`,
`approvals-and-hooks.md`, `sessions.md`, `subagents-and-background.md`,
`mcp-and-acp.md`) остаются справочником; при расхождении с кодом верен код.

## Содержание

1. [Что такое luna-core](#1-что-такое-luna-core)
2. [Быстрый старт](#2-быстрый-старт)
3. [Основные понятия](#3-основные-понятия)
4. [Провайдеры](#4-провайдеры)
5. [Инструменты](#5-инструменты)
6. [Подтверждения и правила](#6-подтверждения-и-правила)
7. [Хуки](#7-хуки)
8. [Сессии](#8-сессии)
9. [Сжатие контекста](#9-сжатие-контекста)
10. [Субагенты и фоновая работа](#10-субагенты-и-фоновая-работа)
11. [MCP и ACP](#11-mcp-и-acp)
12. [Удалённый клиент](#12-удалённый-клиент)
13. [Ошибки](#13-ошибки)
14. [Тестирование без сети](#14-тестирование-без-сети)
15. [Примеры из examples/](#15-примеры-из-examples)
16. [Стабильность API](#16-стабильность-api)

## 1. Что такое luna-core

`luna-core` это асинхронный рантайм агента: цикл «запрос к модели, вызовы
инструментов, подтверждения, повтор» с потоковой выдачей типизированных событий.
В комплекте встроенные инструменты для кода (`ls`, `read_file`, `write_file`,
`edit_file`, `delete`, `glob`, `grep`, `execute`, `write_todos`), сессии в виде
журнала событий в SQLite, адаптеры провайдеров на чистом `httpx` (без вендорских
SDK и без LangChain), политика разрешений, хуки, песочницы, субагенты, фоновые
запуски, MCP и ACP.

**Когда брать luna-core, а когда приложение Luna.**

| Вам нужно | Берите |
|---|---|
| Готовый CLI/TUI-агент для кода, слэш-команды, `luna setup`, конфиги | приложение Luna |
| Встроить агента в свой сервис, бота, IDE-плагин, CI, написать свои инструменты и UI | `luna-core` |
| Собственный клиент к уже запущенному `luna serve` | `luna_core.client.LunaClient` |

В SDK нет конфигурационных файлов приложения, слэш-команд, автоформатирования и
диагностики: всё это надстройки приложения. Ядро делает только то, что вы
явно собрали из `Agent`, `Policy`, `Hooks` и т. д.

**Установка.** Python 3.11+.

```bash
pip install luna-core                  # httpx, httpx-sse, pyyaml
pip install "luna-core[mcp]"           # MCP-серверы как инструменты (mcp>=1.12,<2)
pip install "luna-core[acp]"           # ACP для редакторов (agent-client-protocol>=0.4)
pip install "luna-core[lsp]"           # multilspy
pip install "luna-core[websearch]"     # ddgs
```

## 2. Быстрый старт

Один ход агента. `FakeProvider` проигрывает сценарий: сначала модель вызывает
`ls`, потом отвечает текстом.

```python
import asyncio
import tempfile

from luna_core import (
    Agent, EventLog, Policy, Session, TextDelta, ToolStarted, TurnDone, Workspace,
)
from luna_core.providers import FakeProvider


async def main() -> None:
    root = tempfile.mkdtemp()
    provider = FakeProvider(
        [FakeProvider.call("ls", {"path": "."}), FakeProvider.text("Папка пуста.")]
    )
    workspace = Workspace(root)
    agent = Agent(provider, "fake", workspace=workspace, policy=Policy(workspace=workspace))
    session = Session.create(EventLog(root + "/sessions.db"), str(workspace.root))
    async for event in agent.run(session, "Что в папке?"):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ToolStarted):
            print(f"[{event.name} {event.preview}]")
        elif isinstance(event, TurnDone):
            print(f"\n{event.usage.input + event.usage.output} токенов")


asyncio.run(main())
```

С настоящей моделью меняется только провайдер:

```python
# [нужна сеть]
from luna_core import providers

provider, model = providers.resolve("anthropic")  # читает ANTHROPIC_API_KEY
```

**Синхронный вариант.** `agent.run_sync(session, text, approve=callback)`
возвращает список событий. `approve` получает `ApprovalRequest` и возвращает
`Decision`; без колбэка любое подтверждение отклоняется.

```python
import tempfile
from pathlib import Path

from luna_core import Agent, Decision, EventLog, NeedsApproval, Policy, Session, Workspace
from luna_core.providers import FakeProvider

root = tempfile.mkdtemp()
workspace = Workspace(root)
provider = FakeProvider(
    [FakeProvider.call("write_file", {"file_path": "a.txt", "content": "hi"}),
     FakeProvider.text("Готово.")]
)
agent = Agent(provider, "fake", workspace=workspace, policy=Policy(workspace=workspace))
session = Session.create(EventLog(root + "/s.db"), root)
events = agent.run_sync(session, "создай a.txt", approve=lambda req: Decision.approve())
print([type(e).__name__ for e in events])
print(any(isinstance(e, NeedsApproval) for e in events), Path(root, "a.txt").read_text())
```

`run_sync` вызывает `asyncio.run`, поэтому его нельзя вызывать из работающего
цикла событий.

## 3. Основные понятия

| Понятие | Класс | Роль |
|---|---|---|
| Провайдер | любой объект с `name` и `async stream(request)` | говорит с API модели, отдаёт события модели |
| Агент | `Agent` | конфигурация (модель, инструменты, политика, хуки, песочница) и `run(session, text)` |
| Сессия | `Session` поверх `EventLog` | диалог, хранимый как append-only журнал событий в SQLite |
| Рабочая область | `Workspace` | корень проекта и правила разрешения путей |
| Политика | `Policy`, `Rules` | на каждый вызов: выполнить, отказать или спросить человека |
| Инструмент | `Tool`, `@tool` | функция, которую может вызвать модель, с JSON-схемой |
| Хуки | `Hooks`, `HookResult` | колбэки вокруг цикла |

**Цикл хода.** `agent.run(session, text)` это асинхронный генератор; один вызов
равен одному ходу:

1. Пишется `turn_started`, отдаётся `TurnStarted`, делается снимок для undo.
2. На первом ходе сессии срабатывают хуки `session_start`; затем в журнал
   пишется сообщение пользователя.
3. Если задан `Compactor`, при необходимости сжимается история.
4. Запрос уходит провайдеру, `TextDelta` и `ThinkingDelta` пробрасываются наружу.
5. Каждый вызов инструмента проходит политику; нужное подтверждение даёт
   `NeedsApproval`, и ход ждёт `session.decide(...)`. Разрешённые вызовы
   выполняются: чтения параллельно, записи в один путь по очереди.
6. Результаты добавляются в журнал, цикл повторяется, пока модель не ответит без
   вызовов или не исчерпан `max_steps` (по умолчанию 50).
7. Ход заканчивается ровно одним из `TurnDone`, `TurnError`, `Cancelled`.

Сбой инструмента не бросает исключение из `run`: он становится результатом с
ошибкой, который видит модель. `ProviderError` превращается в `TurnError`.

### События хода

Все классы экспортируются из `luna_core`. Это публичный протокол: их же
потребляют REPL, TUI, SSE-сервер и ACP.

| Событие | Когда приходит | Ключевые поля |
|---|---|---|
| `TurnStarted` | в начале хода | `turn_id` |
| `TextDelta` | очередной кусок ответа | `text` |
| `ThinkingDelta` | кусок рассуждений модели | `text` (подпись рассуждений `signature` в поток хода не попадает) |
| `ToolStarted` | инструмент начал выполняться (после подтверждения и хуков `pre_tool`) | `call_id`, `name`, `args` (итоговые), `preview` |
| `ToolFinished` | инструмент завершён или отклонён политикой/хуком/пользователем | `call_id`, `name`, `ok`, `detail`, `duration` |
| `NeedsApproval` | вызов требует решения человека; ход ждёт `session.decide` | `request: ApprovalRequest` (`call_id`, `name`, `args`, `description`, `suggested_rule`) |
| `TodosUpdated` | агент обновил план через `write_todos` | `todos: list[dict]` |
| `Notice` | короткая строка статуса: заметки хуков, смена модели по ролям, `max_steps`, обрезка по `max_tokens`, ошибки сохранения правила и т. п. | `level` (`dim`/`info`/`ok`/`warn`/`error`), `text` |
| `UsageUpdate` | закончился очередной запрос к модели | `usage: Usage` |
| `Compacted` | старая история сжата в резюме | `summary`, `removed` |
| `BackgroundDone` | завершилась фоновая задача или фоновый запуск; приходит через `session.subscribe(...)`, а не из потока `run` | `handle`, `ok`, `summary` |
| `ReloadRequested` | инструмент просит пересобрать агента (новый MCP-сервер, скил); ядро само его не шлёт, это для инструментов приложения через `ctx.emit` | нет полей |
| `TurnDone` | ход завершён штатно | `usage`, `cost` (USD или `None`), `stop_reason` (`end`, `max_tokens`, `max_steps`, ...) |
| `TurnError` | ход прерван ошибкой провайдера или бюджетом | `message`, `kind` (`auth`, `rate_limit`, `context_length`, ..., `budget`) |
| `Cancelled` | ход отменён (`session.cancel_turn()`) | нет полей |

`Notice` с неизвестным `level` бросает `ValueError`.

### to_wire

`to_wire(event)` превращает событие в JSON-словарь протокола v1 (см.
[docs/protocol.md](../protocol.md)). Имена отличаются: `NeedsApproval` это
`approval_needed`, `TodosUpdated` это `todos`, `UsageUpdate` это `usage_delta`,
`TurnError` это `error`.

```python
import json

from luna_core import NeedsApproval, ApprovalRequest, TextDelta, to_wire

print(to_wire(TextDelta("привет")))
req = ApprovalRequest("c1", "execute", {"command": "ls"}, "ls", "execute:ls *")
print(json.dumps(to_wire(NeedsApproval(req)), ensure_ascii=False))
```

`to_wire` бросает `TypeError` для объекта, не являющегося событием хода.

### Сообщения, изображения, расход

`Message` состоит из блоков `Text`, `Thinking`, `Image`, `ToolCall`, `ToolResult`.
Картинку в ход передают так: `agent.run(session, text, images=[image_from_file("a.png")])`.
`Usage(input, output, cache_read, cache_write, reasoning)` складывается через `+`.
`Agent(max_cost=...)` останавливает цикл ошибкой `TurnError(kind="budget")`, когда
накопленная стоимость достигла лимита. Цены берутся из встроенного каталога
моделей; для неизвестной модели стоимость равна `None`. Свои цены и окна:
`Agent(model_overrides={"my-model": {"window": 32000, "input": 1.0, "output": 2.0}})`
(цены в USD за миллион токенов).

## 4. Провайдеры

Провайдер это любой объект с атрибутом `name` и асинхронным генератором
`stream(request)`. В комплекте пять адаптеров (`openai_chat`, `openai_responses`,
`anthropic`, `google`, `gigachat`); создаются лениво через `providers.resolve`.

### providers.resolve

```python
from luna_core import LunaCoreError, providers

provider, model = providers.resolve("deepseek", env={"DEEPSEEK_API_KEY": "sk-test"})
print(type(provider).__name__, model)

try:
    providers.resolve("anthropic", env={})
except LunaCoreError as exc:
    print(exc)  # No API key for anthropic: set ANTHROPIC_API_KEY or pass api_key.
```

Сигнатура: `resolve(provider, *, model=None, api_key=None, base_url=None,
options=None, custom=None, env=os.environ)`, возвращает `(provider, model_id)`.
Ключ ищется в `api_key`, затем в переменной окружения пресета. Отсутствие ключа
это `LunaCoreError` с именем переменной. Неизвестный ключ пресета тоже
`LunaCoreError` со списком доступных.

### Встроенные пресеты

Список из `providers.PRESETS`:

| Пресет | API | Модель по умолчанию | Переменная ключа | base_url |
|---|---|---|---|---|
| `anthropic` | anthropic | `claude-sonnet-5` | `ANTHROPIC_API_KEY` | по умолчанию API |
| `openai` | openai_responses | `gpt-6-astra` | `OPENAI_API_KEY` | по умолчанию API |
| `openai_chat` | openai_chat | `gpt-6.1-sol` | `OPENAI_API_KEY` | по умолчанию API |
| `deepseek` | openai_chat | `deepseek-flash` | `DEEPSEEK_API_KEY` | `https://api.deepseek.com` |
| `google` | google | `gemini-3.1-pro-preview` | `GOOGLE_API_KEY` | по умолчанию API |
| `gigachat` | gigachat | `GigaChat-2-Max` | `GIGACHAT_CREDENTIALS` | по умолчанию API |
| `yandex` | openai_chat | `yandexgpt/latest` | `YANDEX_API_KEY` | `https://llm.api.cloud.yandex.net/v1` |
| `ollama` | openai_chat | `qwen3-coder` | не нужен | `http://localhost:11434/v1` |
| `mistral` | openai_chat | `mistral-large-latest` | `MISTRAL_API_KEY` | `https://api.mistral.ai/v1` |
| `xai` | openai_chat | `grok-4.7` | `XAI_API_KEY` | `https://api.x.ai/v1` |
| `groq` | openai_chat | `openai/gpt-oss-120b` | `GROQ_API_KEY` | `https://api.groq.com/openai/v1` |
| `fireworks` | openai_chat | `accounts/fireworks/models/qwen3p5-397b-a17b` | `FIREWORKS_API_KEY` | `https://api.fireworks.ai/inference/v1` |
| `together` | openai_chat | `Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8` | `TOGETHER_API_KEY` | `https://api.together.xyz/v1` |
| `openrouter` | openai_chat | `anthropic/claude-sonnet-5` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
| `perplexity` | openai_chat | `sonar` | `PPLX_API_KEY` | `https://api.perplexity.ai` |
| `cerebras` | openai_chat | `gpt-oss-120b` | `CEREBRAS_API_KEY` | `https://api.cerebras.ai/v1` |

Особенности: для `yandex` обязательна `YANDEX_FOLDER_ID` (id модели
превращается в `gpt://<folder>/<model>`); `gigachat` дополнительно читает
`GIGACHAT_SCOPE` и `GIGACHAT_CA_BUNDLE`. Встроенные пресеты всегда имеют
приоритет над `custom` с тем же ключом.

### Свой base_url и свой пресет

```python
from luna_core.providers import Preset, resolve

# любой OpenAI-совместимый сервер: через параметр base_url
provider, model = resolve("openai_chat", base_url="http://localhost:1234/v1", api_key="x")

# или как именованный пресет (env_var=None: ключ не нужен)
mine = Preset("mylocal", "openai_chat", "local-model", None, "http://localhost:8000/v1")
provider, model = resolve("mylocal", custom={"mylocal": mine})
print(provider.name, model)
```

### Свой провайдер

Нужно реализовать протокол: атрибут `name` и `async stream(request) -> AsyncIterator[ModelEvent]`.
Допустимые события модели (из `luna_core.events`): `TextDelta`, `ThinkingDelta`,
`ToolCallStart(index, id, name)`, `ToolCallDelta(index, args_json)`,
`UsageReport(usage)`, `StopEvent(reason)` с причиной `end`, `tool_use`,
`max_tokens` или `refusal`. `ModelRequest` содержит `model`, `system`, `messages`,
`tools`, `max_tokens`, `temperature`, `reasoning`, `cache`.

```python
import asyncio
import tempfile

from luna_core import Agent, EventLog, Session, TextDelta, Usage, Workspace
from luna_core.events import StopEvent, UsageReport
from luna_core.providers import ModelRequest


class EchoProvider:
    name = "echo"

    async def stream(self, request: ModelRequest):
        yield TextDelta("Вы сказали: " + request.messages[-1].text)
        yield UsageReport(Usage(input=3, output=4))
        yield StopEvent("end")


async def main() -> None:
    root = tempfile.mkdtemp()
    agent = Agent(EchoProvider(), "echo-1", tools=[], workspace=Workspace(root))
    session = Session.create(EventLog(root + "/s.db"), root)
    async for event in agent.run(session, "привет"):
        if isinstance(event, TextDelta):
            print(event.text)


asyncio.run(main())
```

Чтобы вызывать инструменты, отдайте `ToolCallStart(0, "id1", "ls")`, затем
`ToolCallDelta(0, '{"path": "."}')` и `StopEvent("tool_use")`. Для сетевой части
используйте `luna_core.providers.base.with_retries` и `post_sse` (повторы с
задержкой и разбор ошибок HTTP). Ошибки провайдера выбрасывайте как
`ProviderError(message, kind=...)`: тогда ход закончится `TurnError`.

Повторы выполняются только до первого события потока; ошибка посреди потока
завершает ход.

### FakeProvider

`FakeProvider(script)` для тестов: каждый вызов `stream` берёт следующий элемент
сценария. Элемент это `Message` (`FakeProvider.text("...")`,
`FakeProvider.call(name, args, call_id=None)`) либо список событий модели (в нём
можно положить исключение, оно будет брошено). Все запросы сохраняются в
`provider.requests`. Подробности в разделе 14.

## 5. Инструменты

`Tool` это имя, описание, JSON-схема и функция. Из типизированной функции его
делает декоратор `@tool(name=None, *, mutating=False, timeout=None)`.

```python
import asyncio
import tempfile
from pathlib import Path
from typing import Literal

from luna_core import (
    Agent, EventLog, Notice, Policy, Session, ToolContext, ToolError, ToolFinished,
    ToolOutput, Workspace, builtin_tools, tool,
)
from luna_core.providers import FakeProvider


@tool(timeout=10)
def word_count(ctx: ToolContext, file_path: str) -> str:
    """Посчитать слова в текстовом файле.

    Args:
        file_path: Путь к файлу относительно корня проекта.
    """
    path = ctx.workspace.resolve(file_path)  # путь вне проекта даст ошибку инструмента
    return f"{file_path}: {len(path.read_text('utf-8').split())} слов"


@tool(mutating=True)
async def deploy(ctx: ToolContext, env: Literal["staging", "prod"], tags: list[str] | None = None) -> ToolOutput:
    """Выкатить проект.

    Args:
        env: Целевое окружение.
        tags: Необязательные теги.
    """
    if env == "prod":
        raise ToolError("prod заблокирован")
    ctx.emit(Notice("info", f"выкатываю в {env}"))
    return ToolOutput.text(f"выкатили в {env}", detail="ok")


async def main() -> None:
    root = tempfile.mkdtemp()
    Path(root, "notes.txt").write_text("раз два три")
    workspace = Workspace(root)
    provider = FakeProvider([
        FakeProvider.call("word_count", {"file_path": "notes.txt"}),
        FakeProvider.call("deploy", {"env": "prod"}, "c2"),
        FakeProvider.text("готово"),
    ])
    agent = Agent(provider, "fake", tools=[*builtin_tools(), word_count, deploy],
                  workspace=workspace, policy=Policy(workspace=workspace))
    session = Session.create(EventLog(root + "/s.db"), root)
    async for event in agent.run(session, "go"):
        if isinstance(event, ToolFinished):
            print(event.name, event.ok, event.detail)


asyncio.run(main())
```

Правила `@tool`:

- Первый параметр с именем `ctx` получает `ToolContext` и скрыт из схемы.
- Первая строка (резюме) докстринга становится описанием; секция `Args:`
  описывает параметры.
- Поддерживаются аннотации `str`, `int`, `float`, `bool`, `dict`, `list[...]`,
  `Literal[...]`, `X | None`. Остальные дают `TypeError` при определении.
- Функция может быть обычной или `async`. Возвращать можно `str`, `ToolOutput`
  (`ToolOutput.text(text, detail)`, `ToolOutput.error(text)`) либо бросить
  `ToolError`. Любое исключение превращается в результат с ошибкой для модели,
  из `run` оно не вылетает.
- Аргументы проверяются по схеме до вызова: неизвестные, отсутствующие и
  неверного типа дают ошибку «invalid arguments».
- `timeout` в секундах; по истечении вызов заканчивается результатом с ошибкой.
- Для динамических инструментов (как MCP) собирайте `Tool(name, description,
  parameters, fn, raw=True)`, где `fn(ctx, args) -> ToolOutput`.

`ToolContext`: `workspace`, `sandbox`, `session_id`, `call_id`,
`emit(event)` (вставить `Notice` или другое событие в поток хода), `state`
(словарь сессии), `cancel` (`asyncio.Event`), `services` (объекты, которые вы
передали в `Agent(services=...)`, плюс `"agent"`, `"session"`, `"turn_id"`).

### Виды инструментов: чтение и изменение

Параметр `mutating` определяет, как с инструментом обращаются остальные части ядра:

| | `mutating=False` (чтение) | `mutating=True` (изменение) |
|---|---|---|
| Режим плана (`plan_mode`) | выполняется | отклоняется |
| Блокировки | параллельно с другими | вызовы с одним `file_path`/`path` идут по очереди |
| Undo | не учитывается | считается «командой, которая могла изменить любой файл» (см. раздел 8) |
| Подтверждение | не нужно | **само по себе не запрашивается** |

Последняя строка важна: подтверждение спрашивают только для имён из
`Policy(approval_tools=...)` (по умолчанию `write_file`, `edit_file`, `delete`,
`execute`) и для вызовов, попавших под правило `ask`. Чтобы свой изменяющий
инструмент требовал подтверждения, добавьте его имя в `approval_tools`
(см. раздел 6).

### Встроенные инструменты

`builtin_tools()` возвращает `ls`, `read_file`, `write_file`, `edit_file`,
`delete`, `glob`, `grep`, `execute`, `write_todos`. Инструменты `task`,
`task_status`, `task_cancel` подключаются отдельно (раздел 10).
`write_file` по существующему файлу, который агент ещё не читал, отклоняется
(сначала `read_file`, затем `edit_file` или `write_file`). Слишком большой вывод инструмента сохраняется в
`.luna/tool-output/<call_id>.txt`, а модели отдаётся выдержка
(`Agent(tool_output_budget=30000)`).

### Workspace

`Workspace(root)` разрешает пути агента: относительный путь считается от корня
проекта, ведущий `/` означает корень проекта, всё, что выходит за корень (через
`..` или симлинк), даёт `OutsideWorkspace` (подкласс `ToolError`). `workspace.rel(path)`
возвращает путь относительно проекта в POSIX-форме.

### Песочница

Инструмент `execute` запускает команды через `Agent(sandbox=...)`. По умолчанию
`LocalSandbox()` выполняет их на хосте.

```python
import asyncio
import tempfile
from pathlib import Path

from luna_core import LocalSandbox


async def main() -> None:
    result = await LocalSandbox().run("echo hi && exit 3", cwd=Path(tempfile.mkdtemp()), timeout=10)
    print(result.exit_code, repr(result.output), result.timed_out)


asyncio.run(main())
```

```python
# [нужен Docker]
from luna_core import Agent, DockerSandbox

sandbox = DockerSandbox(".", image="python:3.12-slim", network="none")  # или "bridge"
agent = Agent(provider, model, sandbox=sandbox)
# ... ходы агента ...
await agent.sandbox.close()  # удаляет контейнер
```

`DockerSandbox` поднимает один контейнер на сессию лениво (при первой команде),
проект монтируется в `/workspace`. Свою песочницу реализуют методами
`async run(command, *, cwd, timeout, env, cancel) -> RunResult` и `async close()`.

Секреты окружения. Команды `execute` не получают переменные-секреты: по умолчанию
отбрасываются имена по шаблонам `luna_core.sandbox.SECRET_ENV_PATTERNS` —
`*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_SECRET_*`, `*_PASSWORD`, `*_CREDENTIALS`,
`GIGACHAT_*` и `LUNA_SERVER_TOKEN` (иначе модель
могла бы выполнить `env`, и ключ попал бы в вывод инструмента, журнал событий и к
провайдеру). Это касается и ключей, заданных в вашей оболочке: провайдер читает их в
своём процессе раньше. Нужную переменную можно пропустить явно:
`LocalSandbox(env_passthrough=("GITHUB_TOKEN",))`; `DockerSandbox(..., env_passthrough=...)`
пробрасывает такие переменные хоста в контейнер по имени (`docker exec -e NAME`, значение
не попадает в argv). В приложении Luna то же задаётся в конфиге:

```toml
[execute]
pass_env = ["GITHUB_TOKEN"]
```

Те же правила действуют для командных хуков (`CommandHook(..., env_passthrough=...)`,
`load_command_hooks(..., env_passthrough=...)`), а в приложении — и для команд
`verify_command`, форматтера и диагностики.

## 6. Подтверждения и правила

`Policy` решает для каждого вызова: выполнить, отказать или спросить.

```python
from luna_core import Policy, Rules, Workspace

workspace = Workspace(".")
policy = Policy(
    Rules.load([]),                # список путей к TOML с таблицей [permissions]
    workspace=workspace,
    yolo=False,                    # True: не спрашивать (deny-правила всё равно действуют)
    plan_mode=False,               # bool или callable без аргументов, возвращающий bool
    approval_tools=frozenset({"write_file", "edit_file", "delete", "execute", "deploy"}),
    persist_rule=None,             # колбэк для сохранения «всегда разрешать»
)
```

Порядок проверки в `Policy.check`:

1. Путь, который не может существовать (NUL-байт и т. п.): отказ.
2. Правило `deny`: отказ.
3. Режим плана: любой изменяющий вызов (`mutating` или из `approval_tools`) отклоняется.
4. Защита от устаревшей правки: `write_file`/`edit_file`/`delete` по файлу, который
   изменился на диске с момента, когда агент его читал или писал, отклоняются с
   просьбой перечитать файл.
5. Правило `allow`, либо `yolo` (если нет `ask`): выполнить.
6. Правило `ask` или имя из `approval_tools`: спросить (`NeedsApproval`).
7. Иначе выполнить.

### Файлы правил

Формат TOML, таблица `[permissions]` со списками `allow`, `deny`, `ask`. Правило
имеет вид `<инструмент или группа>:<шаблон fnmatch>`. Группы: `write`
(`write_file`, `edit_file`, `delete`) и `fs` (все файловые), `*` для любого
инструмента. Для `execute` шаблон сравнивается с командой, для файловых
инструментов с путём относительно проекта (`src/../.env` нормализуется в `.env`,
так что deny обойти нельзя). Порядок приоритета: `deny`, затем `ask`, затем `allow`.

```python
import tempfile
from pathlib import Path

from luna_core import Rules
from luna_core.errors import LunaCoreError
from luna_core.policy import RulesFileError, append_rule

d = Path(tempfile.mkdtemp())
(d / "permissions.toml").write_text(
    '[permissions]\n'
    'allow = ["execute:pytest*"]\n'
    'deny = ["write:.env", "execute:rm *"]\n'
    'ask = ["read_file:secrets/*"]\n'
)
rules = Rules.load([d / "permissions.toml", d / "нет-такого.toml"])  # отсутствующий файл пропускается
print(rules.match("execute", {"command": "pytest -q"}))             # allow
print(rules.match("write_file", {"file_path": "src/../.env"}))      # deny

(d / "broken.toml").write_text("[permissions\n")
try:
    Rules.load([d / "broken.toml"])
except RulesFileError as exc:                                       # подкласс LunaCoreError
    print("fail-closed:", isinstance(exc, LunaCoreError))

append_rule(d / "permissions.toml", "execute:ls *")                 # дописать правило allow
```

**Fail-closed.** Существующий, но нечитаемый или некорректный файл правил
бросает `RulesFileError` вместо пропуска, потому что пропуск молча потерял бы
его deny-правила. Исправьте или удалите файл. Тот же принцип у `append_rule`:
битый файл не перезаписывается, а даёт `ValueError`.

### Ответы на подтверждения

Когда нужно решение, ход отдаёт `NeedsApproval(request)` и ждёт.

```python
import asyncio
import tempfile

from luna_core import (
    Agent, Decision, EventLog, NeedsApproval, Policy, Session, ToolFinished, ToolStarted, Workspace,
)
from luna_core.providers import FakeProvider


async def main() -> None:
    root = tempfile.mkdtemp()
    workspace = Workspace(root)
    saved: list[str] = []
    provider = FakeProvider([
        FakeProvider.call("execute", {"command": "echo one"}, "c1"),
        FakeProvider.call("execute", {"command": "rm -rf build"}, "c2"),
        FakeProvider.call("execute", {"command": "echo three"}, "c3"),
        FakeProvider.text("всё"),
    ])
    policy = Policy(workspace=workspace, persist_rule=saved.append)
    agent = Agent(provider, "fake", workspace=workspace, policy=policy)
    session = Session.create(EventLog(root + "/s.db"), root)

    async for event in agent.run(session, "go"):
        if isinstance(event, NeedsApproval):
            r = event.request  # call_id, name, args, description, suggested_rule
            print("спрашивают:", r.name, r.args, r.suggested_rule)
            if "rm" in r.args["command"]:
                session.decide(r.call_id, Decision.reject("не в этом репозитории"))
            else:
                session.decide(r.call_id, Decision.approve(always=r.suggested_rule))
        elif isinstance(event, (ToolStarted, ToolFinished)):
            print(type(event).__name__, event.call_id, getattr(event, "detail", ""))
    print("сохранено:", saved)


asyncio.run(main())
```

Варианты решения:

- `Decision.approve()` запустить как есть; `Decision.approve(always="execute:pytest*")`
  ещё и запомнить правило (в `policy.rules.allow`, плюс вызывается `persist_rule`,
  если он задан; сохранять можно через `luna_core.policy.append_rule`).
- `Decision.edit({"command": "pytest -q"})` запустить с изменёнными аргументами.
- `Decision.reject("причина")` не запускать; модель увидит «rejected by the user: причина».

Вместо `Decision` можно передать словарь: `{"type": "approve"}`,
`{"type": "edit", "args": {...}}`, `{"type": "reject", "message": "..."}`, при
желании с ключом `"always"`. Если правило «всегда» сохранить не удалось, выдаётся
`Notice("warn", ...)`, а одобренный вызов всё равно выполняется (правило тогда
действует только до конца процесса).

Ловушки: неотвеченное подтверждение держит ход (и сессию занятой) бесконечно;
`session.cancel_turn()` или закрытие генератора отменяет всю пачку вызовов.
`session.decide` для неизвестного `call_id` бросает `LunaCoreError`.
В `run_sync` без `approve` подтверждения отклоняются. Подтверждения субагентов
идут через родительскую сессию (раздел 8).

### Режим плана и yolo

- `Policy(plan_mode=True)` (или `plan_mode=lambda: flag["on"]`) отклоняет все
  изменяющие вызовы сообщением «plan mode is on». Чтения работают.
- `Policy(yolo=True)` убирает вопросы, но `deny` продолжает действовать; `ask`
  в yolo всё равно спрашивает.

## 7. Хуки

Пять точек: `session_start`, `pre_tool`, `post_tool`, `turn_end`, `pre_compact`.
Регистрация: `hooks.add(point, fn, *, match="*", timeout=60.0)`. `fn` это
`async def fn(payload: dict) -> HookResult | None`. `match` это шаблон fnmatch по
имени инструмента (имеет смысл для `pre_tool`/`post_tool`).

| Точка | Ключи payload | Что можно вернуть |
|---|---|---|
| `session_start` | `session_id`, `workdir` | `context` добавляется к первому сообщению пользователя |
| `pre_tool` | `session_id`, `tool`, `args` | `block` + `message` (модель увидит), `args` (заменить аргументы) |
| `post_tool` | `session_id`, `tool`, `args`, `ok`, `output` | `context` дописывается к результату; `block` помечает результат ошибкой |
| `turn_end` | `session_id`, `turn_id`, `text` | `block` + `message`: ход продолжается, `message` подставляется как новое сообщение пользователя |
| `pre_compact` | `session_id`, `messages` | `block`: пропустить сжатие |

В payload всегда есть ключ `event` с именем точки.

```python
import asyncio
import tempfile

from luna_core import (
    Agent, EventLog, HookResult, Hooks, Notice, Policy, Session, ToolFinished, Workspace,
)
from luna_core.providers import FakeProvider


async def no_force_push(payload: dict) -> HookResult | None:
    if "push --force" in payload["args"].get("command", ""):
        return HookResult(block=True, message="force-push запрещён")


async def slow(payload: dict) -> None:
    await asyncio.sleep(5)


async def add_context(payload: dict) -> HookResult:
    return HookResult(context="Ветка: main", notices=["контекст добавлен"])


async def main() -> None:
    root = tempfile.mkdtemp()
    workspace = Workspace(root)
    hooks = Hooks()
    hooks.add("pre_tool", no_force_push, match="execute", timeout=5)
    hooks.add("pre_tool", slow, match="ls", timeout=0.1)
    hooks.add("session_start", add_context)
    provider = FakeProvider([
        FakeProvider.call("execute", {"command": "git push --force"}),
        FakeProvider.call("ls", {"path": "."}, "c2"),
        FakeProvider.text("ok"),
    ])
    agent = Agent(provider, "fake", workspace=workspace, hooks=hooks,
                  policy=Policy(workspace=workspace, yolo=True))
    session = Session.create(EventLog(root + "/s.db"), root)
    async for event in agent.run(session, "go"):
        if isinstance(event, (Notice, ToolFinished)):
            print(event)


asyncio.run(main())
```

`HookResult(block=False, message="", args=None, context="", notices=())`.
Хуки идут в порядке регистрации, первый блокирующий останавливает цепочку.
Исключение или таймаут хука превращается в `Notice` и ход не блокирует.
Хуки-команды из конфигурации: `luna_core.hooks.load_command_hooks(entries, cwd)`
принимает список словарей `{"event", "match", "command", "timeout"}`, команда
получает payload JSON-ом на stdin; код выхода 0 продолжает (stdout уходит в
контекст), 2 блокирует (stderr уходит модели), прочие коды дают предупреждение.
`hooks.extend(other)` объединяет наборы.

## 8. Сессии

Сессия это диалог, хранимый как append-only журнал событий в SQLite (WAL).
`EventLog(path)` одно хранилище на процесс, `Session` дескриптор одного диалога.

```python
import asyncio
import tempfile

from luna_core import Agent, EventLog, Policy, Session, Workspace
from luna_core.providers import FakeProvider


async def main() -> None:
    root = tempfile.mkdtemp()
    db = root + "/sessions.db"
    log = EventLog(db)                      # файл и папки создаются по требованию
    workspace = Workspace(root)
    provider = FakeProvider([FakeProvider.text("привет!")])
    agent = Agent(provider, "fake", workspace=workspace, policy=Policy(workspace=workspace))

    session = Session.create(log, root, title="демо")
    async for _ in agent.run(session, "здравствуй"):
        pass

    again = Session.open(EventLog(db), session.id)      # позже или в другом процессе
    print(again.title, [m.role for m in again.messages()])
    print(again.usage(), again.cost(), again.todos())
    for info in log.list(workdir=root):                  # SessionInfo, свежие первыми
        print(info.id, info.title)


asyncio.run(main())
```

`Session.create(log, workdir, *, title="", undo=True, meta=None)`,
`Session.open(log, session_id, *, undo=True)` (неизвестный id даёт `LunaCoreError`).
`undo=False` отключает журнал для /undo.

| Вызов | Результат |
|---|---|
| `session.messages()` | контекст модели, пересобранный из журнала (с учётом сжатий) |
| `session.entries()` / `session.records()` | записи стенограммы / сырые записи журнала |
| `session.usage()` | `Usage` по всем запросам сессии |
| `session.cost()` | USD по ходам с известной ценой, включая субагентов и фоновые сессии |
| `session.todos()` | последний план, записанный `write_todos` |
| `session.title` | первая строка первого сообщения пользователя, если не задано |
| `session.busy` | `True`, пока идёт ход |
| `session.decide(call_id, decision)` | ответить на подтверждение |
| `session.pending_approvals` | `call_id`, ожидающие решения |
| `session.queue(text)` | доставить текст на границе ближайшего шага (рулевое управление) |
| `session.cancel_turn()` | попросить ход остановиться; `False`, если простаивает |
| `session.fork(at_seq=None)` | новая сессия с историей до `at_seq` |
| `session.export("md"\|"html"\|"json")` | стенограмма текстом |
| `session.undo()` / `session.redo()` | откат и возврат последнего хода |
| `session.rewind(to_seq)` | откатить все ходы, начатые после `to_seq` |
| `await agent.side_question(session, text)` | ответ по контексту без инструментов и без записи в журнал |

### pending_approvals и подтверждения субагентов

Подтверждения субагентов идут через родительскую сессию. Их `call_id` в
`NeedsApproval` и в `session.pending_approvals` получают префикс
`"<id дочерней сессии>:<id вызова>"` (рекурсивно), так что одинаковые id у
соседних субагентов не пересекаются. Отвечайте на родительской сессии:
`session.decide(event.request.call_id, ...)` сам маршрутизирует по префиксу.

### Рулевое управление, отмена, занятость

```python
import asyncio
import tempfile

from luna_core import Agent, EventLog, LunaCoreError, Policy, Session, TextDelta, Workspace
from luna_core.events import StopEvent
from luna_core.providers import FakeProvider


class Slow:
    name = "slow"

    async def stream(self, request):
        yield TextDelta("начало ")
        await asyncio.sleep(30)
        yield StopEvent("end")


async def main() -> None:
    root = tempfile.mkdtemp()
    log = EventLog(root + "/s.db")
    workspace = Workspace(root)

    # отмена: ход заканчивается событием Cancelled
    agent = Agent(Slow(), "slow", workspace=workspace)
    session = Session.create(log, root)

    async def consume() -> list[str]:
        return [type(e).__name__ async for e in agent.run(session, "долго")]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    print(session.busy, session.cancel_turn())
    print(await task, session.busy, session.cancel_turn())

    # второй ход в занятой сессии запрещён
    g = agent.run(session, "a")
    await g.__anext__()
    try:
        async for _ in agent.run(session, "b"):
            pass
    except LunaCoreError as exc:
        print(exc)
    await g.aclose()      # закрывайте недочитанные генераторы: ход отменяется

    # queue: текст попадает в контекст на ближайшем шаге
    provider = FakeProvider([FakeProvider.text("учёл")])
    agent = Agent(provider, "fake", workspace=workspace, policy=Policy(workspace=workspace))
    session = Session.create(log, root)
    session.queue("и ещё: коротко")
    async for _ in agent.run(session, "привет"):
        pass
    print([m.text for m in provider.requests[0].messages])


asyncio.run(main())
```

`cancel_turn()` останавливает запрос и работающие инструменты (группы процессов
убиваются), пишет `Cancelled` и «залечивает» висящие вызовы, чтобы следующий ход
работал. Один ход на сессию: второй `agent.run` на занятой сессии бросает
`LunaCoreError`. `EventLog.list()` возвращает и дочерние сессии (субагенты,
фоновые, форки); для списка выбора фильтруйте по `parent_id`.

### Fork, export, rewind

`fork` копирует записи до `at_seq` в новую сессию с `meta={"kind": "fork"}` и
`parent_id`; `log.children(session.id)` перечисляет дочерние. `export("json")`
отдаёт все записи, `"md"` и `"html"` читаемую стенограмму.

### Undo и redo

Журнал отката лежит в `<workdir>/.luna/undo/<session_id>/`. На каждом ходу
делается снимок состояния до и после. Семантика:

- **Откатываются только пути, изменённые файловыми инструментами** (`write_file`,
  `edit_file`, `delete`) в этом ходу. Снимок всего дерева содержит и ваши
  правки, поэтому трогать «всё» нельзя.
- **Пути, которые вы изменили после хода**, пропускаются и называются в
  сообщении (`skipped b.txt: changed since the turn`). То же при redo: путь,
  отредактированный после undo, пропускается (`changed since the undo`).
- В git-репозитории снимки делаются через объекты дерева git (индекс и
  история проекта не затрагиваются), вне git используется файловый журнал
  (предыдущее содержимое байт в байт).
- Если в ходу выполнялся `execute` или другой изменяющий инструмент (`mutating=True`
  не файловый), он мог поменять любой файл, поэтому в git-режиме откатывается
  полный diff хода, а каждый лишний путь называется в сообщении
  («also restored (changed by commands or outside Luna): …»). Известное
  ограничение: в таком ходу файл, который вы правили в редакторе **во время**
  хода, тоже будет восстановлен (но назван в сообщении). Пути, изменённые
  после хода, по-прежнему пропускаются.
- Если конец хода не записан (процесс упал посреди хода), откатываются только
  пути файловых инструментов.
- Возвращаемое значение `UndoResult(turn_id, note)`; `None`, если откатывать
  нечего. `session.undo_depth` число откатываемых ходов.
- **Во время фоновых задач undo/redo/rewind нужно запрещать самому.** В SDK
  `Session.undo()` этого не проверяет; слэш-команды приложения Luna отказывают,
  пока работают фоновые задачи сессии. В своём коде проверяйте
  `background.list(session.id)` на статусы `queued`/`running` перед откатом.

```python
import asyncio
import tempfile
from pathlib import Path

from luna_core import Agent, EventLog, Policy, Session, Workspace
from luna_core.providers import FakeProvider


async def main() -> None:
    root = tempfile.mkdtemp()
    Path(root, "a.txt").write_text("v0\n")
    Path(root, "b.txt").write_text("b0\n")
    workspace = Workspace(root)
    provider = FakeProvider([
        FakeProvider.call("edit_file", {"file_path": "a.txt", "old_string": "v0", "new_string": "v1"}, "w1"),
        FakeProvider.call("edit_file", {"file_path": "b.txt", "old_string": "b0", "new_string": "b1"}, "w2"),
        FakeProvider.text("готово"),
    ])
    agent = Agent(provider, "fake", workspace=workspace, policy=Policy(workspace=workspace, yolo=True))
    session = Session.create(EventLog(root + "/s.db"), root)
    async for _ in agent.run(session, "правь"):
        pass

    Path(root, "b.txt").write_text("правка пользователя\n")   # вы поправили файл после хода
    print(session.undo().note)    # restored 1 files; skipped b.txt: changed since the turn; ...
    print(Path(root, "a.txt").read_text().strip(), "|", Path(root, "b.txt").read_text().strip())
    print(session.redo().note)


asyncio.run(main())
```

## 9. Сжатие контекста

`Compactor(threshold=0.8, keep_turns=4, provider=None, model=None,
max_summary_tokens=4000)` подключается как `Agent(compactor=...)`. Когда оценка
следующего запроса превышает `threshold` окна модели, старые сообщения
пересказываются моделью в резюме, последние `keep_turns` ходов остаются дословно,
а полная история остаётся в журнале. Отдаётся событие `Compacted(summary, removed)`.
`provider`/`model` задают отдельную (дешёвую) модель для резюме; без них
используется модель агента. Хук `pre_compact` может пропустить сжатие. После
неудачной автоматической попытки повтор не раньше чем через несколько новых записей.
Вручную: `async for event in compactor.compact(agent, session, instructions="...")`.

Окно берётся из каталога моделей; для своей модели задайте его через
`model_overrides`.

```python
import asyncio
import tempfile

from luna_core import Agent, Compacted, Compactor, EventLog, ModelRoles, Session, Workspace
from luna_core.providers import FakeProvider


async def main() -> None:
    root = tempfile.mkdtemp()
    workspace = Workspace(root)
    summarizer = FakeProvider([FakeProvider.text("Краткое резюме.") for _ in range(5)])
    main_model = FakeProvider([FakeProvider.text(f"ответ {i}") for i in range(6)])
    agent = Agent(
        main_model, "demo-model", workspace=workspace,
        model_overrides={"demo-model": {"window": 400, "input": 1.0, "output": 2.0}},
        compactor=Compactor(threshold=0.5, keep_turns=2, provider=summarizer, model="demo-summarizer"),
    )
    session = Session.create(EventLog(root + "/s.db"), root)
    for i in range(5):
        async for event in agent.run(session, f"вопрос {i} " + "слово " * 40):
            if isinstance(event, Compacted):
                print("сжато сообщений:", event.removed, "|", event.summary)
    print(session.messages()[0].text[:50].replace("\n", " "))

    roles = ModelRoles(fast=(FakeProvider([]), "small"), plan="big-model")
    print(roles.get("fast", main_model, "demo-model")[1], roles.get("vision", main_model, "demo-model")[1])


asyncio.run(main())
```

### ModelRoles

`ModelRoles(fast=None, plan=None, vision=None, summarize=None)` сопоставляет роли
моделям: строка (id модели у того же провайдера) или пара `(provider, model)`.
Неуказанная роль берёт модель агента. Передаётся как `Agent(roles=...)`. Роль
`plan` включается в режиме плана, `vision` когда в ходе есть картинки, а
основная модель их не видит (иначе выводится `Notice` с предупреждением),
`fast` модель по умолчанию для субагентов. Смена модели на ходу сопровождается
`Notice("dim", ...)`.

## 10. Субагенты и фоновая работа

Инструмент `task(description, subagent_type, background, isolation)` запускает
дочернего `Agent` в новой ветке сессии со своим набором инструментов.

```python
import asyncio
import tempfile
from pathlib import Path

from luna_core import (
    Agent, BackgroundTasks, EventLog, Notice, Policy, Session, SubagentRunner, SubagentSpec,
    TextDelta, ToolFinished, Workspace, builtin_tools, task_tool,
)
from luna_core.providers import FakeProvider
from luna_core.subagents import BUILTIN_SUBAGENTS
from luna_core.tools.tasks import task_cancel_tool, task_status_tool


async def main() -> None:
    root = tempfile.mkdtemp()
    Path(root, "x.txt").write_text("hi")
    workspace = Workspace(root)

    specs = dict(BUILTIN_SUBAGENTS)         # researcher, reviewer (только чтение), general-purpose
    specs["tester"] = SubagentSpec(
        "tester", "Запускает тесты и сообщает о падениях.", "Ты запускаешь тесты.",
        tools=("ls", "read_file", "execute"), unsafe=True,
    )
    runner = SubagentRunner(specs, max_parallel=4)
    tools = [*builtin_tools(), task_tool(runner), task_status_tool(), task_cancel_tool()]

    provider = FakeProvider([
        FakeProvider.call("task", {"description": "Прочитай x.txt", "subagent_type": "researcher"}),
        FakeProvider.call("read_file", {"file_path": "x.txt"}, "r1"),   # ход субагента
        FakeProvider.text("в файле hi"),                                 # ответ субагента
        FakeProvider.text("Субагент сообщил: hi"),                       # ответ родителя
    ])
    agent = Agent(provider, "fake", tools=tools, workspace=workspace,
                  policy=Policy(workspace=workspace), services={"background": BackgroundTasks()})
    session = Session.create(EventLog(root + "/s.db"), root)
    async for event in agent.run(session, "делегируй"):
        if isinstance(event, (Notice, ToolFinished, TextDelta)):
            print(type(event).__name__, getattr(event, "text", getattr(event, "detail", "")))


asyncio.run(main())
```

- `SubagentSpec(name, description, prompt, tools=("ls","read_file","glob","grep"),
  model=None, unsafe=False, isolation="auto", max_steps=30, max_cost=None)`.
  По умолчанию субагент только читает. Изменяющие инструменты (`write_file`,
  `edit_file`, `delete`, `execute`) в `subagents.toml` требуют `unsafe = true`,
  иначе `SubagentConfigError`; `luna_core.subagents.load_subagents(files)`
  сливает таблицы `[subagent.<name>]` с встроенными. При создании `SubagentSpec`
  в коде такой проверки нет, поэтому `unsafe` ставьте осознанно.
- Несколько вызовов `task` в одном сообщении идут параллельно, до `max_parallel`.
- Ребёнок наследует политику, песочницу и хуки; его подтверждения приходят
  через родителя с префиксом `"<child>:<id>"`. Перед запуском проверяются
  `max_cost` и остаток бюджета родителя.
- Родителю нужен `services={"background": BackgroundTasks()}` для `background=true`
  и для `task_status`/`task_cancel`.

### Изоляция через git worktree

Пишущий субагент по умолчанию (`isolation="auto"`) работает в отдельном git
worktree на ветке `luna/sub/*`, так что параллельные правки не конфликтуют.
Родитель получает резюме, имя ветки и diff stat; правки применяются командой
`git diff <base> <branch> | git apply`. Нужен git, а корень проекта должен быть
верхним уровнем репозитория, иначе `auto` откатывается на общее рабочее
пространство (записи в один путь сериализуются), а явный `isolation="worktree"`
ошибка. Субагент, ничего не изменивший, ветку не оставляет. Родительский файл при
этом не меняется (проверено ниже).

```python
import asyncio
import subprocess
import tempfile
from pathlib import Path

from luna_core import (
    Agent, EventLog, Policy, Session, SubagentRunner, Workspace, builtin_tools, task_tool,
)
from luna_core.providers import FakeProvider
from luna_core.subagents import BUILTIN_SUBAGENTS


async def main() -> None:
    root = tempfile.mkdtemp()

    def git(*args: str) -> None:
        cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args]
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)

    git("init", "-q")
    Path(root, "x.txt").write_text("hi\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    provider = FakeProvider([
        FakeProvider.call("task", {"description": "поправь x.txt",
                                   "subagent_type": "general-purpose", "isolation": "worktree"}),
        FakeProvider.call("edit_file", {"file_path": "x.txt", "old_string": "hi", "new_string": "hello"}, "e1"),
        FakeProvider.text("правка сделана"),
        FakeProvider.text("готово"),
    ])
    tools = [*builtin_tools(), task_tool(SubagentRunner(dict(BUILTIN_SUBAGENTS)))]
    workspace = Workspace(root)
    agent = Agent(provider, "fake", tools=tools, workspace=workspace,
                  policy=Policy(workspace=workspace, yolo=True))
    session = Session.create(EventLog(tempfile.mkdtemp() + "/s.db"), root, undo=False)
    async for _ in agent.run(session, "go"):
        pass
    result = [m for m in session.messages() if m.role == "tool"][0].results[0].text
    print(result)                                   # ... changes are committed on branch luna/sub/...
    print(Path(root, "x.txt").read_text().strip())  # hi: основная копия не тронута


asyncio.run(main())
```

### BackgroundTasks

`task(..., background=true)` сразу возвращает handle, родитель продолжает работу.
Когда ребёнок закончит, результат пишется в журнал, вызывается
`session.notify(BackgroundDone(...))` (получатели, подписанные через
`session.subscribe(fn)`), а текст попадает в родителя **на ближайшей границе шага**,
а не мгновенно. Те же вызовы доступны напрямую:

```python
import asyncio
import tempfile

from luna_core import BackgroundTasks, EventLog, Session


async def main() -> None:
    root = tempfile.mkdtemp()
    session = Session.create(EventLog(root + "/s.db"), root)
    background = BackgroundTasks(max_parallel=4)

    async def work() -> str:
        await asyncio.sleep(0.05)
        return "сделано"

    handle = background.start("демо", work, session=session)
    info = await background.wait(handle)       # TaskInfo(status, summary, ...)
    print(info.status, info.summary)
    print(session.drain_queue())               # то, что получит родитель на следующем шаге


asyncio.run(main())
```

Методы: `start(label, factory, *, session)`, `get(handle)`, `list(session_id=None)`,
`cancel(handle)`, `wait(handle)`, `shutdown()`. Хранятся последние 200 завершённых.
`task_status` и `task_cancel` видят только задачи своей сессии.

### AgentManager

`AgentManager` запускает целые ходы отдельно от потребителя; так сервер
переживает закрытие UI.

```python
import asyncio
import tempfile

from luna_core import Agent, AgentManager, EventLog, Session, Workspace
from luna_core.providers import FakeProvider


async def main() -> None:
    root = tempfile.mkdtemp()
    agent = Agent(FakeProvider([FakeProvider.text("фон")]), "fake", workspace=Workspace(root))
    session = Session.create(EventLog(root + "/s.db"), root)
    manager = AgentManager(max_parallel=2)

    manager.start(agent, session, "сделай в фоне", label="demo")
    unsubscribe = manager.subscribe(session.id, lambda wire: print("live:", wire["event"]))
    info = await manager.wait(session.id)
    unsubscribe()
    print(info.status, info.text)
    print([e["event"] for e in manager.events(session.id)])     # повтор для опоздавших клиентов
    print([r.label for r in manager.list()])


asyncio.run(main())
```

События приходят словарями `to_wire` с добавленным монотонным `index`.
`manager.events(session_id, after=-1)` проигрывает буфер (5000 событий),
`manager.cancel(session_id)` останавливает запуск, `manager.list()` отдаёт
`RunInfo` (`status`: `queued`/`running`/`waiting`/`done`/`failed`/`cancelled`).
Второй запуск в уже работающей сессии бросает `LunaCoreError`.

## 11. MCP и ACP

### MCP-серверы как инструменты (extra `mcp`)

Модуль `luna_core.mcp` не входит в `__all__`, импортируйте его явно. Формат файла
обычный: `{"mcpServers": {"имя": {"command": ..., "args": [...]}}}` для stdio,
`{"type": "http", "url": ...}` для streamable HTTP, `"type": "sse"` для SSE;
`${ENV}` раскрывается из окружения процесса (в приложении Luna сохранённые через
`luna setup` ключи туда не экспортируются — задайте переменную в оболочке или впишите
значение в `env` сервера), токены OAuth лежат в `~/.config/luna/mcp-tokens`. Stdio-серверы
не наследуют ваше окружение, только `env` из конфига.

```python
import asyncio
import json
import tempfile
from pathlib import Path

from luna_core import Agent, builtin_tools
from luna_core.mcp import MCPManager, load_config


async def main() -> None:
    config = Path(tempfile.mkdtemp()) / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"broken": {"command": "/nonexistent/server"}}}))
    manager = MCPManager(load_config([config]), on_warn=print)
    mcp_tools = await manager.start()      # подключается параллельно, возвращает список Tool
    try:
        print(mcp_tools, manager.status())  # [] {'broken': 'failed: ...'}
        # agent = Agent(provider, model, tools=[*builtin_tools(), *mcp_tools])
    finally:
        await manager.close()


asyncio.run(main())
```

Сервер, который не удалось запустить, пропускается с предупреждением
(`on_warn`), `manager.status()` показывает `connected` или `failed: ...`.
Без установленного extra `MCPManager.start()` не бросает исключение, а выдаёт
подсказку через `on_warn` и возвращает пустой список.

### ACP для редакторов (extra `acp`)

`run_acp(make_agent, log)` обслуживает агента по stdio для Zed, JetBrains и
других ACP-клиентов. `make_agent(cwd)` вызывается на каждую сессию редактора.

```python
# [нужен редактор-клиент на другом конце stdio]
import asyncio

from luna_core import Agent, EventLog, Policy, Workspace, providers
from luna_core.acp import run_acp


def make_agent(cwd: str) -> Agent:
    provider, model = providers.resolve("anthropic")
    workspace = Workspace(cwd)
    return Agent(provider, model, workspace=workspace, policy=Policy(workspace=workspace))


asyncio.run(run_acp(make_agent, EventLog("acp.db")))
```

Соответствие: `new_session`/`load_session` это сессии, `prompt` ход, `cancel` это
`session.cancel_turn()`; текст и рассуждения идут как message chunks, вызовы
инструментов как `tool_call`/`tool_call_update`, todos как `plan`, расход как
`usage_update`, подтверждения как `session/request_permission` (разрешить один
раз, всегда, отклонить). Ловушки: stdout это канал протокола, ничего не печатайте
в него (логируйте в stderr), и он должен быть каналом, а не файлом. Проверить
без редактора можно `python examples/acp_agent.py --self-test`.

## 12. Удалённый клиент

Если сервер Luna уже запущен (`luna serve`, это часть приложения), используйте
протокол v1: HTTP + SSE с заголовком `Authorization: Bearer <token>`. Полная
спецификация эндпоинтов и событий в [docs/protocol.md](../protocol.md);
`PROTOCOL_VERSION == 1`, события валидируются `luna_core.protocol.validate_event`,
JSON Schema выдаёт `luna_core.protocol.json_schema()`.

```python
# [нужен запущенный сервер: LUNA_SERVER_TOKEN=SECRET luna serve --port 8765]
import asyncio

from luna_core.client import LunaClient


async def main() -> None:
    client = LunaClient("http://127.0.0.1:8765", "SECRET")
    try:
        print(await client.health())                  # {'version': ..., 'protocol': 1}
        session_id = await client.create_session("/path/to/project")
        async for event in client.send(session_id, "Скажи привет.", "/path/to/project"):
            if event["event"] == "text_delta":
                print(event["text"], end="", flush=True)
            elif event["event"] == "approval_needed":
                call_id = event["value"]["action_requests"][0]["call_id"]
                await client.decide(session_id, call_id, {"type": "reject", "message": "только чтение"})
    finally:
        await client.aclose()


asyncio.run(main())
```

Методы `LunaClient`: `health`, `list_sessions`, `create_session`, `delete_session`,
`history`, `state`, `send` (SSE-поток словарей), `decide`, `cancel`, `commands`,
`run_command`, `background`, `agents`, `attach(session_id, after=-1)` (подключение
к фоновому запуску с повтором пропущенного), `aclose`. Ошибки HTTP и недоступность
сервера это `luna_core.client.ServerError` (подкласс `LunaCoreError`).
Неотвеченное подтверждение держит поток, как и локально. Рабочий пример:
`python examples/remote_client.py --fake`.

## 13. Ошибки

```
LunaCoreError
├── ProviderError   # kind, status, retry_after, retryable
└── ToolError       # OutsideWorkspace, InvalidPath
```

- `LunaCoreError` базовый класс; также его бросают: `providers.resolve` (нет ключа
  или неизвестный пресет), `Session.open` (нет сессии), `Session.decide` (нет такого
  подтверждения), `agent.run` на занятой сессии, `BackgroundTasks.wait` и
  `AgentManager.wait` на неизвестный handle. Его подклассы вне `__all__`:
  `RulesFileError` (`luna_core.policy`), `SubagentConfigError` (`luna_core.subagents`),
  `MCPConfigError`, `SkillError`, `ServerError`.
- `ProviderError(message, kind, status, retry_after)`: `kind` один из `auth`,
  `rate_limit`, `context_length`, `bad_request`, `server`, `network`, `unknown`;
  `retryable` истинно для `rate_limit`, `server`, `network`. Из `agent.run` он **не
  вылетает**: превращается в `TurnError(message, kind)`. Бросайте его из своего
  провайдера.
- `ToolError` бросает код инструмента, чтобы вернуть модели ошибку; наружу тоже
  не вылетает (станет `ToolFinished(ok=False)`).

Что ловить: в обычном цикле по событиям проверяйте `TurnError` (`event.kind`) и
`Cancelled`; вокруг построения (`providers.resolve`, `Session.open`, `Rules.load`,
`agent.run` на занятой сессии) ловите `LunaCoreError`. Исключения, не являющиеся
`ProviderError` (баг в вашем провайдере, `AssertionError` из исчерпанного
`FakeProvider`), из `run` вылетают как есть. Бюджет `max_cost` даёт
`TurnError(kind="budget")`, `max_steps` не ошибка: `Notice` и
`TurnDone(stop_reason="max_steps")`.

```python
import asyncio
import tempfile

from luna_core import Agent, EventLog, ProviderError, Session, TurnError, Workspace
from luna_core.providers import FakeProvider


async def main() -> None:
    root = tempfile.mkdtemp()
    provider = FakeProvider([[ProviderError("плохой ключ", kind="auth", status=401)]])
    agent = Agent(provider, "fake", workspace=Workspace(root))
    session = Session.create(EventLog(root + "/s.db"), root)
    async for event in agent.run(session, "привет"):
        if isinstance(event, TurnError):
            print(event.kind, "|", event.message)


asyncio.run(main())
```

## 14. Тестирование без сети

`FakeProvider(script)` позволяет проверять агента детерминированно. Элемент
сценария это `FakeProvider.text("...")`, `FakeProvider.call(name, args, call_id)`
или список событий модели (можно с исключением вроде `ProviderError`, оно будет
брошено). Каждый вызов модели съедает один элемент; когда сценарий кончился,
следующий запрос даёт `AssertionError("FakeProvider script exhausted")`,
который вылетает из `run`. После хода `provider.requests` хранит все
`ModelRequest`, по ним можно проверять системный промпт, историю и список
инструментов.

```python
import asyncio
import tempfile

from luna_core import Agent, EventLog, ModelRoles, Session, TextDelta, TurnDone, Workspace
from luna_core.events import StopEvent
from luna_core.providers import FakeProvider


async def collect(agent: Agent, session: Session, text: str) -> list:
    return [event async for event in agent.run(session, text)]


async def main() -> None:
    root = tempfile.mkdtemp()
    log = EventLog(root + "/s.db")
    workspace = Workspace(root)

    # 1. потоковый ответ по кускам
    provider = FakeProvider([[TextDelta("при"), TextDelta("вет"), StopEvent("end")]])
    agent = Agent(provider, "fake", workspace=workspace)
    events = await collect(agent, Session.create(log, root), "x")
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "привет"

    # 2. что отправили модели
    provider = FakeProvider([FakeProvider.text("ok")])
    agent = Agent(provider, "fake", system="Ты бот.", workspace=workspace)
    await collect(agent, Session.create(log, root), "вопрос")
    request = provider.requests[0]
    assert request.system == "Ты бот."
    assert request.messages[-1].text == "вопрос"
    assert "read_file" in [t.name for t in request.tools]

    # 3. бюджет: цену задаём через model_overrides
    provider = FakeProvider([FakeProvider.text("a"), FakeProvider.text("b")])
    agent = Agent(provider, "m", workspace=workspace, max_cost=0.00001,
                  model_overrides={"m": {"input": 100.0, "output": 100.0}})
    session = Session.create(log, root)
    await collect(agent, session, "1")
    last = (await collect(agent, session, "2"))[-1]
    assert last.kind == "budget", last

    # 4. исчерпанный сценарий
    try:
        await collect(Agent(FakeProvider([]), "fake", workspace=workspace), Session.create(log, root), "q")
    except AssertionError as exc:
        print("ожидаемо:", exc)
    print("тесты прошли")


asyncio.run(main())
```

Подсказки: используйте временную папку и отдельный `EventLog` на тест; для
подтверждений отвечайте `session.decide` прямо в цикле по событиям или
передавайте `approve=` в `run_sync`; чтобы не ждать диска, `Policy(yolo=True)`
пропускает вопросы. Для проверки провайдеров против HTTP используйте
`httpx.MockTransport`.

## 15. Примеры из examples/

Все запускаются из корня репозитория. Офлайн-режим не требует ключей.

| Файл | Что показывает | Запуск офлайн |
|---|---|---|
| `examples/minimal_agent.py` | один ход агента, вывод потока событий | `python examples/minimal_agent.py --fake --workdir .` |
| `examples/custom_tool.py` | свой инструмент `word_count` рядом со встроенными | `python examples/custom_tool.py --fake --workdir .` |
| `examples/custom_provider.py` | свой провайдер (эхо) без сети; `--fake` принимается, но ни на что не влияет | `python examples/custom_provider.py --workdir .` |
| `examples/approvals_web.py` | подтверждения по HTTP: Starlette-приложение со стримом хода и эндпоинтом решений (нужны `starlette`, `uvicorn`) | `python examples/approvals_web.py --self-test --workdir .` |
| `examples/remote_client.py` | `LunaClient`: сессия, сообщение, подтверждение, потоковый ответ | `python examples/remote_client.py --fake --workdir .` (поднимает сервер в процессе) или `LUNA_SERVER_TOKEN=... python examples/remote_client.py --url ...` к настоящему серверу |
| `examples/acp_agent.py` | агент по ACP для редактора (extra `acp`) | `python examples/acp_agent.py --self-test --workdir .` играет роль редактора |

С настоящей моделью уберите `--fake` и задайте ключ, например
`ANTHROPIC_API_KEY=... python examples/minimal_agent.py "Что это за проект?"`
(`--provider` выбирает пресет).

## 16. Стабильность API

Публичный API это имена в `luna_core.__all__`; начиная с `0.1.0` он следует
semver: несовместимые изменения только в мажорной версии, новые имена и
необязательные параметры в минорной. Версия: `luna_core.__version__`.

Всё остальное нестабильно, даже если импортируется: внутренние модули
(`luna_core.loop`, `luna_core.undo`, `luna_core.policy` кроме `Decision`, `Policy`
и `Rules`), а также `luna_core.mcp`, `luna_core.acp`, `luna_core.client`,
`luna_core.protocol`, `luna_core.subagents.load_subagents`, `luna_core.hooks.load_command_hooks`,
`luna_core.providers.base` и `luna_core.tools.tasks`. Этими модулями, как
показано в руководстве, можно пользоваться, но их сигнатуры могут меняться
быстрее. Протокол v1 версионируется отдельно (`PROTOCOL_VERSION`): аддитивные
изменения (новое необязательное поле, новое событие) версию не повышают, поэтому
клиент должен игнорировать неизвестные поля и события.
