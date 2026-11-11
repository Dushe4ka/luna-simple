# Luna на Windows: Git Bash, процессы, файлы, правила

Дата: 2026-11-11 · Ветка: `feat/windows`

## Зачем

Luna (CLI, REPL, TUI, сервер протокола v1, `luna-core`) сейчас работает только на macOS и
Linux. Это нужно и само по себе, и как основа будущего десктоп-приложения под три ОС. Аудит
кода нашёл, что на Windows:

- `os.kill(pid, 0)` в `luna/server/run.py` не проверяет, а **завершает** процесс сервера;
- `os.killpg` (sandbox, hooks) отсутствует, зависшую команду нельзя прервать;
- `shell=True` уходит в `cmd.exe`, а правила `execute` разбирают команду по грамматике bash,
  поэтому их можно обойти, а команды моделей (bash) не выполняются;
- текст читается и пишется без `encoding`, в кодировке ANSI (cp1251/cp1252);
- `os.fchmod` нет в Python < 3.13.

Попутно найдены баги, которые есть **уже сейчас на всех ОС**:

- `edit_file` превращает CRLF-файл в LF (`read_text`/`write_text` переводят строки;
  подтверждено опытом);
- правила для путей сравниваются с учётом регистра, а файловые системы Windows и macOS
  (по умолчанию) регистр не различают. Возможный обход: `.ENV` при `deny` на `.env`.
  Перед исправлением подтверждается тестом.

**Критерий успеха:** весь набор тестов зелёный в CI на `ubuntu-latest`, `macos-latest` и
`windows-latest`. На Windows `execute` работает через Git Bash, отмена и таймаут снимают всё
дерево процессов, правило `deny` нельзя обойти регистром, `.exe` или путём к программе,
`edit_file` сохраняет переводы строк, кириллица не ломается.

**Главное требование пользователя:** после установки на любой ОС достаточно набрать в
терминале `luna`, и всё работает одинаково: TUI, REPL, одиночный запрос, команды агента.
Дополнительный критерий: в CI на трёх ОС Luna ставится так же, как у пользователя, и
проходит сквозной прогон (п. 8).

**Вне рамок:** публикация на PyPI, десктоп-приложение, PowerShell как оболочка, системное хранилище ключей
(Credential Manager / Keychain), опрос цвета фона терминала на Windows, сборка установщиков.

## Решения

### 1. Единый запуск shell-команд: `luna_core/shell.py` (новый)

```python
class ShellNotFound(RuntimeError): ...

def find_shell(env: Mapping[str, str] | None = None) -> list[str]:
    """argv-префикс оболочки: ['/bin/sh', '-c'] или ['C:/…/Git/bin/bash.exe', '-c']."""

def shell_argv(command: str, env=None) -> list[str]:
    """find_shell(env) + [command]."""

def popen_kwargs() -> dict:
    """Новая группа процессов: start_new_session=True на POSIX,
    creationflags=CREATE_NEW_PROCESS_GROUP на Windows."""

def kill_tree(pid: int) -> None:
    """POSIX: killpg(SIGKILL). Windows: `taskkill /PID pid /T /F`. Ошибки «нет такого процесса» глушатся."""
```

Поиск Git Bash на Windows, по порядку:

1. `LUNA_GIT_BASH_PATH`. Если путь задан, но файла нет, это `ShellNotFound` с указанием
   переменной, без молчаливого перехода к следующему шагу.
2. Рядом с `git.exe` из `PATH`: `<git>/../bin/bash.exe`, затем `<git>/../../bin/bash.exe`
   (для `cmd/git.exe` и `mingw64/bin/git.exe`).
3. `%ProgramFiles%\Git\bin\bash.exe`, `%ProgramFiles(x86)%\Git\bin\bash.exe`,
   `%LOCALAPPDATA%\Programs\Git\bin\bash.exe`.
4. `%LOCALAPPDATA%\luna\git\bin\bash.exe` — PortableGit, который ставит установщик Luna,
   если Git в системе нет (п. 8).

