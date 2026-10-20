# Основные понятия

| Понятие | Класс | Роль |
|---|---|---|
| Провайдер | любой объект с `name` и `async stream(request)` | Обращается к API модели и отдаёт события модели |
| Агент | `Agent` | Конфигурация (модель, инструменты, политика, хуки, песочница) и `run(session, text)` |
| Сессия | `Session` поверх `EventLog` | Диалог, хранящийся как журнал событий SQLite с записью только в конец |
| Инструмент | `Tool`, создаётся через `@tool` | Функция, которую может вызвать модель, со схемой JSON |
| Политика | `Policy`, `Rules` | Для каждого вызова решает: выполнить, отказать или спросить человека |
| Workspace | `Workspace` | Корень проекта и правила разрешения путей агента |
| Песочница | `LocalSandbox`, `DockerSandbox` | Где `execute` выполняет команды оболочки |
| Хуки | `Hooks`, `HookResult` | Колбэки вокруг цикла (`pre_tool`, `turn_end`, ...) |

## Цикл

`agent.run(session, text)` это асинхронный генератор. Один вызов это один ход:

1. Отдаётся `TurnStarted` и делается снимок для undo; на первом ходе сессии
   выполняются хуки `session_start`; затем сообщение пользователя дописывается в журнал.
2. Если контекст занимает больше 80% окна модели, запускается сжатие.
3. Запрос потоково уходит провайдеру; отдаются события `TextDelta` / `ThinkingDelta`.
4. Каждый вызов инструмента проверяется политикой. Вызовы, требующие подтверждения, отдают
   `NeedsApproval`, и ход ждёт `session.decide(call_id, decision)`.
5. Разрешённые вызовы выполняются (чтения параллельно, записи в один путь последовательно);
   результаты дописываются, и цикл повторяется, пока модель не остановится или не будет
   достигнут `max_steps`.
6. Ход заканчивается ровно одним из событий `TurnDone`, `TurnError` или `Cancelled`.

Упавший инструмент никогда не бросает исключение из `run`: он превращается в результат с
ошибкой, который видит модель. `ProviderError` становится `TurnError`; любое другое
исключение провайдера (ошибка в коде или исчерпанный `FakeProvider`) выходит из `run`.

## События

События хода составляют публичный протокол: `TurnStarted`, `TextDelta`, `ThinkingDelta`,
`ToolStarted`, `ToolFinished`, `NeedsApproval`, `TodosUpdated`, `Notice`, `UsageUpdate`,
`Compacted`, `BackgroundDone`, `ReloadRequested`, `TurnDone`, `TurnError`, `Cancelled`.
`to_wire(event)` превращает любое из них в JSON-вид, описанный в
[protocol.md](../protocol.md).

## Сообщения

`Message` состоит из блоков: `Text`, `Thinking` (с подписью провайдера, нужной для
повторной передачи рассуждений), `Image`, `ToolCall`, `ToolResult`. `image_from_file(path)`
создаёт `Image`; изображения передаются в ход через `agent.run(session, text, images=[...])`.

## Ошибки

`LunaCoreError` это базовый класс. У `ProviderError` есть `kind` (`auth`, `rate_limit`,
`context_length`, `bad_request`, `server`, `network`), `status` и `retry_after`.
`ToolError` (и его подклассы, например `OutsideWorkspace`) это то, что код инструмента
бросает, чтобы вернуть результат с ошибкой.

## Расход и стоимость

`Usage(input, output, cache_read, cache_write, reasoning)` прикрепляется к `UsageUpdate`
и `TurnDone`. `session.usage()` и `session.cost()` накапливаются за сессию; стоимость берётся
из поставляемого каталога `models.toml` (переопределяется через `Agent(model_overrides=...)`)
и равна `None` для модели без цены. `Agent(max_cost=...)` останавливает цикл по достижении бюджета.
