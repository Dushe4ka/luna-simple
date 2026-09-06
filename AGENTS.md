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
- `luna/session.py` — потоковый REPL / режим одного запроса, подтверждения, `/reload`
- `luna/registry.py` — курируемый реестр MCP-серверов / скилов (+ `registry.toml`)
- `luna/mcp.py` — чтение/трансляция `mcp.json`; обнаружение MCP-инструментов
- `luna/skills.py` — установка/список/удаление скилов в стиле Anthropic
- `luna/subagents.py` — встроенные субагенты + из `subagents.toml`
- `luna/extension_tools.py` — инструменты агента `manage_mcp` / `manage_skills`
- `luna/ui/` — тема `rich`, заставка, консоль, диалог подтверждения, оформление реплик
- `luna/cli.py` — точка входа на argparse (`setup`/`config`/`mcp`/`skills`/`agents`)

## Соглашения

- Python 3.11+, PEP 8 / PEP 257, чистый `ruff`.
- Все импорты `deepagents` / `langgraph` держать внутри `luna/agent.py` и
  `luna/session.py`.
- ID моделей — в `luna/providers.py` или конфиге, никогда в логике агента.
- Тесты не ходят в сеть — используйте фикстуру `FakeToolCallingModel`.