Путь внутри `%SystemRoot%\System32` (это WSL-лаунчер `bash.exe`) **никогда** не принимается.
Результат кешируется на процесс, функция поиска чистая и принимает `env`, чтобы её можно было
тестировать на любой ОС.

На POSIX поведение не меняется: `/bin/sh -c`, как у `create_subprocess_shell`.

Переходят на `shell.py`:

- `luna_core/sandbox.py`: `LocalSandbox.run` и `_run_process` (`create_subprocess_exec(*shell_argv(...))`,
  `kill_tree` вместо `killpg`);
- `luna_core/sandbox.py:176`: второй `shell=True`;
- `luna_core/hooks.py`: `CommandHook`;
- `luna/turn/fmt.py`, `luna/turn/diagnose.py`, `luna/turn/verify.py`, `luna/repl/usercmd.py`
  (`!cmd`).

`ShellNotFound` в `execute` превращается в обычный неуспешный результат инструмента с текстом
«Для команд на Windows нужен Git for Windows (Git Bash): https://git-scm.com/download/win,
или укажите путь в LUNA_GIT_BASH_PATH». В хуках это `notice`, в fmt/diagnose/verify шаг
пропускается с `notice`. Остальная Luna продолжает работать.

`shlex.quote` в `fmt.py`/`diagnose.py` остаётся верным, потому что команды теперь всегда
исполняет bash/sh.

Системный промпт (`luna/config/prompts.py` / `luna_core/context.py`) на Windows добавляет
строку: «Shell: Git Bash (bash). Use POSIX syntax and forward slashes».

### 2. Процессы сервера

`luna/server/run.py`:

- `_default_is_alive(pid)` на Windows: `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` +
  `GetExitCodeProcess == STILL_ACTIVE` через `ctypes`, без новых зависимостей. На POSIX без
  изменений.
- `_default_kill(pid)` использует `shell.kill_tree`.
- Запуск сервера на Windows: `creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
  CREATE_NO_WINDOW` вместо `start_new_session`, иначе закрытие консоли убивает сервер.
- `os.fchmod` заменяется вызовом `luna_core.fsperm.restrict(fd)` (см. п. 5).

### 3. Правила `execute`

`luna_core/shellsplit.py` / `policy.py`:

- **Нормализация имени программы** перед сравнением с правилами: отбрасывается путь
  (`/usr/bin/rm`, `C:/Git/usr/bin/rm.exe`, `C:\…\rm.exe` → `rm`), на Windows снимается
  суффикс `.exe`/`.cmd`/`.bat`/`.com` и имя приводится к нижнему регистру. Нормализуется
  только первое слово сегмента (программа), аргументы остаются как были.
- Отбрасывать путь безопасно на всех ОС только для **deny/ask**. Для **allow** по-прежнему
  нужно, чтобы совпадала исходная запись, иначе `./rm` (свой скрипт) совпал бы с `allow rm`.
  Правило: deny/ask проверяются и на исходном, и на нормализованном виде; allow — только на
  исходном (а на Windows — на исходном в нижнем регистре без `.exe`).
- `cmd`, `cmd.exe`, `powershell`, `pwsh`, `wsl`, `start`, `cygstart`, `winpty` добавляются к
  «запускателям» (как `bash`, `sudo`, `xargs`): для них `suggest_rules` не предлагает `allow`,
  `check_rule_safe` запрещает такие allow-правила, автоматического разрешения нет.

### 4. Файлы пользователя: переводы строк и кодировка

`luna_core/tools/files.py`:

- `edit_file`: читает байты, декодирует `utf-8` без перевода строк. Если в файле есть
  `\r\n`, то в `old_string`/`new_string`, где нет `\r\n`, все `\n` заменяются на `\r\n`.
  Результат пишется байтами. CRLF остаётся CRLF, LF остаётся LF.
- `write_file`: для существующего файла сохраняет его стиль (CRLF, если он есть в старом
  содержимом), новый файл записывает как есть (`newline=""`, без перевода).
- `AnchorTracker` уже хеширует байты, поэтому изменений не требует (проверяется тестом).

Кодировка везде явная:

