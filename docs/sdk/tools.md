# Инструменты

`Tool` это имя, описание, схема JSON и функция. Создайте его из типизированной функции
через `@tool`:

```python
from luna_core import Agent, ToolContext, builtin_tools, tool


@tool(timeout=10)
def word_count(ctx: ToolContext, file_path: str) -> str:
    """Count the words in a text file.

    Args:
        file_path: Path of the file, relative to the project root.
    """
    path = ctx.workspace.resolve(file_path)   # путь вне проекта это ошибка инструмента
    return f"{file_path}: {len(path.read_text('utf-8').split())} words"


agent = Agent(provider, model, tools=[*builtin_tools(), word_count])
```

- Первый параметр с именем `ctx` получает `ToolContext` и скрыт из схемы.
- Первая строка докстринга становится описанием; секция `Args:` описывает параметры.
- Поддерживаемые аннотации: `str`, `int`, `float`, `bool`, `dict`, `list[...]`,
  `Literal[...]`, `X | None` / `Optional`. Остальные отклоняются при определении инструмента.
- Верните `str`, `ToolOutput` (`ToolOutput.text(text, detail)` / `ToolOutput.error(text)`)
  или бросьте `ToolError`; любое исключение становится для модели результатом с ошибкой.
- `@tool(mutating=True)` помечает инструмент, меняющий состояние. Сам по себе он подтверждения
  **не** требует: режим плана отклоняет такой вызов, вызовы с одним и тем же `file_path`/`path`
  выполняются последовательно, а undo считает его командой, которая могла изменить любой файл.
  Чтобы требовать подтверждение, добавьте его имя в `Policy(approval_tools=...)` или в правило `ask`.
- Для динамических инструментов (MCP) создайте `Tool(name, description, parameters, fn, raw=True)`,
  где `fn(ctx, args) -> ToolOutput`.

`ToolContext` содержит `workspace`, `sandbox`, `session_id`, `call_id`, `emit(event)`
(отправить в ход `Notice` или другое событие), `state`, `cancel` (`asyncio.Event`) и
`services` (объекты, которые подставляет приложение, например `"agent"`, `"session"`, `"background"`).

## Встроенные инструменты

`builtin_tools()` возвращает `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`,
`grep`, `execute` и `write_todos`. `task`, `task_status` и `task_cancel` поставляются вместе с
[субагентами](subagents-and-background.md). Схемы встроенных инструментов укладываются в бюджет
2 000 токенов (проверяется тестом `test_builtin_schemas_fit_the_token_budget`).

| Инструмент | Параметры |
|---|---|
| `ls` | `path` |
| `read_file` | `file_path`, `offset`, `limit` |
| `write_file` | `file_path`, `content` |
| `edit_file` | `file_path`, `old_string`, `new_string`, `replace_all` |
| `delete` | `file_path` |
| `glob` | `pattern`, `path` |
| `grep` | `pattern`, `path`, `glob`, `output_mode`, `max_count` |
| `execute` | `command`, `timeout` |
| `write_todos` | `todos` |

## Workspace и пути

`Workspace(root)` разрешает пути агента: относительные пути отсчитываются от проекта,
ведущий `/` означает корень проекта, а всё, что оказывается за пределами корня (в том числе
через `..` или симлинки), вызывает `OutsideWorkspace`, то есть `ToolError`. Путь никогда не
подменяется молча. `workspace.rel(path)` даёт POSIX-вид относительно проекта.

## Песочница

`execute` выполняется через `Agent(sandbox=...)`:

```python
from luna_core import DockerSandbox, LocalSandbox

agent = Agent(provider, model, sandbox=DockerSandbox(".", image="python:3.12-slim", network="none"))
```

`LocalSandbox` работает на хосте (по умолчанию). `DockerSandbox` запускает один контейнер на
сессию (`await sandbox.start()`, лениво выполняется первой командой) с проектом, смонтированным в
`/workspace`; `network` равен `"none"` или `"bridge"`. По окончании вызовите
`await agent.sandbox.close()`, чтобы контейнер был удалён. Своя песочница реализует
`async run(command, *, cwd, timeout, env, cancel)` и `async close()`.

## Подводные камни

- Большие результаты записываются в `.luna/tool-output/<id>.txt` и заменяются выдержкой.
- Независимые вызовы в одном сообщении ассистента выполняются параллельно; делайте инструменты
  потокобезопасными или меняйте состояние только через пути, чтобы работала блокировка по путям.
- Тайм-аут инструмента завершает вызов результатом с ошибкой, исключение не бросается.
