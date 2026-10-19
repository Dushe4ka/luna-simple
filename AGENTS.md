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
  - `store.py` — журнал событий сессий ядра (`sessions.db`), `conversations`
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
  - `engine.py` — подготовка хода и хуки format/diagnose/verify на `luna-core`
  - `gitinfo.py` — проверка git-дерева
  - `fmt.py` — автоформатирование тронутых файлов после правок
  - `diagnose.py` — диагностика после правок, `/diagnose`
  - `verify.py` — verify-команда
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
- `luna/server/` — локальный Starlette+SSE сервер протокола v1
  (`docs/protocol.md`) поверх `luna-core`: каждый ход идёт в
  `AgentManager` и переживает отключение клиента
  - `app.py` — `create_app(token=, build=, trust_check=, log=)`: маршруты,
    bearer-auth (чистый ASGI), `on_done` хода (уведомление о завершении,
    авто-reload, `after_turn`)
  - `auth.py` — файл токена сервера (`~/.config/luna/server.json`, права
    0600), который читают TUI/CLI-клиенты
  - `runtime.py` — `SessionRuntime` (сессия ядра + `SessionState` + бандл
    агента + out-of-turn события `session.notify`) и LRU `RuntimeRegistry`
  - `sessions.py` — `GET/POST/DELETE /sessions` над журналом событий
  - `turns.py` — `/messages`, `/background`, `/events`, `/approve`,
    `/cancel`, `/agents`, история
  - `commands.py` — `/command`, `/commands`, `/state`
  - `run.py` — жизненный цикл процесса сервера (`ensure_running`),
    `make_session_build` (свой агент на каждую сессию) и `luna serve`
- `luna/tui/` — полноэкранный клиент на `Textual` поверх `luna/server/`
  (через `luna_core.client.LunaClient`, протокол v1)
  - `app.py` — `LunaApp`: 3-зонная раскладка (сайдбары + чат + статус-бар),
    `Ctrl+B` скрывает/показывает сайдбары
  - `theme.py` — палитра `luna/ui/theme.py`, переведённая в CSS-переменные
    Textual
  - `app.py` также: `Ctrl+G` — задача в фон, опрос `/agents` каждые 3 с
    (тосты о завершении фоновых сессий/задач)
  - `chat.py` — центральная панель: стриминг ответа в Markdown-транскрипт,
    автодополнение slash-команд, подтверждения через `decide` (поток хода
    продолжается), Esc/`Ctrl+C` — отмена хода, вставка пути картинки →
    `@mention`, наблюдатель out-of-turn подтверждений (`/events`)
  - `errors.py` — русский текст для `ServerError` клиента
  - `commands.py` — фильтрация списка slash-команд для автодополнения
    (переиспользует `luna/repl/commands.py`'s `HELP`)
  - `sidebar_sessions.py` — левый сайдбар: список сессий текущего
    каталога, клик постит `SessionSelected`
  - `sidebar_activity.py` — правый сайдбар: live-список инструментов,
    выполняющихся в текущем ходе
  - `approval_modal.py` — модальный диалог подтверждения мутирующих
    действий (approve/always/edit/reject) взамен блокирующего REPL-промпта
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
- Прямых импортов `deepagents` / `langgraph` в приложении больше нет: всё работает на
  `luna-core`. Slash-команды — одна реализация в `luna/commands` (async, общая для
  REPL и сервера); откат/повтор/diff живут в `luna_core.undo` и `Session`.
- ID моделей — в `luna/config/providers.py` или конфиге, никогда в логике агента.
- Тесты не ходят в сеть — используйте `FakeProvider` (`luna_core.providers.fake`).
- `subagents.toml` получил ключ `unsafe` — opt-in для мутирующих инструментов
  (`write_file` / `edit_file` / `delete` / `execute`) у субагента; правки
  субагентов пишутся в общий журнал `/undo` сессии.