- все `read_text`/`write_text`/`open` в `luna/` и `luna_core/` получают `encoding="utf-8"`;
- все `subprocess.run(..., text=True)` получают `encoding="utf-8", errors="replace"`;
- в `ruff.toml` включается `PLW1514` (preview-правило, через `explicit-preview-rules = true`).

### 5. Конфиг и права на файлы

- `luna/config/config.py::config_dir`: `XDG_CONFIG_HOME` → иначе на Windows
  `%APPDATA%\luna` → иначе `~/.config/luna`. На macOS и Linux без изменений.
- `mcp.py` (`~/.config/luna/mcp-tokens`) берёт папку из того же места: в `luna_core`
  появляется `luna_core.paths.user_config_dir()`, и `luna/config/config.py` использует его.
- `luna_core/fsperm.py` (новый): `restrict(fd)` на POSIX вызывает `os.fchmod(fd, 0o600)`, на
  Windows ничего не делает. Профиль пользователя по умолчанию закрыт ACL, а `fchmod` там
  умеет только флаг «только чтение». Используется в `credentials.py`, `server/run.py`
  (лог), `mcp.py`.

### 6. Терминал

- `luna/core/session.py::_ctrl_c_cancels`: если `add_signal_handler` недоступен
  (`NotImplementedError` на Windows), ставится `signal.signal(SIGINT, …)`, вызывающий
  `loop.call_soon_threadsafe(on_sigint)`; прежний обработчик восстанавливается.
- `luna/ui/termbg.py` уже возвращает `None` без `termios`. Тема «Авто» на Windows берёт
  `COLORFGBG`, иначе тёмную. Нужен только тест.

### 7. Регистр путей в правилах для файлов

Сначала тест: на файловой системе, не различающей регистр (macOS по умолчанию, Windows),
`read_file(".ENV")` при `deny = ["read:.env"]`.

- Если обход подтвердился, правила для путей (`Rules._hit` для не-`execute` инструментов)
  сравнивают `casefold()` шаблона и пути, когда ФС проекта не различает регистр. Это
  определяется один раз на `Policy`: создаётся временный файл в `.luna/` и проверяется,
  виден ли он под именем в другом регистре. Если проверить нельзя, считаем, что ФС регистр
  не различает (безопасная сторона: deny срабатывает шире). На `Path.resolve()` не
  опираемся: на macOS он не возвращает фактический регистр имени.
- Для allow-правил то же сравнение без учёта регистра безопасно: это тот же файл.
- Если не подтвердился, пишется только регрессионный тест.

### 8. Установка и запуск командой `luna`

Сейчас README предлагает `uv pip install luna-simple`, но на PyPI пакета нет, а установка в
venv не кладёт `luna` в `PATH`. Проверено 2026-11-11: `uv tool install
git+https://github.com/Dushe4ka/luna-simple` ставит `luna` и `luna-simple` с собственной
(не editable) копией `luna-core`, `luna --version` работает.

#### Что делают аналоги (изучено 2026-11-11)

| | Установка | Python / рантайм | Git Bash на Windows |
|---|---|---|---|
| Claude Code | `curl …/install.sh \| bash`, `irm …/install.ps1 \| iex`, версия аргументом | свой бинарник в `~/.local/bin` | ищет Git Bash, без него PowerShell; путь можно задать в настройках |
| opencode | `curl …/install \| bash`, `--version`, `--no-modify-path` | свой бинарник | порядок pwsh → powershell → Git Bash → cmd, Git Bash ищет рядом с `git` |
| aider (Python) | `curl -LsSf …/install.sh \| sh`, `irm …/install.ps1 \| iex` — это установщик uv | `uv tool install --force --python 3.12 aider-chat` | — |
| Hermes (Python) | `curl … \| bash`, `irm … \| iex`, ставит в `%LOCALAPPDATA%\hermes` | uv | **если Git нет, кладёт проверенный PortableGit в свою папку, системный Git не трогает** |
| goose | `download_cli.sh` / `download_cli.ps1` в `~/.local/bin` | свой бинарник | — |
| Codex | npm / brew | свой бинарник | PowerShell и отдельный парсер команд для правил |

