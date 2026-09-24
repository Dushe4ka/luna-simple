# AGENTS.md

Этот репозиторий **и есть** Luna — простой лёгкий CLI-агент для кода на
фреймворке `deepagents`. Когда Luna запускается внутри этого репозитория, файл
загружается как память.

## Сборка и тесты

- Окружение: `uv venv --python 3.12 && uv pip install -e ".[dev,all]"`
- Тесты: `uv run pytest`
- Линт: `uv run ruff check .` и `uv run ruff format --check .`
- Локальный запуск: `uv run luna --no-splash "…"`

## Структура

- `luna/cli.py` — точка входа на argparse (`setup`/`config`/`mcp`/`skills`/`agents`/`init`)
- `luna/core/` — рантайм агента и его защита
  - `agent.py` — сборка `create_deep_agent` (вызовы фреймворка живут здесь)
  - `session.py` — потоковый REPL / режим одного запроса, подтверждения, `/reload`, `compact_thread`
  - `turn_events.py` — `iter_turn`: чистый генератор типизированных событий
    одного прохода `agent.stream(...)` (без `deepagents`/`langgraph`); из
    него `session.py`'s `_stream_turn` строит REPL-рендер, а
    `luna/server/turns.py` — SSE для TUI
  - `persistence.py` — SqliteSaver + индекс сессий
  - `toolguard.py` — middleware: deny-правила + снапшоты + `/plan`
  - `permissions.py` — правила allow/deny
- `luna/repl/` — интерактивный слой поверх core
  - `commands.py` — диспетчер slash-команд
  - `setup_wizard.py` — интерактивный мастер `luna setup`
  - `usercmd.py` — пользовательские slash-команды из `.luna/commands/*.md`
- `luna/config/` — настройки, ключи, провайдеры, цены
  - `config.py` — слоистое разрешение настроек в `LunaConfig`; запись `config.toml`
  - `credentials.py` — API-ключи в `~/.config/luna/credentials.toml` (права 0600)
  - `providers.py` — реестр провайдеров → chat-модель LangChain
  - `prompts.py` — системный промпт Luna
  - `usage.py` — учёт токенов, `$`-стоимость (`models.toml`)
  - `models.toml` — реестр моделей: окно контекста, цена input/output
- `luna/turn/` — всё, что крутится вокруг одного хода
  - `context.py` — `@file` + закреплённые файлы
  - `memory.py` — `.luna/memory/*.md`
  - `undo.py` — журнал снапшотов, `/diff` `/undo` `/redo` (в git — снапшоты
    дерева + разговора через `git commit-tree`, вне git — файловый журнал)
  - `gitinfo.py` — проверка git-дерева
  - `fmt.py` — автоформатирование тронутых файлов после правок
  - `diagnose.py` — диагностика после правок, `/diagnose`
  - `verify.py` — verify-команда
  - `anchor.py` — трекинг хешей содержимого прочитанных/записанных
    файлов, staleness-check перед `write_file`/`edit_file`/`delete`
- `luna/extensions/` — подключаемые возможности
  - `subagents.py` — встроенные субагенты + из `subagents.toml`
  - `extension_tools.py` — инструменты агента `manage_mcp` / `manage_skills` /
    `save_skill`
  - `mcp.py` — чтение/трансляция `mcp.json`; обнаружение MCP-инструментов
  - `skills.py` — установка/список/удаление скилов в стиле Anthropic
  - `registry.py` — курируемый реестр MCP-серверов / скилов (+ `registry.toml`)
  - `lspnav.py` — LSP-навигация (`goto_definition` / `find_references` /
    `hover` / `symbol_range`), extra `luna-simple[lsp]`
  - `initgen.py` — `luna init` / `/init`
