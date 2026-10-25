# Субагенты и фоновая работа

## Субагенты

Инструмент `task(description, subagent_type, background, isolation)` запускает дочерний `Agent` в
новой ветке сессии со своим набором инструментов и своей моделью.

```python
from luna_core import Agent, BackgroundTasks, SubagentRunner, builtin_tools, task_tool
from luna_core.subagents import BUILTIN_SUBAGENTS, SubagentSpec
from luna_core.tools.tasks import task_cancel_tool, task_status_tool

specs = dict(BUILTIN_SUBAGENTS)          # researcher, reviewer (только чтение), general-purpose
specs["tester"] = SubagentSpec(
    "tester", "Run the test suite and report failures.", "You run tests and report.",
    tools=("ls", "read_file", "grep", "execute"), unsafe=True,
)
runner = SubagentRunner(specs, max_parallel=4)
tools = [*builtin_tools(), task_tool(runner), task_status_tool(), task_cancel_tool()]
agent = Agent(provider, model, tools=tools, services={"background": BackgroundTasks()})
```

- `SubagentSpec(name, description, prompt, tools, model, unsafe, isolation, max_steps, max_cost)`.
  По умолчанию субагент работает только на чтение (`ls`, `read_file`, `glob`, `grep`). Изменяющие
  инструменты (`write_file`, `edit_file`, `delete`, `execute`) в записи `subagents.toml` требуют
  `unsafe = true`, иначе загрузка завершается `SubagentConfigError`. Это проверяет только
  `load_subagents`; создание `SubagentSpec` в коде не проверяет.
- `load_subagents(files)` накладывает таблицы `[subagent.<name>]` из TOML-файлов поверх встроенных.
- Несколько вызовов `task` в одном сообщении ассистента выполняются параллельно, до `max_parallel`.
- Дочерний агент наследует политику, песочницу и хуки; его подтверждения отвечаются через
  родительскую сессию. `max_cost` и оставшийся бюджет родителя проверяются до запуска.

### Изоляция через git worktree

Пишущий субагент по умолчанию работает в собственном git worktree на ветке `luna/sub/*`
(`isolation="auto"`), поэтому параллельные правки не сталкиваются. Родитель получает сводку,
а также ветку и diff для слияния. Подводные камни:

- Для worktree нужен git, а корень проекта должен быть корнем git-репозитория; иначе `auto`
  возвращается к общему рабочему пространству (записи в один путь тогда выполняются
  последовательно), а явный `isolation="worktree"` это ошибка.
- Снимок для worktree включает неотслеживаемые файлы, не попавшие под игнорирование.
- Создание, очистка (`git worktree prune` и удаление осиротевших папок в `.luna/worktrees`) и
  завершение worktree идут под блокировкой и между потоками, и между процессами (REPL и сервер
  на одном репозитории): файл `<git common dir>/luna-worktrees.lock` под `flock`/`msvcrt.locking`.
- Дочерний агент, ничего не изменивший, не оставляет после себя ветку.

## Фоновые задачи

`task(..., background=true)` сразу возвращает дескриптор, а родитель продолжает работу. Когда
дочерний агент завершается, его результат дописывается в журнал, `BackgroundDone` доставляется
слушателям `session.subscribe(fn)` (через `session.notify`; в поток `agent.run` он не входит), а
текст доставляется родителю **на его ближайшей границе шага** (не мгновенно). `task_status`
и `task_cancel` проверяют и останавливают задачу; они видят только задачи своей сессии.

```python
from luna_core import BackgroundTasks

background = BackgroundTasks(max_parallel=4)
agent = Agent(..., services={"background": background})
handle = background.start("label", lambda: some_coroutine(), session=session)
info = await background.wait(handle)        # TaskInfo(status, summary, ...)
```

Завершённые записи сохраняются (последние 200) для запросов статуса.

## Фоновые сессии

`AgentManager` запускает целые ходы отдельно от любого потребителя; так сервер сохраняет запуск
живым после закрытия интерфейса:

```python
from luna_core import AgentManager

manager = AgentManager(max_parallel=4)
run = manager.start(agent, session, "refactor the parser", label="parser")
unsubscribe = manager.subscribe(session.id, print)     # словари wire-событий; повтор через events()
await manager.wait(session.id)
```

`manager.events(session_id, after=-1)` воспроизводит буферизованные события для опоздавших
подписчиков, `manager.cancel(session_id)` останавливает запуск, `manager.list()` отдаёт `RunInfo`
на каждый запуск. Приложение Luna открывает это как `luna run --background`, `/agents`, Ctrl+G в
TUI и `POST /sessions/{id}/background`.

## Роли моделей

`ModelRoles(fast=..., plan=..., vision=..., summarize=...)` сопоставляет роли идентификаторам
моделей (или парам `(provider, model)`). Субагенты по умолчанию используют `fast`, режим плана
использует `plan`, ввод изображений переключается на `vision`, когда основная модель не видит, а
сжатие использует `summarize`. Передаётся как `Agent(roles=...)`.