Общие практики, которые берём:

- одна строка установки для каждой ОС (`curl | sh` и `irm | iex`), без прав администратора;
- Python-агенты (aider, Hermes) не просят ставить Python, а используют uv: так и делаем;
- команда в `~/.local/bin` (на Windows `%USERPROFILE%\.local\bin`), каталог добавляется в
  пользовательский `PATH`; опция «не трогать PATH»;
- установка конкретной версии;
- внешние загрузки проверяются по SHA-256 (Hermes, t3code);
- **как Hermes: если на Windows нет Git, установщик сам кладёт PortableGit** в папку Luna, и
  `execute` работает сразу, без ручной установки Git. Системный Git не трогается;
- диагностика установки (`claude doctor`) и обновление одной командой (Claude Code,
  opencode, Hermes).

PowerShell как запасную оболочку (Claude Code, opencode) не берём: с PortableGit Git Bash
есть всегда, а вторая оболочка требует второго парсера правил (как у Codex).

#### Установщик Luna

Официальный способ — **`uv tool`** на всех ОС: Python ставить не нужно, uv скачает его сам.

- `scripts/install.sh` (macOS/Linux):
  `curl -LsSf https://raw.githubusercontent.com/Dushe4ka/luna-simple/main/scripts/install.sh | sh`
- `scripts/install.ps1` (Windows):
  `powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Dushe4ka/luna-simple/main/scripts/install.ps1 | iex"`

Оба скрипта делают одно и то же:

1. Если `uv` нет, ставят его официальным установщиком Astral (`astral.sh/uv/install.sh` /
   `install.ps1`).
2. `uv tool install --python 3.12 --force "luna-simple[all] @ git+https://github.com/Dushe4ka/luna-simple@<ref>"`.
   `<ref>` по умолчанию `main`, задаётся `LUNA_REF` или параметром (`--ref` / `-Ref`).
3. `uv tool update-shell`, чтобы каталог с `luna` попал в `PATH`; пропускается при
   `--no-modify-path` / `-NoModifyPath`. Скрипт сообщает, что нужно открыть новый терминал.
4. Только Windows: если Git Bash не найден тем же поиском, что в п. 1, скачивается
   PortableGit (64-bit или arm64) с GitHub-релизов git-for-windows, конкретной версии с
   зашитым SHA-256. Он распаковывается в `%LOCALAPPDATA%\luna\git`. При несовпадении хеша
   установка Git прерывается с ошибкой, Luna остаётся установленной. Отказаться можно
   параметром `-NoGit`.
5. В конце запускают `luna doctor`.

Новые подкоманды CLI, одинаковые на всех ОС:

- `luna doctor`: версия Luna и Python, откуда запущена `luna` и есть ли она в `PATH`, папка
  конфига, найденная оболочка (или почему не найдена), `git`, состояние фонового сервера.
  Каждая строка — «ок» или «проблема и что сделать».
- `luna update`: `uv tool upgrade luna-simple`. Если Luna установлена не через `uv tool`,
  печатает, как обновиться, и ничего не делает.

Удаление: `uv tool uninstall luna-simple` (PortableGit удаляется вместе с
`%LOCALAPPDATA%\luna`). README переписывается под этот способ, установка для разработки
остаётся отдельно.

Одинаковое поведение после запуска:

- `luna/cli.py::main` на Windows переключает `stdout`/`stderr` на UTF-8
  (`reconfigure(encoding="utf-8", errors="replace")`). Иначе при выводе в файл или пайп
  (`luna --json … > out.json`) кириллица падает с `UnicodeEncodeError`, потому что
  перенаправленный вывод на Windows идёт в ANSI-кодировке.
- Фоновый сервер, который запускает TUI, на Windows стартует без окна консоли и переживает
  её закрытие (п. 2).
- Поддерживаемые терминалы на Windows: Windows Terminal и консоль Windows 10+ с VT. Глифы
  TUI (`✳ ✦ ✧ ◐ ● ◌`) есть в Cascadia Mono, шрифте Windows Terminal по умолчанию. Проверить
  отрисовку в CI нельзя, это пункт ручной проверки (см. «Риски»).