- `luna/server/` — локальный Starlette+SSE сервер, обёртка над
  существующими session/agent/toolguard без изменения их логики
  - `app.py` — сборка `Starlette`-приложения: маршруты + bearer-auth
    middleware
  - `auth.py` — файл токена сервера (`~/.config/luna/server.json`, права
    0600), который читают TUI/CLI-клиенты
  - `sessions.py` — `GET/POST /sessions` — тонкая обёртка над
    `SessionIndex` (список сессий, относительное время активности)
  - `turns.py` — `POST /sessions/{id}/messages`: гоняет `iter_turn` и
    стримит его события как SSE
  - `approvals.py` — `POST /sessions/{id}/approve`: возобновляет ход,
    остановленный на `Interrupted`, через `Command(resume=...)`
  - `run.py` — жизненный цикл процесса сервера: переиспользовать-или-
    запустить (`ensure_running`) и сама подкоманда `luna serve`
  - `client.py` — асинхронный HTTP+SSE клиент к локальному серверу,
    которым пользуется TUI
- `luna/tui/` — полноэкранный клиент на `Textual` поверх `luna/server/`
  - `app.py` — `LunaApp`: 3-зонная раскладка (сайдбары + чат + статус-бар),
    `Ctrl+B` скрывает/показывает сайдбары
  - `theme.py` — палитра `luna/ui/theme.py`, переведённая в CSS-переменные
    Textual
  - `chat.py` — центральная панель: стриминг ответа в Markdown-транскрипт,
    автодополнение slash-команд, обработка approval-паузы
  - `commands.py` — фильтрация списка slash-команд для автодополнения
    (переиспользует `luna/repl/commands.py`'s `HELP`)
  - `sidebar_sessions.py` — левый сайдбар: список сессий текущего
    каталога, клик постит `SessionSelected`
  - `sidebar_activity.py` — правый сайдбар: live-список инструментов,
    выполняющихся в текущем ходе
  - `approval_modal.py` — модальный диалог подтверждения мутирующих
    действий (approve/always/reject) взамен блокирующего REPL-промпта
  - `status_bar.py` — нижняя строка статуса: модель, стоимость,
    закреплённый `@file`, режим `/plan`, глубина `/undo`
- `luna/ui/` — тема `rich` и весь визуальный слой REPL
  - `theme.py` — цветовая палитра и `rich`-тема
  - `colors.py` — чистая RGB-математика (используется `splash.py` и `progress.py`)
  - `splash.py` — стартовая заставка (градиентный wordmark)
  - `progress.py` — живой индикатор выполнения инструментов во время хода
  - `answer.py` — живой рендер markdown для текста ответа ассистента
  - `turn.py` — рамка хода (открывающая/закрывающая линия)
  - `console.py` — консоль `rich`
  - `approve.py` — диалог подтверждения мутирующих действий
  - `interact.py` — стрелочный выбор (questionary) с текстовым откатом

## Соглашения

- Python 3.11+, PEP 8 / PEP 257, чистый `ruff`.
- Все импорты `deepagents` / `langgraph` держать внутри `luna/core/agent.py`,
  `luna/core/session.py`, `luna/core/persistence.py` и
  `luna/core/toolguard.py`. Известные исключения:
  `luna/extensions/subagents.py` держит модульные импорты `SubAgent` /
  `FilesystemMiddleware` из `deepagents` (нужны для сборки декларативных
  субагентов); `luna/turn/undo.py` — `undo()`/`redo()` принимают уже
  собранного агента параметром и лениво импортируют
  `langchain_core.messages` только внутри этих двух функций, остальной
  модуль framework-free; `luna/server/approvals.py` — модульный импорт
  `Command` из `langgraph.types` (зеркалит собственное использование в
  `session.py`), нужен, чтобы завернуть решение пользователя в
  `Command(resume=...)` и возобновить ход. Остальной `luna/server/`
  (общается с движком только через `luna/core/turn_events.py`'s
  `iter_turn`, который сам уже framework-free) и весь `luna/tui/` —
  прямых импортов `deepagents`/`langgraph` не держат.
- ID моделей — в `luna/config/providers.py` или конфиге, никогда в логике агента.
- Тесты не ходят в сеть — используйте фикстуру `FakeToolCallingModel`.
- `subagents.toml` получил ключ `unsafe` — opt-in для мутирующих инструментов
  (`write_file` / `edit_file` / `delete` / `execute`) у субагента; правки
  субагентов пишутся в общий журнал `/undo` сессии.
