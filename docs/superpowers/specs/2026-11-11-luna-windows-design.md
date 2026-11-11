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

**Вне рамок:** десктоп-приложение, PowerShell как оболочка, системное хранилище ключей
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

## Риски

- `taskkill /T` не снимает процессы, уже отцепившиеся от дерева (как `setsid` на POSIX). Это
  приемлемо и совпадает с текущим поведением на POSIX.
- MSYS-bash переводит аргументы, похожие на пути (`/c/...`). Команды моделей обычно пишутся
  с прямыми слешами, но возможны редкие сюрпризы. Документируется в README.
- Если Windows-раннер покажет падения, которые не воспроизводятся локально, отлаживать
  придётся только через CI (локальной Windows нет).