## Тесты и CI

- `.github/workflows/ci.yml`: матрица `os: [ubuntu-latest, macos-latest, windows-latest]`.
  Python 3.11 и 3.12 на Ubuntu, 3.12 на macOS и Windows. Lint только на Ubuntu.
- POSIX-тесты (11 файлов: `chmod`, сигналы, `/bin/sh`, symlink) переписываются переносимо;
  `skipif(os.name == "nt")` только там, где проверка по смыслу POSIX (права `0600`).
- Новые тесты, без сети и Windows-машины (поиск bash проверяется на подставленном `env` и
  фейковой ФС):
  - `find_shell`: переменная, соседство с `git.exe`, стандартные пути, отказ от `System32`,
    ошибка при битой `LUNA_GIT_BASH_PATH`;
  - `kill_tree` снимает внука (реально на всех трёх ОС в CI);
  - `execute` при отсутствии bash выдаёт понятную ошибку;
  - правила: `RM -rf`, `C:/x/rm.exe`, `/bin/rm`, `cmd /c del` не проходят мимо `deny rm` /
    не авто-разрешаются; `./rm` не совпадает с `allow rm`;
  - `edit_file` / `write_file` сохраняют CRLF и LF;
  - кириллица в конфиге и выводе `git`;
  - регистр путей (п. 7).
- На Windows-раннере GitHub Git Bash предустановлен, поэтому `execute`-тесты идут через
  настоящий bash.
- **Сквозной smoke-job** на трёх ОС, отдельный от pytest. Он проверяет то, что получит
  пользователь:
  1. `uv build --all-packages`, затем `uv tool install luna-simple --find-links dist`: ставятся
     собранные колёса, а не editable-исходники;
  2. `luna --version`, `luna config path` (на Windows путь в `%APPDATA%`);
  3. `scripts/ci/mock_llm.py` — крошечный OpenAI-совместимый сервер на `127.0.0.1`, который
     стримит заготовленный ответ. Первый ход вызывает `execute` с `echo привет && pwd`,
     второй ход даёт текст;
  4. Luna настраивается на него через `[provider.custom.mock]` во временном
     `XDG_CONFIG_HOME`, и запускается `luna --yolo --json "…"`. Проверяется, что вывод —
     валидный JSON, в нём есть `привет`, и инструмент `execute` завершился успешно. На
     Windows это значит, что сработал настоящий Git Bash;
  5. `luna serve` + `GET /health` через `LunaClient`, затем остановка сервера.
  Тестовых веток в продакшен-коде для этого не нужно: используется обычный кастомный
  провайдер.
- Скрипты установки проверяются в CI запуском с `LUNA_REF=<текущий sha>` на всех трёх ОС
  (`install.sh` на Ubuntu/macOS, `install.ps1` на Windows), затем `luna doctor` из нового
  shell-процесса.
- Ветка установщика с PortableGit проверяется на Windows-раннере, где системный Git скрыт
  из `PATH` на время шага. После установки `luna doctor` должен найти bash в
  `%LOCALAPPDATA%\luna\git`.

## Риски

- `taskkill /T` не снимает процессы, уже отцепившиеся от дерева (как `setsid` на POSIX). Это
  приемлемо и совпадает с текущим поведением на POSIX.
- MSYS-bash переводит аргументы, похожие на пути (`/c/...`). Команды моделей обычно пишутся
  с прямыми слешами, но возможны редкие сюрпризы. Документируется в README.
- Отрисовку TUI в Windows Terminal (глифы, цвета, ширина символов) CI не проверит. После
  зелёного CI нужен один ручной прогон на любой Windows-машине: установка скриптом, `luna`,
  один ход с `execute`, `/theme`.
- Скрипт установки ставит пакет из ветки `main`, поэтому установщик заработает у
  пользователей только после merge и push.
- Если Windows-раннер покажет падения, которые не воспроизводятся локально, отлаживать
  придётся только через CI (локальной Windows нет).
