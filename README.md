<div align="center">

# 🌙 Luna

**Простой лёгкий CLI-агент для работы с кодом.**

`Observe · Understand · Plan · Act`

*LUNA — тихий интеллект для навигации в сложных системах.*

</div>

---

> «Луна не спорит с ночью — она делает её понятной.»
> «Ясность вместо шума. Маршрут вместо хаоса.»
> «Я свет, который не ослепляет, а помогает видеть путь.»

- *A brighter tomorrow, together*
- *Same moon, brighter possibilities*
- *Ideas into reality*
- *Human and AI, further together*

---

Luna — это **CLI-агент для кода** на фреймворке
[`deepagents`](https://github.com/langchain-ai/deepagents) (LangChain / LangGraph).
Это намеренно маленький аналог [`pi`](https://github.com/earendil-works/pi):
единый API к моделям, агентный цикл и CLI для кодинга — с одним REPL на `rich`
вместо собственного TUI.

Акцент — на **`luna-simple`**: минимум зависимостей, одна команда, код читается
от начала до конца.

## Установка

```bash
# со всеми провайдерами
uv pip install "luna-simple[all]"

# или только провайдер по умолчанию (Anthropic)
uv pip install luna-simple

# из клона репозитория
uv venv --python 3.12 && uv pip install -e ".[dev,all]"
```

Требуется Python **3.11+**.

## Быстрый старт

```bash
# первый запуск: выбрать провайдера и вставить API-ключ
luna setup

# один запрос и выход
luna "прочитай pyproject.toml и опиши точки входа"

# исследовать репозиторий и создать/обновить AGENTS.md
luna init

# интерактивный REPL (с заставкой)
luna
```

Если запустить Luna без настроенного ключа, она сама предложит `luna setup`.
В REPL: `/help`, `/tools`, `/agents`, `/sessions`, `/resume`, `/usage`,
`/compact`, `/add`, `/drop`, `/context`, `/diff`, `/undo`, `/verify`, `/init`,
`/model`, `/provider`, `/reload`, `/new`, `/clear`, `/exit`. Команды
`/model <name>` и
`/provider <key>` теперь меняют модель или провайдера прямо в сессии,
сохраняя тред.

## Сессии и контекст

Каждая сессия — и REPL, и одиночный запрос — чекпойнтится в
`~/.config/luna/sessions.db`, так что диалог можно продолжить позже.

```bash
luna -c            # или --continue: возобновить последнюю сессию этого каталога
luna --resume            # выбрать сессию из списка
luna --resume <thread-id>  # возобновить конкретную сессию
```

- `/sessions` — список прошлых сессий этого каталога; `/resume <n>` —
  переключиться на выбранную по номеру (без номера печатает список).

- `/usage` — учёт токенов за сессию (после каждого хода печатается тусклая
  строка `ctx ~X/Y · turn … · session …`).
- `/compact` — сжать диалог в плотную заметку и продолжить на свежем треде.
- `@путь` (или `@"a b.py"`) в сообщении подставляет содержимое файла в этот
  ход; `/add путь …` закрепляет файлы во всех следующих ходах, `/drop`
  открепляет, `/context` показывает закреплённое.

## Настройка и ключи

`luna setup` создаёт два файла в `~/.config/luna/` (с учётом XDG):

- `config.toml` — провайдер, модель и прочие настройки (безопасно шарить)
- `credentials.toml` — API-ключи, права `0600`

Переменные окружения (`ANTHROPIC_API_KEY` и т.д.) всегда важнее сохранённых
ключей.

```bash
luna config path                        # где лежат файлы
luna config show                        # эффективные настройки (ключи скрыты)
luna config set model.provider deepseek
luna config set-key openai              # спросит ключ скрытым вводом
luna config unset-key openai
```

## Провайдеры

Luna создаёт модели через `init_chat_model` из LangChain. Установите нужный
API-ключ (см. `.env.example`) и выберите провайдера флагом `--provider` /
`--model` либо в конфиге.

| Провайдер | `--provider` | Модель по умолчанию | Ключ | Extra |
| --- | --- | --- | --- | --- |
| Anthropic *(по умолчанию)* | `anthropic` | `claude-sonnet-4-5` | `ANTHROPIC_API_KEY` | (входит в базовую установку) |
| DeepSeek | `deepseek` | `deepseek-chat` | `DEEPSEEK_API_KEY` | `luna-simple[deepseek]` |
| OpenAI | `openai` | `gpt-4.1` | `OPENAI_API_KEY` | `luna-simple[openai]` |
| Google | `google` | `gemini-2.5-pro` | `GOOGLE_API_KEY` | `luna-simple[google]` |
| Ollama | `ollama` | `qwen2.5-coder` | — (локально) | `luna-simple[ollama]` |

```bash
luna --provider deepseek --model deepseek-reasoner "отрефактори utils.py"
luna --provider ollama "объясни этот стек-трейс"
```

Можно по-прежнему пользоваться обычным окружением / `.env` — см. `.env.example`.

## Расширение Luna

Luna может получать новые возможности по запросу — вы через CLI, либо сам агент
(он спрашивает подтверждение, а затем просит выполнить `/reload`).

```bash
luna mcp add github            # из встроенного реестра
luna mcp add custom -- npx -y my-mcp-server
luna mcp list
luna skills add pdf            # имя из реестра
luna skills add owner/repo/path/to/skill
luna agents list              # встроенные: researcher, reviewer
```

- **MCP-серверы** лежат в `~/.config/luna/mcp.json` (или `./.luna/mcp.json`)
  в стандартном формате `{"mcpServers": {...}}` — записи можно копировать прямо
  из Claude Desktop / Claude Code. `${ENV}` подставляется. Нужен
  `pip install "luna-simple[mcp]"`.
- **Скилы** — это Anthropic Agent Skills (`<имя>/SKILL.md`), ставятся в
  `~/.config/luna/skills/`. Нужен `git`.
- **Субагенты** описываются в `~/.config/luna/subagents.toml`; агент делегирует
  им работу инструментом `task`.
- В REPL: `/reload` активирует добавленные скилы / MCP / субагенты без
  перезапуска; `/tools`, `/agents` показывают, что доступно.

## Безопасность

Luna работает с **реальными файлами** в текущей директории и может выполнять
команды оболочки. Перед каждым `write_file`, `edit_file`, `delete` и `execute`
она останавливается и спрашивает:

```
[Enter] approve · [e] edit · [n] reject >
```

Флаг `--yolo` отключает все запросы подтверждения.

## Конфигурация

Приоритет (по убыванию): флаги CLI → окружение → `./.luna.toml` →
`~/.config/luna/config.toml` → значения по умолчанию.

```toml
# .luna.toml
[model]
provider = "anthropic"
name = "claude-sonnet-4-5"
fast = "anthropic:claude-haiku-4-5"

[agent]
yolo = false
temperature = 0.0
verify_command = "uv run pytest -q"

[permissions]
allow = ["execute:pytest*"]
deny = ["execute:git push*", "write_file:.env"]

[ui]
splash = true
```

- `[model] fast` — необязательная более дешёвая модель (полная строка
  `<префикс>:<модель>`), её используют встроенные субагенты `researcher` /
  `reviewer`.
- `[agent] verify_command` — команда проверки: после хода, изменившего файлы,
  Luna запускает её и при провале один раз отдаёт вывод агенту на
  исправление; `/verify` — вручную.
- `[permissions]` — списки `allow` / `deny` вида `"<инструмент>:<glob>"`;
  `deny` перекрывает даже `--yolo`. То же можно положить в
  `<repo>/.luna/permissions.toml`, а пункт `[a] always` в запросе
  подтверждения дописывает правило `allow` туда сам.

Окружение: `LUNA_PROVIDER`, `LUNA_MODEL`, `LUNA_YOLO`, `LUNA_WORKDIR`.

## Ограничения

- `/compact` не учитывает токены и не запускает verify — это просто сжатие
  диалога в заметку и переход на новый тред.
- `luna --continue` возобновляет тред, но заводит свежий журнал `/diff` /
  `/undo`: изменения прошлого запуска через них не видны.
- deny-правила действуют по точному имени инструмента: `write_file:.env` не
  покрывает `edit_file`, `delete` или `execute` над тем же файлом.
- Снапшоты (`/diff` / `/undo`) и правила доступа не применяются к вызовам
  инструментов внутри субагентов.

## Лицензия

MIT — см. [LICENSE](LICENSE).
