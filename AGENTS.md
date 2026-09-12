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
- `luna/extensions/` — подключаемые возможности
  - `subagents.py` — встроенные субагенты + из `subagents.toml`
  - `extension_tools.py` — инструменты агента `manage_mcp` / `manage_skills`
  - `mcp.py` — чтение/трансляция `mcp.json`; обнаружение MCP-инструментов
  - `skills.py` — установка/список/удаление скилов в стиле Anthropic
  - `registry.py` — курируемый реестр MCP-серверов / скилов (+ `registry.toml`)
  - `lspnav.py` — LSP-навигация (`goto_definition` / `find_references` /
    `hover`), extra `luna-simple[lsp]`
  - `initgen.py` — `luna init` / `/init`
- `luna/ui/` — тема `rich`, заставка, консоль, диалог подтверждения, оформление реплик

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
  модуль framework-free.
- ID моделей — в `luna/config/providers.py` или конфиге, никогда в логике агента.
- Тесты не ходят в сеть — используйте фикстуру `FakeToolCallingModel`.
- `subagents.toml` получил ключ `unsafe` — opt-in для мутирующих инструментов
  (`write_file` / `edit_file` / `delete` / `execute`) у субагента; правки
  субагентов пишутся в общий журнал `/undo` сессии.
