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

- `luna/config.py` — слоистое разрешение настроек в `LunaConfig`; запись `config.toml`
- `luna/credentials.py` — API-ключи в `~/.config/luna/credentials.toml` (права 0600)
- `luna/setup_wizard.py` — интерактивный мастер `luna setup`
- `luna/providers.py` — реестр провайдеров → chat-модель LangChain
- `luna/prompts.py` — системный промпт Luna
- `luna/agent.py` — сборка `create_deep_agent` (вызовы фреймворка живут здесь)
- `luna/session.py` — потоковый REPL / режим одного запроса, подтверждения, `/reload`, `compact_thread`
- `luna/commands.py` — диспетчер slash-команд
- `luna/persistence.py` — SqliteSaver + индекс сессий
- `luna/usage.py` — учёт токенов
- `luna/context.py` — `@file` + закреплённые файлы
- `luna/permissions.py` — правила allow/deny
- `luna/toolguard.py` — middleware: deny + снапшоты
- `luna/undo.py` — журнал снапшотов, `/diff` `/undo` `/redo` (в git — снапшоты
  дерева + разговора через `git commit-tree`, вне git — файловый журнал)
- `luna/gitinfo.py` — проверка git-дерева
- `luna/memory.py` — `.luna/memory/*.md`
- `luna/verify.py` — verify-команда
- `luna/fmt.py` — автоформатирование тронутых файлов после правок
- `luna/diagnose.py` — диагностика после правок, `/diagnose`
- `luna/lspnav.py` — LSP-навигация (`goto_definition` / `find_references` /
  `hover`), extra `luna-simple[lsp]`
- `luna/usercmd.py` — пользовательские slash-команды из `.luna/commands/*.md`
- `luna/models.toml` — реестр моделей: окно контекста, цена input/output
- `luna/initgen.py` — `luna init`
- `luna/registry.py` — курируемый реестр MCP-серверов / скилов (+ `registry.toml`)
- `luna/mcp.py` — чтение/трансляция `mcp.json`; обнаружение MCP-инструментов
- `luna/skills.py` — установка/список/удаление скилов в стиле Anthropic
- `luna/subagents.py` — встроенные субагенты + из `subagents.toml`
- `luna/extension_tools.py` — инструменты агента `manage_mcp` / `manage_skills`
- `luna/ui/` — тема `rich`, заставка, консоль, диалог подтверждения, оформление реплик
- `luna/cli.py` — точка входа на argparse (`setup`/`config`/`mcp`/`skills`/`agents`)

## Соглашения

- Python 3.11+, PEP 8 / PEP 257, чистый `ruff`.
- Все импорты `deepagents` / `langgraph` держать внутри `luna/agent.py`,
  `luna/session.py`, `luna/persistence.py` и `luna/toolguard.py`. Известные
  исключения: `luna/subagents.py` держит модульные импорты `SubAgent` /
  `FilesystemMiddleware` из `deepagents` (нужны для сборки декларативных
  субагентов); `luna/undo.py` — `undo()`/`redo()` принимают уже собранного
  агента параметром и лениво импортируют `langchain_core.messages` только
  внутри этих двух функций, остальной модуль framework-free.
- ID моделей — в `luna/providers.py` или конфиге, никогда в логике агента.
- Тесты не ходят в сеть — используйте фикстуру `FakeToolCallingModel`.
- `subagents.toml` получил ключ `unsafe` — opt-in для мутирующих инструментов
  (`write_file` / `edit_file` / `delete` / `execute`) у субагента; правки
  субагентов пишутся в общий журнал `/undo` сессии.
