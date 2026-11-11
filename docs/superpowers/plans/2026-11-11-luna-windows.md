# Luna на Windows — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Luna ставится одной командой и запускается как `luna` одинаково на macOS, Linux и Windows. На Windows команды агента идут через Git Bash, правила безопасности нельзя обойти, а файлы пользователя не портятся.

**Architecture:**
- Новый модуль `luna_core/shell.py` становится единственным местом, которое знает, какой оболочкой и как запускать команду и как снимать дерево процессов. На него переводятся все шесть мест с `shell=True`.
- Разбор правил `execute` дополняется нормализацией имени программы. Файловые инструменты сохраняют переводы строк. Конфиг и права на файлы получают платформенные функции.
- Сверху добавляются `luna doctor`, `luna update`, установщики одной строкой и CI на трёх ОС со сквозным smoke-прогоном через мок-LLM.

**Tech Stack:** Python 3.11+, asyncio subprocess, ctypes (WinAPI), uv / `uv tool`, GitHub Actions (ubuntu / macos / windows), POSIX sh и PowerShell для установщиков.

**Spec:** `docs/superpowers/specs/2026-11-11-luna-windows-design.md`

## Global Constraints

- Python ≥ 3.11 (`requires-python = ">=3.11"`), PEP 8 / PEP 257, чистые `ruff check .` и `ruff format --check .`.
- Новых runtime-зависимостей нет: только стандартная библиотека (ctypes, subprocess).
- Тесты не ходят в сеть. Провайдеры — `FakeProvider`, в smoke-прогоне — локальный мок на `127.0.0.1`.
- На macOS и Linux поведение не меняется: `/bin/sh -c`, `~/.config/luna`, `0600`.
- Оболочка на Windows — Git Bash. Порядок поиска: `LUNA_GIT_BASH_PATH` → рядом с `git.exe` из `PATH` → `%ProgramFiles%\Git`, `%ProgramFiles(x86)%\Git`, `%LOCALAPPDATA%\Programs\Git` → `%LOCALAPPDATA%\luna\git` (PortableGit). Всё, что лежит под `%SystemRoot%` или в `\WindowsApps\`, отвергается (это WSL-лаунчер).
- Если `LUNA_GIT_BASH_PATH` задана, но файла нет, это ошибка `ShellNotFound` без перехода к следующим кандидатам.
- PortableGit закреплён на `v2.56.0.windows.4`:
  - x64: `https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/PortableGit-2.56.0.4-64-bit.7z.exe`, sha256 `b1f3e52216e2ed6882d1bebfd4d3fdf319bec40d2edbbf9fd26ec4fd3fce7ed1`;
  - arm64: `https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/PortableGit-2.56.0.4-arm64.7z.exe`, sha256 `044110f9d951858452ddb5e4b5415ea0f2b5b2598e794d6fec77fe28b9aa8cc0`.
- Репозиторий установки: `https://github.com/Dushe4ka/luna-simple`, пакет `luna-simple`, Python для `uv tool` — 3.12.
- Тексты для пользователя на русском, как и вся остальная Luna.
- Каждый коммит заканчивается строкой `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Путь с пробелами и кириллицей** (`C:\Users\Павел\My Project`) как `cwd` для `execute` и хуков: команда должна выполниться, а вывод с кириллицей — прочитаться. Тест в задаче 2.
2. **Смешанные переводы строк в одном файле** (часть строк CRLF, часть LF): `edit_file` не должен ни терять совпадение, ни менять чужие строки. Тест в задаче 6.
3. **`ENV.EXE rm -rf x` и `BASH.EXE -c "rm -rf x"` на Windows**: префикс и оболочка в верхнем регистре с `.exe` не должны выводить команду из-под `deny rm`. Тест в задаче 4.
4. **Вывод в файл или пайп на Windows** (`luna --json … > out.json`) с кириллицей: не должно быть `UnicodeEncodeError`. Тест в задаче 10, проверка в smoke (задача 13).
5. **`luna update` на Windows, пока запущен `luna.exe`**: Windows блокирует запущенный exe, поэтому обновление не должно падать на «отказано в доступе». Тест в задаче 11.

---

## Файловая структура

| Файл | Ответственность |
|---|---|
| `packages/luna-core/src/luna_core/shell.py` (новый) | Выбор оболочки, argv, флаги новой группы процессов, снятие дерева, синхронный запуск, PortableGit в `PATH` |
| `packages/luna-core/src/luna_core/paths.py` (новый) | Папка пользовательского конфига Luna на каждой ОС |
| `packages/luna-core/src/luna_core/fsperm.py` (новый) | «Только владелец» для файла: `fchmod 0600` на POSIX, ничего на Windows |
| `packages/luna-core/src/luna_core/sandbox.py` | `LocalSandbox` через `shell.py` |
| `packages/luna-core/src/luna_core/hooks.py` | `CommandHook` через `shell.py` |
| `packages/luna-core/src/luna_core/shellsplit.py` | `program_name`, нормализация `.exe` и регистра |
| `packages/luna-core/src/luna_core/policy.py` | Новые «запускатели», `fold_case` для путей, проба регистра ФС |
| `packages/luna-core/src/luna_core/tools/files.py` | Сохранение CRLF/LF |
| `packages/luna-core/src/luna_core/context.py` | Строка `Shell:` в системном промпте |
| `packages/luna-core/src/luna_core/mcp.py` | Папка токенов и права через новые модули |
| `luna/turn/fmt.py`, `diagnose.py`, `verify.py`, `luna/repl/usercmd.py` | `shell.run_sync` вместо `shell=True` |
| `luna/config/config.py`, `credentials.py`, `luna/server/auth.py`, `luna/server/run.py` | Конфиг-папка, права, процесс сервера на Windows |
| `luna/core/session.py` | Ctrl+C на Windows |
| `luna/cli.py` | UTF-8 stdio, PortableGit в `PATH`, подкоманды `doctor` / `update` |
| `luna/doctor.py` (новый) | Проверки установки |
| `scripts/install.sh`, `scripts/install.ps1` (новые) | Установщики |
| `scripts/ci/mock_llm.py`, `scripts/ci/smoke.py` (новые) | Сквозной прогон в CI |
| `.github/workflows/ci.yml` | Матрица трёх ОС, smoke, установщики |
| `tests/test_portability.py` (новый) | AST-проверка: нет текстового I/O без `encoding` |

---

### Task 1: `luna_core/shell.py` — единая оболочка

**Files:**
- Create: `packages/luna-core/src/luna_core/shell.py`
- Test: `packages/luna-core/tests/test_shell.py`

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `IS_WINDOWS: bool` — модульный атрибут; тесты подменяют его через `monkeypatch.setattr(shell, "IS_WINDOWS", True)`;
  - `GIT_BASH_ENV = "LUNA_GIT_BASH_PATH"`;
  - `class ShellNotFound(RuntimeError)`;
  - `git_bash_candidates(env: Mapping[str, str], which=shutil.which) -> list[str]`;
  - `find_shell(env=None, *, windows=None, exists=os.path.isfile, which=shutil.which) -> list[str]` — префикс argv, например `["/bin/sh", "-c"]`;
  - `shell_argv(command: str) -> list[str]` — кешированный `find_shell()` + `[command]`; `reset_cache()`;
  - `describe_shell() -> str` — строка для промпта и `luna doctor`;
  - `popen_kwargs() -> dict`;
  - `kill_tree(pid: int) -> None`;
  - `run_sync(command, *, cwd, timeout, env=None) -> subprocess.CompletedProcess[str]` — поднимает `ShellNotFound`, `subprocess.TimeoutExpired`, `OSError`;
  - `portable_git_root(env=None) -> Path | None`;
  - `ensure_git_on_path(env: MutableMapping | None = None) -> bool`.

- [ ] **Step 1: Написать падающие тесты**

```python
# packages/luna-core/tests/test_shell.py
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from luna_core import shell

WIN_ENV = {
    "SystemRoot": r"C:\Windows",
    "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "LOCALAPPDATA": r"C:\Users\u\AppData\Local",
    "PATH": "",
}


def _exists(*paths):
    present = {p.lower() for p in paths}
    return lambda p: p.lower() in present


def _no_git(name, path=None):
    return None


def test_posix_uses_bin_sh():
    assert shell.find_shell({}, windows=False) == ["/bin/sh", "-c"]


def test_windows_explicit_env_wins():
    env = {**WIN_ENV, "LUNA_GIT_BASH_PATH": r"D:\tools\Git\bin\bash.exe"}
    argv = shell.find_shell(env, windows=True, exists=_exists(r"D:\tools\Git\bin\bash.exe"),
                            which=_no_git)
    assert argv == [r"D:\tools\Git\bin\bash.exe", "-c"]


def test_windows_explicit_env_missing_file_is_an_error_not_a_fallback():
    env = {**WIN_ENV, "LUNA_GIT_BASH_PATH": r"D:\nope\bash.exe"}
    std = r"C:\Program Files\Git\bin\bash.exe"
    with pytest.raises(shell.ShellNotFound, match="LUNA_GIT_BASH_PATH"):
        shell.find_shell(env, windows=True, exists=_exists(std), which=_no_git)


def test_windows_finds_bash_next_to_git_on_path():
    git = r"E:\PortableGit\cmd\git.exe"
    bash = r"E:\PortableGit\bin\bash.exe"
    argv = shell.find_shell(WIN_ENV, windows=True, exists=_exists(bash),
                            which=lambda name, path=None: git if name == "git" else None)
    assert argv[0] == bash


def test_windows_finds_bash_for_mingw64_git():
    git = r"E:\G\mingw64\bin\git.exe"
    bash = r"E:\G\bin\bash.exe"
    argv = shell.find_shell(WIN_ENV, windows=True, exists=_exists(bash),
                            which=lambda name, path=None: git if name == "git" else None)
    assert argv[0] == bash


def test_windows_standard_and_portable_locations():
    std = r"C:\Program Files\Git\bin\bash.exe"
    assert shell.find_shell(WIN_ENV, windows=True, exists=_exists(std), which=_no_git)[0] == std
    portable = r"C:\Users\u\AppData\Local\luna\git\bin\bash.exe"
    assert shell.find_shell(WIN_ENV, windows=True, exists=_exists(portable),
                            which=_no_git)[0] == portable


@pytest.mark.parametrize("wsl", [
    r"C:\Windows\System32\bash.exe",
    r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\bash.exe",
])
def test_windows_never_picks_the_wsl_launcher(wsl):
    env = {**WIN_ENV, "LUNA_GIT_BASH_PATH": wsl}
    with pytest.raises(shell.ShellNotFound):
        shell.find_shell(env, windows=True, exists=_exists(wsl), which=_no_git)


def test_windows_without_git_bash_explains_how_to_get_it():
    with pytest.raises(shell.ShellNotFound, match="git-scm.com"):
        shell.find_shell(WIN_ENV, windows=True, exists=_exists(), which=_no_git)


def test_run_sync_runs_a_shell_line_and_decodes_utf8(tmp_path):
    proc = shell.run_sync("echo привет && echo err 1>&2", cwd=tmp_path, timeout=30)
    assert proc.returncode == 0
    assert "привет" in proc.stdout and "err" in proc.stderr


def test_run_sync_cwd_with_spaces_and_cyrillic(tmp_path):
    cwd = tmp_path / "Мой проект"
    cwd.mkdir()
    (cwd / "файл.txt").write_text("x", encoding="utf-8")
    proc = shell.run_sync("ls", cwd=cwd, timeout=30)
    assert "файл.txt" in proc.stdout


def test_run_sync_timeout_kills_the_whole_tree(tmp_path):
    marker = tmp_path / "late"
    py = Path(sys.executable).as_posix()
    late = f'"{py}" -c "import time,pathlib; time.sleep(3); pathlib.Path(r\'{marker}\').touch()"'
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        shell.run_sync(f"{late} & sleep 30", cwd=tmp_path, timeout=0.5)
    assert time.monotonic() - started < 15
    time.sleep(3.5)
    assert not marker.exists()


def test_kill_tree_on_a_dead_pid_is_silent():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    shell.kill_tree(proc.pid)  # no exception


def test_ensure_git_on_path_prepends_portable_git(tmp_path, monkeypatch):
    root = tmp_path / "luna" / "git"
    (root / "cmd").mkdir(parents=True)
    (root / "cmd" / "git.exe").write_bytes(b"")
    env = {"LOCALAPPDATA": str(tmp_path), "PATH": "C:/x"}
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    monkeypatch.setattr(shell.shutil, "which", lambda name, path=None: None)
    assert shell.ensure_git_on_path(env) is True
    assert env["PATH"].split(os.pathsep)[0] == str(root / "cmd")


def test_ensure_git_on_path_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(shell, "IS_WINDOWS", False)
    env = {"PATH": "a"}
    assert shell.ensure_git_on_path(env) is False and env == {"PATH": "a"}


def test_describe_shell_mentions_bash_on_windows(monkeypatch):
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    monkeypatch.setattr(shell, "find_shell", lambda env=None, **kw: [r"C:\G\bin\bash.exe", "-c"])
    assert "Git Bash" in shell.describe_shell()
```

- [ ] **Step 2: Запустить и убедиться, что тесты падают**

Run: `uv run pytest packages/luna-core/tests/test_shell.py -q -o addopts=""`
Expected: FAIL с `ImportError: cannot import name 'shell' from 'luna_core'` (или `ModuleNotFoundError`).

- [ ] **Step 3: Реализация**

```python
# packages/luna-core/src/luna_core/shell.py
"""One way to run a shell command line on every OS: ``sh`` on POSIX, Git Bash on Windows.

Every place that runs a user or model command line (``execute``, command
hooks, formatters, verify) goes through here, so the shell grammar that
permission rules parse (:mod:`luna_core.shellsplit`, bash) is the grammar
that actually runs the command. On Windows ``cmd.exe`` would not be.
"""

from __future__ import annotations

import contextlib
import functools
import os
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path, PureWindowsPath

IS_WINDOWS = os.name == "nt"
GIT_BASH_ENV = "LUNA_GIT_BASH_PATH"
INSTALL_HINT = (
    "Для команд на Windows нужен Git for Windows (Git Bash): https://git-scm.com/download/win "
    f"— или укажите путь к bash.exe в {GIT_BASH_ENV}"
)


class ShellNotFound(RuntimeError):
    """No usable shell for running command lines on this machine."""


def _is_wsl_launcher(path: str, env: Mapping[str, str]) -> bool:
    """True for ``bash.exe`` stubs that start WSL instead of Git Bash."""
    lowered = str(PureWindowsPath(path)).lower()
    system_root = str(PureWindowsPath(env.get("SystemRoot") or r"C:\Windows")).lower()
    return lowered.startswith(system_root + "\\") or "\\windowsapps\\" in lowered


def git_bash_candidates(env: Mapping[str, str], which=shutil.which) -> list[str]:
    """Places Git Bash is looked for, in order (see the module docstring)."""
    out: list[str] = []
    git = which("git", path=env.get("PATH"))
    if git:
        git_path = PureWindowsPath(git)
        out.append(str(git_path.parent.parent / "bin" / "bash.exe"))
        out.append(str(git_path.parent.parent.parent / "bin" / "bash.exe"))
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        if env.get(var):
            out.append(str(PureWindowsPath(env[var]) / "Git" / "bin" / "bash.exe"))
    local = env.get("LOCALAPPDATA")
    if local:
        out.append(str(PureWindowsPath(local) / "Programs" / "Git" / "bin" / "bash.exe"))
        out.append(str(PureWindowsPath(local) / "luna" / "git" / "bin" / "bash.exe"))
    return out


def find_shell(
    env: Mapping[str, str] | None = None,
    *,
    windows: bool | None = None,
    exists: Callable[[str], bool] = os.path.isfile,
    which=shutil.which,
) -> list[str]:
    """Return the argv prefix that runs one command line: ``[shell, "-c"]``.

    Raises :class:`ShellNotFound` on Windows when Git Bash cannot be found,
    or when ``LUNA_GIT_BASH_PATH`` names a file that is missing or a WSL stub.
    """
    windows = IS_WINDOWS if windows is None else windows
    if not windows:
        return ["/bin/sh", "-c"]
    env = os.environ if env is None else env
    explicit = env.get(GIT_BASH_ENV)
    if explicit:
        if exists(explicit) and not _is_wsl_launcher(explicit, env):
            return [explicit, "-c"]
        raise ShellNotFound(f"{GIT_BASH_ENV}={explicit}: файл не найден или это не Git Bash")
    for candidate in git_bash_candidates(env, which):
        if exists(candidate) and not _is_wsl_launcher(candidate, env):
            return [candidate, "-c"]
    raise ShellNotFound(INSTALL_HINT)


@functools.cache
def _default_shell() -> tuple[str, ...]:
    return tuple(find_shell())


def reset_cache() -> None:
    """Forget the cached shell (after ``LUNA_GIT_BASH_PATH`` or ``PATH`` changed)."""
    _default_shell.cache_clear()


def shell_argv(command: str) -> list[str]:
    """Full argv that runs ``command`` with this machine's shell."""
    return [*_default_shell(), command]


def describe_shell() -> str:
    """One line for the system prompt and ``luna doctor``."""
    if not IS_WINDOWS:
        return "sh (POSIX)"
    try:
        path = find_shell()[0]
    except ShellNotFound:
        return "нет (Git Bash не найден)"
    return f"Git Bash ({path}) — use POSIX shell syntax and forward slashes in paths"


def popen_kwargs() -> dict:
    """Start a child in its own process group so :func:`kill_tree` can stop all of it."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(pid: int) -> None:
    """Kill ``pid`` and everything it started; a process that is already gone is fine."""
    if IS_WINDOWS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        return
    # started with start_new_session=True, so the group id is the pid itself
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, signal.SIGKILL)


def run_sync(
    command: str,
    *,
    cwd: str | os.PathLike,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command line, capture UTF-8 output, kill the whole tree on timeout."""
    argv = shell_argv(command)
    with subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs(),
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=5)
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def portable_git_root(env: Mapping[str, str] | None = None) -> Path | None:
    """``%LOCALAPPDATA%\\luna\\git`` when the installer put PortableGit there."""
    env = os.environ if env is None else env
    local = env.get("LOCALAPPDATA")
    if not local:
        return None
    root = Path(local) / "luna" / "git"
    return root if (root / "cmd" / "git.exe").is_file() else None


def ensure_git_on_path(env: MutableMapping[str, str] | None = None) -> bool:
    """On Windows, put Luna's PortableGit on ``PATH`` when no other ``git`` is there.

    Returns True when ``PATH`` was changed. Undo and ``execute`` need ``git``;
    the installer does not touch the user's ``PATH`` for it.
    """
    if not IS_WINDOWS:
        return False
    env = os.environ if env is None else env
    if shutil.which("git", path=env.get("PATH")):
        return False
    root = portable_git_root(env)
    if root is None:
        return False
    env["PATH"] = os.pathsep.join([str(root / "cmd"), env.get("PATH", "")])
    reset_cache()
    return True
```

- [ ] **Step 4: Запустить тесты**

Run: `uv run pytest packages/luna-core/tests/test_shell.py -q -o addopts=""`
Expected: PASS (все тесты).

- [ ] **Step 5: Lint и коммит**

```bash
uv run ruff check packages/luna-core && uv run ruff format packages/luna-core
git add packages/luna-core/src/luna_core/shell.py packages/luna-core/tests/test_shell.py
git commit -m "feat(core): luna_core.shell — one shell for every OS (Git Bash on Windows)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `execute` и хуки через `shell.py`

**Files:**
- Modify: `packages/luna-core/src/luna_core/sandbox.py:85-175` (`_run_process`, `_kill_group`, `LocalSandbox.run`)
- Modify: `packages/luna-core/src/luna_core/hooks.py:126-170` (`CommandHook.__call__`, `_kill_group`)
- Modify: `packages/luna-core/tests/test_sandbox.py`, `packages/luna-core/tests/test_hooks.py`

**Interfaces:**
- Consumes: `shell.shell_argv`, `shell.popen_kwargs`, `shell.kill_tree`, `shell.ShellNotFound` (Task 1).
- Produces: `LocalSandbox.run` без Git Bash на Windows возвращает `RunResult(127, <INSTALL_HINT>)`. `execute` превращает это в неуспешный результат инструмента без изменений в `tools/shell.py`. `DockerSandbox` не меняется: он сам запускает `docker` через argv.

- [ ] **Step 1: Тесты**

Добавить в `packages/luna-core/tests/test_sandbox.py`:

```python
from luna_core import shell


async def test_local_without_a_shell_reports_how_to_fix_it(tmp_path, monkeypatch):
    def missing(command):
        raise shell.ShellNotFound("нужен Git Bash: https://git-scm.com/download/win")

    monkeypatch.setattr("luna_core.sandbox.shell_argv", missing)
    r = await LocalSandbox().run("echo hi", cwd=tmp_path, timeout=10)
    assert r.exit_code == 127 and "git-scm.com" in r.output


async def test_local_cwd_with_spaces_and_cyrillic(tmp_path):
    cwd = tmp_path / "Мой проект"
    cwd.mkdir()
    r = await LocalSandbox().run("echo привет && pwd", cwd=cwd, timeout=30)
    assert r.exit_code == 0 and "привет" in r.output and "проект" in r.output
```

В том же файле сделать существующие тесты переносимыми. Путь внутри bash-строки всегда пишется через `as_posix()`:

```python
async def test_local_timeout_kills_the_process_group(tmp_path):
    marker = (tmp_path / "late").as_posix()
    started = time.monotonic()
    r = await LocalSandbox().run(f"(sleep 3; touch '{marker}') & sleep 30", cwd=tmp_path, timeout=0.5)
    assert r.timed_out and time.monotonic() - started < 5
    await asyncio.sleep(3.5)
    assert not (tmp_path / "late").exists()  # the background child died with the group
```

То же правило в `test_hooks.py::test_command_hook_timeout_kills_whole_process_group`: `marker = (tmp_path / "marker").as_posix()`, внутри команды `touch '{marker}'`, проверка через `(tmp_path / "marker").exists()`. Добавить тест:

```python
async def test_command_hook_without_a_shell_is_a_notice(tmp_path, monkeypatch):
    from luna_core import shell

    def missing(command):
        raise shell.ShellNotFound("нужен Git Bash")

    monkeypatch.setattr("luna_core.hooks.shell_argv", missing)
    result = await CommandHook("echo hi", cwd=tmp_path, timeout=5)({"tool": "x", "args": {}})
    assert not result.block and "нужен Git Bash" in result.notices[0]
```

- [ ] **Step 2: Убедиться, что новые тесты падают**

Run: `uv run pytest packages/luna-core/tests/test_sandbox.py packages/luna-core/tests/test_hooks.py -q -o addopts=""`
Expected: FAIL: `AttributeError: ... has no attribute 'shell_argv'`.

- [ ] **Step 3: Реализация `sandbox.py`**

Импорты: убрать `signal`, добавить `from luna_core.shell import ShellNotFound, kill_tree, popen_kwargs, shell_argv`. Заменить начало `_run_process` и `_kill_group`:

```python
async def _run_process(
    argv_or_cmd,
    *,
    shell: bool,
    cwd: Path | None,
    timeout: float,
    env: dict | None,
    cancel: asyncio.Event | None,
    limit: int,
) -> RunResult:
    argv = shell_argv(argv_or_cmd) if shell else list(argv_or_cmd)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=str(cwd) if cwd else None,
        env=env,
        **popen_kwargs(),
    )
    # ... the rest of the function is unchanged ...


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    kill_tree(proc.pid)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
```

Вывод уже декодируется в `_cap(data, ...)`. Проверить, что там `data.decode("utf-8", errors="replace")`, и если кодировка не указана, указать её.

`LocalSandbox.run` (докстринг «Run ``command`` with this machine's shell (sh / Git Bash)»):

```python
        full_env = scrub_env({**os.environ, **(env or {})}, passthrough=self.env_passthrough)
        try:
            return await _run_process(
                command,
                shell=True,
                cwd=cwd,
                timeout=timeout,
                env=full_env,
                cancel=cancel,
                limit=self.max_output,
            )
        except ShellNotFound as exc:
            return RunResult(127, str(exc))
```

- [ ] **Step 4: Реализация `hooks.py`**

Импорты: убрать `signal`, добавить `from luna_core.shell import ShellNotFound, kill_tree, popen_kwargs, shell_argv`. В `CommandHook.__call__`:

```python
        try:
            argv = shell_argv(self.command)
        except ShellNotFound as exc:
            return HookResult(notices=(f"hook `{self.command}` not run: {exc}",))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrub_env(os.environ, passthrough=self.env_passthrough),
            **popen_kwargs(),
        )
```

`_kill_group(pid)` → тело `kill_tree(pid)`, докстринг «Kill ``pid`` and every process it started.».

- [ ] **Step 5: Тесты**

Run: `uv run pytest packages/luna-core/tests -q -o addopts=""`
Expected: PASS (Docker-тесты пропускаются, как раньше).

- [ ] **Step 6: Коммит**

```bash
git add packages/luna-core/src/luna_core/sandbox.py packages/luna-core/src/luna_core/hooks.py packages/luna-core/tests/test_sandbox.py packages/luna-core/tests/test_hooks.py
git commit -m "feat(core): execute and command hooks run through luna_core.shell

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: fmt / diagnose / verify / `!cmd` через `shell.run_sync`

**Files:**
- Modify: `luna/turn/fmt.py:36-58`, `luna/turn/diagnose.py:37-70`, `luna/turn/verify.py:15-43`, `luna/repl/usercmd.py:62-70`
- Test: `tests/test_turn_shell.py` (новый)

**Interfaces:**
- Consumes: `luna_core.shell.run_sync`, `ShellNotFound` (Task 1).
- Produces: прежние сигнатуры `fmt.run`, `diagnose.run`, `run_verify`, `usercmd._run_shell`. Без оболочки: `fmt.run` → `[]`, `diagnose.run` → `""`, `run_verify` → `(False, <hint>)`, `!cmd` → `""`.

- [ ] **Step 1: Тесты**

```python
# tests/test_turn_shell.py
import pytest
from luna.repl import usercmd
from luna.turn import diagnose, fmt, verify
from luna_core import shell


@pytest.fixture
def no_shell(monkeypatch):
    def missing(command, **kw):
        raise shell.ShellNotFound("нужен Git Bash: https://git-scm.com/download/win")

    for mod in (fmt, diagnose, verify, usercmd):
        monkeypatch.setattr(mod, "run_sync", missing)


def test_verify_runs_through_the_shell_and_reads_utf8(tmp_path):
    ok, tail = verify.run_verify("echo привет && exit 0", str(tmp_path))
    assert ok and "привет" in tail


def test_verify_without_shell_fails_with_hint(tmp_path, no_shell):
    ok, tail = verify.run_verify("echo hi", str(tmp_path))
    assert not ok and "git-scm.com" in tail


def test_fmt_and_diagnose_degrade_without_shell(tmp_path, no_shell, monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda name: "/x/" + name)
    assert fmt.run("ruff format", str(tmp_path), ["a.py"]) == []
    assert diagnose.run("ruff check", str(tmp_path), ["a.py"]) == ""


def test_usercmd_shell_injection_uses_the_shell(tmp_path):
    cmd = usercmd.UserCommand(name="x", description="", body="v=!`echo привет`")
    assert "привет" in usercmd.expand(cmd, "", str(tmp_path))
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_turn_shell.py -q -o addopts=""`
Expected: FAIL: `AttributeError: module 'luna.turn.fmt' has no attribute 'run_sync'`.

- [ ] **Step 3: Реализация**

Во всех четырёх модулях: `from luna_core.shell import ShellNotFound, run_sync`. Ловить `ShellNotFound` рядом с `OSError`.

`verify.py`:

```python
    try:
        proc = run_sync(
            command,
            cwd=workdir,
            timeout=_TIMEOUT,
            env=scrub_env(os.environ, passthrough=pass_env),
        )
    except subprocess.TimeoutExpired:
        return False, f"verify command timed out after {_TIMEOUT}s"
    except ShellNotFound as exc:
        return False, str(exc)
    except OSError as exc:
        return False, f"could not run verify command: {exc}"
```

`fmt.py`:

```python
    try:
        result = run_sync(
            full, cwd=workdir, timeout=_TIMEOUT, env=scrub_env(os.environ, passthrough=pass_env)
        )
    except (OSError, subprocess.SubprocessError, ShellNotFound):
        return []
```

`diagnose.py`: тот же вызов вместо `subprocess.run(... shell=True ...)`, `except (OSError, subprocess.SubprocessError, ShellNotFound): return ""`.

`usercmd.py` (`cwd` берётся из `expand`; если `_run_shell` вызывается без него, передать `workdir` через `functools.partial` в `_SHELL_RE.sub`):

```python
def _run_shell(match: re.Match, workdir: str) -> str:
    try:
        proc = run_sync(match.group(1), cwd=workdir, timeout=_SHELL_TIMEOUT)
    except (OSError, subprocess.SubprocessError, ShellNotFound):
        return ""
    return (proc.stdout or "").strip()[:_SHELL_CAP]
```

В `expand`: `text = _SHELL_RE.sub(functools.partial(_run_shell, workdir=workdir), text)`.

`shlex.quote` в `fmt.py`/`diagnose.py` остаётся: команду теперь всегда исполняет sh/bash.

- [ ] **Step 4: Тесты**

Run: `uv run pytest tests -q -o addopts="" -k "turn_shell or verify or fmt or diagnose or usercmd"`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add luna/turn/fmt.py luna/turn/diagnose.py luna/turn/verify.py luna/repl/usercmd.py tests/test_turn_shell.py
git commit -m "feat: formatters, diagnostics, verify and !cmd use luna_core.shell

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Правила `execute` — `.exe`, регистр, Windows-запускатели

**Files:**
- Modify: `packages/luna-core/src/luna_core/shellsplit.py` (`_variants` ~186, `_normalise` ~232, `_payload` ~272, новая `program_name`)
- Modify: `packages/luna-core/src/luna_core/policy.py:250-283` (`RUNNER_PROGRAMS`, `check_rule_safe`)
- Test: `packages/luna-core/tests/test_policy_windows.py` (новый)

**Interfaces:**
- Consumes: `luna_core.shell.IS_WINDOWS` (Task 1), читается в момент вызова через `from luna_core import shell as _shell` и `_shell.IS_WINDOWS`.
- Produces: `shellsplit.program_name(word: str) -> str` — путь отброшен; на Windows ещё нижний регистр без `.exe`, `.cmd`, `.bat`, `.com`. `shellsplit.allow_form(word: str) -> str` — путь сохранён; на Windows нижний регистр без расширения; на POSIX слово без изменений.

- [ ] **Step 1: Тесты**

```python
# packages/luna-core/tests/test_policy_windows.py
import pytest
from luna_core import shell
from luna_core.policy import Rules, UnsafeRule, check_rule_safe, suggest_rules
from luna_core.shellsplit import program_name


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(shell, "IS_WINDOWS", True)


DENY_RM = Rules(deny=["execute:rm *"], allow=["execute:ls *", "execute:git *"])


@pytest.mark.parametrize("command", [
    "RM -rf x",
    "rm.exe -rf x",
    "Rm.EXE -rf x",
    "C:/Git/usr/bin/rm.exe -rf x",
    "/usr/bin/rm -rf x",
    "ENV.EXE rm -rf x",
    'BASH.EXE -c "rm -rf x"',
    "ls && RM.exe x",
])
def test_deny_rm_cannot_be_bypassed_on_windows(windows, command):
    assert DENY_RM.match("execute", {"command": command}) == "deny"


def test_posix_keeps_case_sensitive_program_names():
    # on Linux/macOS `RM` is a different program; only the path is stripped there
    assert program_name("/usr/bin/rm") == "rm"
    assert program_name("RM") == "RM"


def test_program_name_on_windows(windows):
    assert program_name("C:/Git/usr/bin/RM.EXE") == "rm"
    assert program_name("npm.cmd") == "npm"


def test_allow_keeps_the_path_so_a_local_script_is_not_the_real_tool(windows):
    assert DENY_RM.match("execute", {"command": "./ls.exe -la"}) is None
    assert DENY_RM.match("execute", {"command": "GIT.exe status"}) == "allow"


@pytest.mark.parametrize("runner", ["cmd", "cmd.exe", "powershell", "pwsh", "wsl", "start",
                                    "cygstart", "winpty"])
def test_windows_runners_are_never_allowed(windows, runner):
    with pytest.raises(UnsafeRule):
        check_rule_safe(f"execute:{runner} *")
    assert suggest_rules("execute", {"command": f"{runner} /c del x"}) == ()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest packages/luna-core/tests/test_policy_windows.py -q -o addopts=""`
Expected: FAIL: `ImportError: cannot import name 'program_name'`.

- [ ] **Step 3: Реализация `shellsplit.py`**

```python
from luna_core import shell as _shell

_WIN_EXTENSIONS = frozenset({"exe", "cmd", "bat", "com"})


def _strip_windows(name: str) -> str:
    name = name.lower()
    stem, dot, ext = name.rpartition(".")
    return stem if dot and stem and ext in _WIN_EXTENSIONS else name


def program_name(word: str) -> str:
    """Basename of a program word; on Windows also lower-cased without ``.exe`` & co.

    Windows finds ``rm.exe`` for ``RM``, ``Rm.EXE`` or ``C:/Git/usr/bin/rm``, so a
    deny rule on ``rm`` must see all of them as ``rm``.
    """
    name = word.rsplit("/", 1)[-1]
    return _strip_windows(name) if _shell.IS_WINDOWS else name


def allow_form(word: str) -> str:
    """The program word as an allow rule must match it: path kept, Windows-normalised."""
    if not _shell.IS_WINDOWS:
        return word
    head, sep, tail = word.rpartition("/")
    return f"{head}{sep}{_strip_windows(tail)}"
```

`_variants`:

```python
def _variants(words: list[str]) -> list[str]:
    """Return the joined command, plus a form with the program cut to :func:`program_name`."""
    if not words:
        return []
    out = [" ".join(words)]
    base = program_name(words[0])
    if base != words[0]:
        out.append(" ".join([base, *words[1:]]))
    return out
```

`_normalise`: условие префикса `if words and program_name(words[0]) in _PREFIXES:` и `prefix = program_name(words.pop(0))`. Проверка `_LEADING` остаётся по исходному слову: ключевые слова оболочки чувствительны к регистру и в bash.

`_payload`: `program = program_name(words[0])`.

`_collect`: команды для allow-проверки в Windows-форме:

```python
        normalised = " ".join([allow_form(words[0]), *words[1:]])
```

- [ ] **Step 4: Реализация `policy.py`**

Добавить в `RUNNER_PROGRAMS`: `"cmd", "powershell", "pwsh", "wsl", "start", "cygstart", "winpty"`. В `check_rule_safe`:

```python
from luna_core.shellsplit import program_name, rough_split, split_command
...
    if program_name(program) in RUNNER_PROGRAMS:
        raise UnsafeRule(f"refusing to save {rule!r}: {program!r} can run any command")
```

На POSIX `cmd.exe` → `program_name` = `cmd.exe`, но `_PLAIN_PROGRAM` его пропускает. Поэтому в `RUNNER_PROGRAMS` кладём и `"cmd.exe"`, чтобы правило отвергалось на любой ОС.

- [ ] **Step 5: Все тесты правил**

Run: `uv run pytest packages/luna-core/tests/test_policy.py packages/luna-core/tests/test_policy_windows.py packages/luna-core/tests/test_shellsplit*.py -q -o addopts=""`
Expected: PASS. Прежние POSIX-тесты не меняются.

- [ ] **Step 6: Коммит**

```bash
git add packages/luna-core/src/luna_core/shellsplit.py packages/luna-core/src/luna_core/policy.py packages/luna-core/tests/test_policy_windows.py
git commit -m "fix(policy): .exe, case and path variants cannot bypass execute rules on Windows

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Регистр путей в правилах для файлов

**Files:**
- Modify: `packages/luna-core/src/luna_core/policy.py` (`Rules` dataclass ~54-73, `Policy.__init__` ~475-492, новая `case_insensitive_fs`)
- Test: `packages/luna-core/tests/test_policy_case.py` (новый)

**Interfaces:**
- Consumes: ничего нового.
- Produces: поле `Rules.fold_case: bool = False`. Функция `case_insensitive_fs(root: Path) -> bool`. `Policy(...)` с `workspace` выставляет `rules.fold_case = True`, если ФС проекта не различает регистр.

- [ ] **Step 1: Тесты (сначала подтвердить баг)**

```python
# packages/luna-core/tests/test_policy_case.py
import pytest
from luna_core.policy import Policy, Rules, case_insensitive_fs
from luna_core.tools.files import read_file
from luna_core.workspace import Workspace


def test_fold_case_matches_paths_without_case():
    rules = Rules(deny=["read_file:.env"], fold_case=True)
    assert rules.match("read_file", {"file_path": ".ENV"}) == "deny"
    assert Rules(deny=["read_file:.env"]).match("read_file", {"file_path": ".ENV"}) is None


def test_fold_case_never_touches_execute():
    rules = Rules(deny=["execute:rm *"], fold_case=True)
    assert rules.match("execute", {"command": "RM x"}) is None  # Task 4 handles Windows


def test_case_insensitive_fs_probe_is_consistent(tmp_path):
    (tmp_path / "Probe.txt").write_text("x", encoding="utf-8")
    assert case_insensitive_fs(tmp_path) == (tmp_path / "PROBE.TXT").exists()


def test_deny_on_dot_env_holds_on_a_case_insensitive_project(tmp_path):
    if not case_insensitive_fs(tmp_path):
        pytest.skip("this filesystem tells .ENV and .env apart")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    policy = Policy(Rules(deny=["read_file:.env"]), workspace=Workspace(tmp_path))
    assert policy.check(read_file, {"file_path": ".ENV"}).action == "deny"
```

- [ ] **Step 2: Запустить и записать результат**

Run: `uv run pytest packages/luna-core/tests/test_policy_case.py -q -o addopts=""`
Expected on macOS (APFS без учёта регистра): FAIL `test_deny_on_dot_env_holds_on_a_case_insensitive_project`, verdict `"ask"`/`"allow"`, а не `"deny"`. Это подтверждает обход. Если тест прошёл, обхода нет: записать это в заметку и в сообщение коммита, правку из Step 3 оставить для Windows. Остальные тесты падают из-за `ImportError`.

- [ ] **Step 3: Реализация**

```python
import contextlib
import tempfile


def case_insensitive_fs(root: Path) -> bool:
    """True when ``root``'s filesystem ignores letter case (macOS / Windows defaults).

    Probes with a temporary file; when the probe itself fails, answers True —
    deny rules then match more, never less.
    """
    try:
        with tempfile.NamedTemporaryFile(prefix=".luna-CaseProbe-", dir=root) as handle:
            probe = Path(handle.name)
            return (probe.parent / probe.name.swapcase()).exists()
    except OSError:
        return True
```

`Rules`: поле `fold_case: bool = False`. В `_hit` для не-`execute`:

```python
            if tool != "execute":
                pattern = _relpath(pattern)
                if self.fold_case:
                    if fnmatchcase(subject.casefold(), pattern.casefold()):
                        return True
                    continue
            if fnmatch(subject, pattern):
                return True
```

Импорт: `from fnmatch import fnmatch, fnmatchcase`. В `Policy.__init__` после `self.rules = rules or Rules()`:

```python
        if workspace is not None and case_insensitive_fs(workspace.root):
            self.rules.fold_case = True
```

`Rules.load(...)` не трогаем: значение по умолчанию `False`.

- [ ] **Step 4: Тесты**

Run: `uv run pytest packages/luna-core/tests -q -o addopts="" -k policy`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add packages/luna-core/src/luna_core/policy.py packages/luna-core/tests/test_policy_case.py
git commit -m "fix(policy): path rules ignore case on case-insensitive filesystems

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Сохранение переводов строк в `edit_file` / `write_file`

**Files:**
- Modify: `packages/luna-core/src/luna_core/tools/files.py:113-176`
- Test: `packages/luna-core/tests/test_file_tools_eol.py` (новый)

**Interfaces:**
- Consumes: ничего.
- Produces: прежние сигнатуры инструментов. Новые приватные `_to_crlf(text: str) -> str` и `_uses_crlf(data: bytes) -> bool`.

- [ ] **Step 1: Тесты**

```python
# packages/luna-core/tests/test_file_tools_eol.py
import pytest
from luna_core.tools.base import ToolContext
from luna_core.tools.files import edit_file, read_file, write_file
from luna_core.workspace import Workspace


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=Workspace(tmp_path))


async def call(tool, ctx, **args):
    return await tool.invoke(ctx, args)


async def test_edit_keeps_crlf(ctx, tmp_path):
    (tmp_path / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    await call(read_file, ctx, file_path="w.txt")
    out = await call(edit_file, ctx, file_path="w.txt", old_string="one\ntwo", new_string="1\n2")
    assert not out.is_error
    assert (tmp_path / "w.txt").read_bytes() == b"1\r\n2\r\nthree\r\n"


async def test_edit_keeps_lf(ctx, tmp_path):
    (tmp_path / "u.txt").write_bytes(b"one\ntwo\n")
    await call(read_file, ctx, file_path="u.txt")
    await call(edit_file, ctx, file_path="u.txt", old_string="two", new_string="2\n3")
    assert (tmp_path / "u.txt").read_bytes() == b"one\n2\n3\n"


async def test_edit_mixed_endings_finds_the_lf_part(ctx, tmp_path):
    (tmp_path / "m.txt").write_bytes(b"a\r\nb\nc\n")
    await call(read_file, ctx, file_path="m.txt")
    out = await call(edit_file, ctx, file_path="m.txt", old_string="b\nc", new_string="B\nC")
    assert not out.is_error
    assert (tmp_path / "m.txt").read_bytes() == b"a\r\nB\nC\n"


async def test_overwrite_keeps_crlf_and_new_file_is_lf(ctx, tmp_path):
    (tmp_path / "w.txt").write_bytes(b"x\r\n")
    await call(read_file, ctx, file_path="w.txt")
    await call(write_file, ctx, file_path="w.txt", content="y\nz\n")
    assert (tmp_path / "w.txt").read_bytes() == b"y\r\nz\r\n"
    await call(write_file, ctx, file_path="new.txt", content="a\nb\n")
    assert (tmp_path / "new.txt").read_bytes() == b"a\nb\n"


async def test_cyrillic_round_trip(ctx, tmp_path):
    await call(write_file, ctx, file_path="r.txt", content="привет\n")
    await call(edit_file, ctx, file_path="r.txt", old_string="привет", new_string="мир")
    assert (tmp_path / "r.txt").read_bytes() == "мир\n".encode()
```

Если у `ToolOutput` нет `.is_error`, взять имя флага ошибки из `luna_core/tools/base.py` (поле, которое `ToolOutput.error(...)` выставляет в `True`) и использовать его.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest packages/luna-core/tests/test_file_tools_eol.py -q -o addopts=""`
Expected: FAIL `test_edit_keeps_crlf` и `test_overwrite_keeps_crlf...` (файл стал LF).

- [ ] **Step 3: Реализация**

```python
def _uses_crlf(data: bytes) -> bool:
    """True when the file's line breaks are Windows-style."""
    return b"\r\n" in data


def _to_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")
```

`write_file.run()`:

```python
    def run() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        body = content
        if target.is_file() and _uses_crlf(target.read_bytes()):
            body = _to_crlf(content)
        target.write_bytes(body.encode("utf-8"))
```

`edit_file`, вместо `read_text`/`write_text`:

```python
    data = await asyncio.to_thread(target.read_bytes)
    text = data.decode("utf-8")
    old, new = old_string, new_string
    if _uses_crlf(data) and "\n" in old_string and text.count(_to_crlf(old_string)):
        old, new = _to_crlf(old_string), _to_crlf(new_string)
    count = text.count(old) if old else 0
    ...
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    await asyncio.to_thread(target.write_bytes, updated.encode("utf-8"))
```

`UnicodeDecodeError` ведёт себя как раньше: `read_text(encoding="utf-8")` поднимал ту же ошибку.

- [ ] **Step 4: Тесты**

Run: `uv run pytest packages/luna-core/tests -q -o addopts="" -k "file_tools or anchor"`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add packages/luna-core/src/luna_core/tools/files.py packages/luna-core/tests/test_file_tools_eol.py
git commit -m "fix(tools): edit_file and write_file keep the file's CRLF/LF line endings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Явная кодировка везде + страж от регрессий

**Files:**
- Create: `tests/test_portability.py`
- Modify: `luna/config/config.py:418,436`, `luna/config/model_discovery.py:29`, `luna/config/usage.py:43`, `luna/repl/usercmd.py:53`, `luna/server/auth.py:45`, `luna/extensions/lspnav.py:73,188`, `luna/core/session.py:266`, `luna/turn/gitinfo.py:11,27`, `packages/luna-core/src/luna_core/undo.py:51,127,151,162`, `packages/luna-core/src/luna_core/subagents.py:204`, `packages/luna-core/src/luna_core/skills.py:190`

**Interfaces:**
- Consumes: ничего.
- Produces: AST-тест, который падает на любом текстовом I/O без `encoding` в `luna/` и `packages/luna-core/src/`. Правило ruff `PLW1514` не используем: проверено 2026-11-11, оно ловит только `open()`, а `Path.read_text`/`write_text` пропускает. Отличие от спецификации записать в неё же.

- [ ] **Step 1: Страж**

```python
# tests/test_portability.py
"""Text I/O in Luna must name its encoding: Windows defaults to the ANSI code page."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [ROOT / "luna", ROOT / "packages" / "luna-core" / "src" / "luna_core"]
TEXT_METHODS = {"read_text", "write_text"}
SUBPROCESS = {"run", "Popen", "check_output", "check_call", "call"}


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        has_encoding = _kw(node, "encoding") is not None
        where = f"{path.relative_to(ROOT)}:{node.lineno}"
        if name in TEXT_METHODS and not has_encoding:
            positional = node.args[1:] if name == "write_text" else node.args
            if not positional:
                out.append(f"{where} {name} without encoding")
        if name == "open" and isinstance(func, ast.Name):
            mode = node.args[1] if len(node.args) > 1 else _kw(node, "mode")
            binary = isinstance(mode, ast.Constant) and "b" in str(mode.value)
            if not binary and not has_encoding:
                out.append(f"{where} open() without encoding")
        if name in SUBPROCESS and not has_encoding:
            if _is_true(_kw(node, "text")) or _is_true(_kw(node, "universal_newlines")):
                out.append(f"{where} subprocess text=True without encoding")
    return out


def test_no_text_io_without_encoding():
    offences = [o for src in SOURCES for p in sorted(src.rglob("*.py")) for o in _offences(p)]
    assert offences == []
```

- [ ] **Step 2: Запустить и увидеть список**

Run: `uv run pytest tests/test_portability.py -q -o addopts=""`
Expected: FAIL. В списке все места из «Files» выше (около 17 строк).

- [ ] **Step 3: Исправить каждое место**

- `read_text()` → `read_text(encoding="utf-8")`, `write_text(x)` → `write_text(x, encoding="utf-8")`.
- `resources.files(...).read_text()` → `.read_text(encoding="utf-8")`.
- `open(os.devnull, "w")` → `open(os.devnull, "w", encoding="utf-8")`.
- Вызовы `subprocess.run(..., text=True, ...)` для `git` и прочих получают `encoding="utf-8", errors="replace"`.

- [ ] **Step 4: Страж и полный набор**

Run: `uv run pytest -q -o addopts="-m 'not live'"`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add -A luna packages/luna-core/src tests/test_portability.py
git commit -m "fix: explicit UTF-8 for every text read/write and subprocess, with an AST guard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Папка конфига на Windows и права на приватные файлы

**Files:**
- Create: `packages/luna-core/src/luna_core/paths.py`, `packages/luna-core/src/luna_core/fsperm.py`
- Modify: `luna/config/config.py:47-52`, `luna/config/credentials.py:67-69`, `luna/server/run.py:113-116`, `packages/luna-core/src/luna_core/mcp.py:144-146,238-240`
- Modify (тесты прав): `tests/test_credentials.py`, `tests/test_server_credentials.py`, `tests/test_commands.py:714`, `tests/test_server_run.py:250-266`, `packages/luna-core/tests/test_mcp.py:133-156`
- Test: `packages/luna-core/tests/test_paths.py` (новый)

**Interfaces:**
- Produces:
  - `luna_core.paths.user_config_dir(env: Mapping[str, str] | None = None, *, windows: bool | None = None) -> Path`;
  - `luna_core.fsperm.restrict(fd: int) -> None`;
  - `luna_core.fsperm.POSIX_PERMISSIONS: bool` (`os.name != "nt"`) — для `skipif` в тестах.
- `luna.config.config.config_dir(env)` возвращает `user_config_dir(env)`.

- [ ] **Step 1: Тесты**

```python
# packages/luna-core/tests/test_paths.py
from pathlib import Path

from luna_core.paths import user_config_dir


def test_xdg_wins_everywhere():
    assert user_config_dir({"XDG_CONFIG_HOME": "/x"}, windows=True) == Path("/x") / "luna"


def test_windows_uses_appdata():
    env = {"APPDATA": r"C:\Users\u\AppData\Roaming"}
    assert user_config_dir(env, windows=True) == Path(env["APPDATA"]) / "luna"


def test_posix_default(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert user_config_dir({}, windows=False) == tmp_path / ".config" / "luna"
```

Во всех тестах, проверяющих `0o600`, добавить
`@pytest.mark.skipif(not POSIX_PERMISSIONS, reason="POSIX file modes")`
(`from luna_core.fsperm import POSIX_PERMISSIONS`).

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest packages/luna-core/tests/test_paths.py -q -o addopts=""`
Expected: FAIL `ModuleNotFoundError: luna_core.paths`.

- [ ] **Step 3: Реализация**

```python
# packages/luna-core/src/luna_core/paths.py
"""Where Luna keeps per-user files on each OS."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def user_config_dir(
    env: Mapping[str, str] | None = None, *, windows: bool | None = None
) -> Path:
    """``$XDG_CONFIG_HOME/luna``; else ``%APPDATA%\\luna`` on Windows; else ``~/.config/luna``."""
    env = os.environ if env is None else env
    windows = (os.name == "nt") if windows is None else windows
    if env.get("XDG_CONFIG_HOME"):
        return Path(env["XDG_CONFIG_HOME"]) / "luna"
    if windows and env.get("APPDATA"):
        return Path(env["APPDATA"]) / "luna"
    return Path.home() / ".config" / "luna"
```

```python
# packages/luna-core/src/luna_core/fsperm.py
"""Owner-only files: 0600 on POSIX; on Windows the user profile's ACL already is."""

from __future__ import annotations

import os

POSIX_PERMISSIONS = os.name != "nt"


def restrict(fd: int) -> None:
    """Make the open file readable and writable by its owner only (no-op on Windows)."""
    if POSIX_PERMISSIONS:
        os.fchmod(fd, 0o600)
```

- `config.py::config_dir`: `return user_config_dir(env)`, докстринг обновить.
- `credentials.py`: `os.fchmod(fd, _FILE_MODE)` → `restrict(fd)`; в модульном докстринге указать, что на Windows путь `%APPDATA%\luna`.
- `server/run.py`: `os.fchmod(fd, 0o600)` → `restrict(fd)`.
- `mcp.py`: `os.fchmod(fd, 0o600)` → `restrict(fd)`; `token_dir` по умолчанию `user_config_dir() / "mcp-tokens"`.
- `luna/ui/trust.py:110`: в тексте ошибки вместо жёсткого `~/.config/luna/sessions.db` подставлять реальный путь `config_dir() / "sessions.db"`.

- [ ] **Step 4: Тесты**

Run: `uv run pytest -q -o addopts="-m 'not live'"`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add -A packages/luna-core/src/luna_core/paths.py packages/luna-core/src/luna_core/fsperm.py luna packages/luna-core/src/luna_core/mcp.py tests packages/luna-core/tests
git commit -m "feat: %APPDATA%\\luna on Windows; owner-only files via fsperm.restrict

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Фоновый сервер на Windows

**Files:**
- Modify: `luna/server/run.py:36-41` (`_default_is_alive`), `66-70` (`_default_kill`), `117-127` (spawn)
- Test: `tests/test_server_run.py` (дописать)

**Interfaces:**
- Consumes: `luna_core.shell.kill_tree`, `luna_core.shell.IS_WINDOWS` (Task 1).
- Produces: `_windows_is_alive(pid: int) -> bool` (только Windows), `_spawn_kwargs() -> dict`.

- [ ] **Step 1: Тесты**

```python
import subprocess
import sys

from luna.server import run as server_run


def test_is_alive_true_for_self_and_false_for_a_finished_process():
    assert server_run._default_is_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not server_run._default_is_alive(proc.pid)


def test_is_alive_never_kills_the_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert server_run._default_is_alive(proc.pid)
        assert proc.poll() is None  # on Windows os.kill(pid, 0) would have terminated it
    finally:
        proc.kill()


def test_spawn_kwargs_detach_from_the_console(monkeypatch):
    from luna_core import shell

    monkeypatch.setattr(shell, "IS_WINDOWS", False)
    assert server_run._spawn_kwargs() == {"start_new_session": True}
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    flags = server_run._spawn_kwargs()["creationflags"]
    assert flags & 0x00000008 and flags & 0x00000200  # DETACHED_PROCESS, NEW_PROCESS_GROUP
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_server_run.py -q -o addopts=""`
Expected: FAIL `AttributeError: _spawn_kwargs`.

- [ ] **Step 3: Реализация**

```python
from luna_core import shell as _shell

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _windows_is_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _default_is_alive(pid: int) -> bool:
    if _shell.IS_WINDOWS:
        return _windows_is_alive(pid)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _default_kill(pid: int) -> None:
    _shell.kill_tree(pid)


def _spawn_kwargs() -> dict:
    """Detach the server so closing the launching terminal does not stop it."""
    if _shell.IS_WINDOWS:
        return {
            "creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        }
    return {"start_new_session": True}
```

В `subprocess.Popen(...)` заменить `start_new_session=True` на `**_spawn_kwargs()`. `signal` больше не нужен, импорт убрать.

`_default_kill` теперь шлёт SIGKILL группе, а не SIGTERM процессу. Сервер Luna без состояния в памяти, которое надо сбрасывать, поэтому это допустимо. Если тест в `test_server_run.py` ждёт именно SIGTERM, обновить его под `kill_tree`.

- [ ] **Step 4: Тесты**

Run: `uv run pytest tests/test_server_run.py tests/test_server_runtime.py -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add luna/server/run.py tests/test_server_run.py
git commit -m "fix(server): process liveness via WinAPI, detached spawn on Windows

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Запуск `luna` — UTF-8, Ctrl+C, PortableGit, строка оболочки в промпте

**Files:**
- Modify: `luna/cli.py:794-801` (`main`)
- Modify: `luna/core/session.py:85-106` (`_ctrl_c_cancels`)
- Modify: `packages/luna-core/src/luna_core/context.py:56-60`
- Test: `tests/test_startup_portability.py` (новый), `packages/luna-core/tests/test_context.py` (дописать)

**Interfaces:**
- Consumes: `shell.ensure_git_on_path`, `shell.describe_shell` (Task 1).
- Produces: `luna.cli._utf8_stdio(streams=None, *, windows=None) -> None`.

- [ ] **Step 1: Тесты**

```python
# tests/test_startup_portability.py
import asyncio
import signal

from luna import cli
from luna.core import session as session_mod


class _Stream:
    def __init__(self):
        self.calls = []

    def reconfigure(self, **kw):
        self.calls.append(kw)


def test_utf8_stdio_on_windows_only():
    out, err = _Stream(), _Stream()
    cli._utf8_stdio([out, err], windows=False)
    assert out.calls == []
    cli._utf8_stdio([out, err], windows=True)
    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}] == err.calls


async def test_ctrl_c_falls_back_to_a_plain_signal_handler(monkeypatch):
    loop = asyncio.get_running_loop()

    def unsupported(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", unsupported)
    hits = []
    previous = signal.getsignal(signal.SIGINT)
    with session_mod._ctrl_c_cancels(lambda: hits.append(1)):
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0.05)
    assert hits == [1]
    assert signal.getsignal(signal.SIGINT) is previous
```

В `packages/luna-core/tests/test_context.py`:

```python
def test_prompt_names_the_shell(tmp_path):
    from luna_core.context import build_system_prompt

    assert "Shell:" in build_system_prompt("base", tmp_path)
```

Имя функции промпта (`build_system_prompt`) сверить с `context.py:48-56` и при необходимости подставить реальное.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_startup_portability.py packages/luna-core/tests/test_context.py -q -o addopts=""`
Expected: FAIL (`_utf8_stdio` нет, Ctrl+C не вызывает обработчик, `Shell:` нет).

- [ ] **Step 3: Реализация**

`luna/cli.py`:

```python
def _utf8_stdio(streams=None, *, windows: bool | None = None) -> None:
    """On Windows, print UTF-8 even when redirected (the ANSI code page breaks Cyrillic)."""
    windows = (os.name == "nt") if windows is None else windows
    if not windows:
        return
    for stream in streams if streams is not None else (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    _utf8_stdio()
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw in (["--version"], ["-V"]):  # before any heavy import: this path must stay instant
        print(f"luna {__version__}")
        return 0
    from luna_core.shell import ensure_git_on_path

    ensure_git_on_path()
    return _main(raw)
```

Импорты `contextlib` и `os` в шапке `cli.py`, если их ещё нет.

`luna/core/session.py::_ctrl_c_cancels`:

```python
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    installed = plain = False
    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
        installed = True
    except (NotImplementedError, RuntimeError, ValueError):
        # Windows has no loop signal handlers: a plain handler hops back onto the loop
        try:
            signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(on_sigint))
            plain = True
        except ValueError:
            pass  # not the main thread
    try:
        yield
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)
        if installed or plain:
            with contextlib.suppress(TypeError, ValueError):
                signal.signal(signal.SIGINT, previous)
```

`context.py`:

```python
from luna_core.shell import describe_shell
...
    env = (
        f"# Environment\n\nWorking directory: {root}\n"
        f"Platform: {platform.system()} {platform.machine()}\n"
        f"Shell: {describe_shell()}\nDate: {today}"
    )
```

- [ ] **Step 4: Тесты**

Run: `uv run pytest -q -o addopts="-m 'not live'"`
Expected: PASS. Если тесты сравнивают промпт целиком (снапшоты), обновить их под новую строку `Shell:`.

- [ ] **Step 5: Коммит**

```bash
git add luna/cli.py luna/core/session.py packages/luna-core/src/luna_core/context.py tests/test_startup_portability.py packages/luna-core/tests/test_context.py
git commit -m "feat(cli): UTF-8 stdio and Ctrl+C on Windows, PortableGit on PATH, shell in prompt

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: `luna doctor` и `luna update`

**Files:**
- Create: `luna/doctor.py`
- Modify: `luna/cli.py` (`_SUBCOMMANDS`, `handlers`, справка)
- Test: `tests/test_doctor.py` (новый)

**Interfaces:**
- Consumes: `luna_core.shell.find_shell`, `ShellNotFound`, `describe_shell`; `luna.config.config.config_dir`; `luna.server.auth.read_token_file`; `luna.server.run._default_is_alive`.
- Produces:
  - `@dataclass(frozen=True) class Check: name: str; ok: bool; detail: str; hint: str = ""`;
  - `collect() -> list[Check]`;
  - `render(checks) -> str`;
  - `run_doctor(argv) -> int` (0 — всё ок, 1 — есть проблемы);
  - `run_update(argv, *, which=shutil.which, run=subprocess.run, popen=subprocess.Popen, windows=None) -> int`.

- [ ] **Step 1: Тесты**

```python
# tests/test_doctor.py
import subprocess
import sys

from luna import doctor
from luna_core import shell


def test_doctor_reports_each_area():
    names = {c.name for c in doctor.collect()}
    assert {"luna", "python", "path", "config", "shell", "git", "server"} <= names


def test_doctor_explains_a_missing_shell(monkeypatch):
    def missing(*a, **k):
        raise shell.ShellNotFound("нужен Git Bash: https://git-scm.com/download/win")

    monkeypatch.setattr(doctor, "find_shell", missing)
    check = next(c for c in doctor.collect() if c.name == "shell")
    assert not check.ok and "git-scm.com" in check.hint


def test_doctor_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "collect", lambda: [doctor.Check("x", False, "плохо", "сделай y")])
    assert doctor.run_doctor([]) == 1
    assert "сделай y" in capsys.readouterr().out


def _uv_dir(prefix):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, f"{prefix}\n", "")


def test_update_refuses_when_not_a_uv_tool(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    code = doctor.run_update([], which=lambda n: "/bin/uv", run=_uv_dir(tmp_path / "tools"),
                             windows=False)
    assert code == 1 and "uv tool" in capsys.readouterr().err


def test_update_runs_uv_tool_upgrade_on_posix(monkeypatch, tmp_path):
    tools = tmp_path / "tools"
    monkeypatch.setattr(sys, "prefix", str(tools / "luna-simple"))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{tools}\n" if "dir" in argv else "", "")

    assert doctor.run_update([], which=lambda n: "/bin/uv", run=run, windows=False) == 0
    assert calls[-1] == ["/bin/uv", "tool", "upgrade", "luna-simple"]


def test_update_on_windows_hands_off_to_a_new_console(monkeypatch, tmp_path):
    tools = tmp_path / "tools"
    monkeypatch.setattr(sys, "prefix", str(tools / "luna-simple"))
    started = []
    code = doctor.run_update(
        [], which=lambda n: r"C:\uv.exe", run=_uv_dir(tools),
        popen=lambda argv, **kw: started.append((argv, kw)), windows=True,
    )
    assert code == 0
    argv, kw = started[0]
    assert "upgrade" in " ".join(argv) and kw["creationflags"] & 0x00000010  # NEW_CONSOLE
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_doctor.py -q -o addopts=""`
Expected: FAIL `ModuleNotFoundError: luna.doctor`.

- [ ] **Step 3: Реализация**

```python
# luna/doctor.py
"""`luna doctor` (is this install healthy?) and `luna update` (upgrade via uv tool)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from luna_core.shell import IS_WINDOWS, ShellNotFound, describe_shell, find_shell

from luna import __version__

INSTALL_URL = "https://github.com/Dushe4ka/luna-simple#установка"
_CREATE_NEW_CONSOLE = 0x00000010


@dataclass(frozen=True)
class Check:
    """One line of the report."""

    name: str
    ok: bool
    detail: str
    hint: str = ""


def collect() -> list[Check]:
    """Run every check; never raises."""
    from luna.config.config import config_dir
    from luna.server.auth import read_token_file
    from luna.server.run import _default_is_alive

    checks = [
        Check("luna", True, f"{__version__} ({Path(sys.argv[0]).resolve()})"),
        Check("python", True, sys.version.split()[0]),
    ]
    on_path = shutil.which("luna")
    checks.append(
        Check("path", bool(on_path), on_path or "luna нет в PATH",
              "" if on_path else "выполните `uv tool update-shell` и откройте новый терминал")
    )
    cfg = config_dir()
    writable = os.access(cfg if cfg.exists() else cfg.parent, os.W_OK)
    checks.append(Check("config", writable, str(cfg), "" if writable else "нет прав на запись"))
    try:
        find_shell()
        checks.append(Check("shell", True, describe_shell()))
    except ShellNotFound as exc:
        checks.append(Check("shell", False, "оболочка для команд не найдена", str(exc)))
    git = shutil.which("git")
    checks.append(Check("git", bool(git), git or "git не найден",
                        "" if git else "нужен для /undo: https://git-scm.com/downloads"))
    token = read_token_file()
    alive = bool(token and _default_is_alive(token["pid"]))
    checks.append(Check("server", True, f"работает (pid {token['pid']})" if alive else "не запущен"))
    return checks


def render(checks: list[Check]) -> str:
    """Plain-text report, one check per line (hints indented below)."""
    lines = []
    for check in checks:
        lines.append(f"{'ок ' if check.ok else 'ОШ '} {check.name:<7} {check.detail}")
        if check.hint:
            lines.append(f"            → {check.hint}")
    return "\n".join(lines)


def run_doctor(argv: list[str]) -> int:
    """`luna doctor`."""
    checks = collect()
    print(render(checks))
    return 0 if all(c.ok for c in checks) else 1


def run_update(argv, *, which=shutil.which, run=subprocess.run, popen=subprocess.Popen,
               windows: bool | None = None) -> int:
    """`luna update`: `uv tool upgrade luna-simple`, only for a `uv tool` install."""
    windows = IS_WINDOWS if windows is None else windows
    uv = which("uv")
    if uv is None:
        print(f"luna: uv не найден — переустановите Luna: {INSTALL_URL}", file=sys.stderr)
        return 1
    tool_dir = run([uv, "tool", "dir"], capture_output=True, text=True, encoding="utf-8").stdout
    tool_dir = tool_dir.strip()
    if not tool_dir or not Path(sys.prefix).resolve().is_relative_to(Path(tool_dir).resolve()):
        print("luna: эта Luna установлена не через `uv tool` — обновите её тем же способом, "
              "каким ставили", file=sys.stderr)
        return 1
    if windows:
        # Windows locks the running luna.exe: upgrade in a new console after we exit.
        popen(["cmd", "/c", f'timeout /t 2 >nul & "{uv}" tool upgrade luna-simple & pause'],
              creationflags=_CREATE_NEW_CONSOLE)
        print("luna: обновление запущено в новом окне")
        return 0
    return run([uv, "tool", "upgrade", "luna-simple"]).returncode
```

`luna/cli.py`: в `_SUBCOMMANDS` добавить `"doctor"` и `"update"`; в `handlers`:

```python
            "doctor": _run_doctor,
            "update": _run_update,
```

```python
def _run_doctor(argv: list[str]) -> int:
    from luna.doctor import run_doctor

    return run_doctor(argv)


def _run_update(argv: list[str]) -> int:
    from luna.doctor import run_update

    return run_update(argv)
```

Добавить обе команды туда, где `cli.py` описывает подкоманды в `--help` (рядом с `serve`/`sessions`).

- [ ] **Step 4: Тесты**

Run: `uv run pytest tests/test_doctor.py tests/test_cli.py -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Проверить вручную на этой машине**

Run: `uv run luna doctor`
Expected: строки `ок` для luna, python, config, shell (`sh (POSIX)`), git. Строка `path` может быть `ОШ`, если `luna` из venv не в `PATH`: так и должно быть.

- [ ] **Step 6: Коммит**

```bash
git add luna/doctor.py luna/cli.py tests/test_doctor.py
git commit -m "feat(cli): luna doctor and luna update

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Установщики, README, синхронизация спецификации

**Files:**
- Create: `scripts/install.sh`, `scripts/install.ps1`
- Modify: `README.md` (раздел «Установка»), `docs/superpowers/specs/2026-11-11-luna-windows-design.md` (отличия), `CHANGELOG.md`, `AGENTS.md` (структура: `luna_core/shell.py`, `paths.py`, `fsperm.py`, `luna/doctor.py`, `scripts/`)
- Test: `tests/test_install_scripts.py` (новый, статические проверки)

**Interfaces:**
- Consumes: `luna doctor` (Task 11), поиск bash (Task 1, повторён на PowerShell).
- Produces: переменные окружения установщиков `LUNA_REF` (по умолчанию `main`), `LUNA_REPO` (по умолчанию `https://github.com/Dushe4ka/luna-simple`), `LUNA_NO_MODIFY_PATH=1`, `LUNA_NO_GIT=1` (только Windows).

- [ ] **Step 1: Статический тест**

```python
# tests/test_install_scripts.py
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PINS = {
    "PortableGit-2.56.0.4-64-bit.7z.exe":
        "b1f3e52216e2ed6882d1bebfd4d3fdf319bec40d2edbbf9fd26ec4fd3fce7ed1",
    "PortableGit-2.56.0.4-arm64.7z.exe":
        "044110f9d951858452ddb5e4b5415ea0f2b5b2598e794d6fec77fe28b9aa8cc0",
}


def test_install_sh_is_valid_posix_sh():
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh")
    assert subprocess.run([sh, "-n", str(SCRIPTS / "install.sh")]).returncode == 0


def test_both_installers_install_the_same_package():
    spec = 'luna-simple[all] @ git+'
    assert spec in (SCRIPTS / "install.sh").read_text(encoding="utf-8")
    assert spec in (SCRIPTS / "install.ps1").read_text(encoding="utf-8")


def test_portable_git_is_pinned_by_sha256():
    text = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    for name, digest in PINS.items():
        assert name in text and digest in text


def test_ps1_never_accepts_the_wsl_bash():
    text = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    assert re.search(r"WindowsApps", text) and "SystemRoot" in text
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/test_install_scripts.py -q -o addopts=""`
Expected: FAIL (файлов нет).

- [ ] **Step 3: `scripts/install.sh`**

```sh
#!/bin/sh
# Luna installer for macOS and Linux.
#   curl -LsSf https://raw.githubusercontent.com/Dushe4ka/luna-simple/main/scripts/install.sh | sh
# Options: --ref <branch|tag|sha>, --no-modify-path (or LUNA_REF, LUNA_NO_MODIFY_PATH=1).
set -eu

REPO="${LUNA_REPO:-https://github.com/Dushe4ka/luna-simple}"
REF="${LUNA_REF:-main}"
MODIFY_PATH=1
[ "${LUNA_NO_MODIFY_PATH:-0}" = "1" ] && MODIFY_PATH=0

say() { printf '%s\n' "luna-install: $*"; }
die() { printf '%s\n' "luna-install: ошибка: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) [ $# -ge 2 ] || die "--ref требует значение"; REF="$2"; shift 2 ;;
    --no-modify-path) MODIFY_PATH=0; shift ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) die "неизвестный параметр: $1" ;;
  esac
done

fetch() {
  if command -v curl >/dev/null 2>&1; then curl -LsSf "$1"
  elif command -v wget >/dev/null 2>&1; then wget -qO- "$1"
  else die "нужен curl или wget"; fi
}

if ! command -v uv >/dev/null 2>&1; then
  say "ставлю uv (менеджер Python от Astral)…"
  if [ "$MODIFY_PATH" = 1 ]; then fetch https://astral.sh/uv/install.sh | sh
  else fetch https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh; fi
  PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
  export PATH
  command -v uv >/dev/null 2>&1 || die "uv установлен, но не найден в PATH"
fi

command -v git >/dev/null 2>&1 || die "нужен git (uv ставит Luna из git-репозитория)"

say "ставлю Luna ($REF)…"
uv tool install --python 3.12 --force "luna-simple[all] @ git+$REPO@$REF"

if [ "$MODIFY_PATH" = 1 ]; then uv tool update-shell >/dev/null 2>&1 || true; fi

BIN="$(uv tool dir --bin)"
"$BIN/luna" doctor || say "doctor нашёл проблемы — подсказки выше"
say "готово. Откройте новый терминал и наберите: luna"
```

- [ ] **Step 4: `scripts/install.ps1`**

```powershell
# Luna installer for Windows.
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Dushe4ka/luna-simple/main/scripts/install.ps1 | iex"
# Environment: LUNA_REF, LUNA_REPO, LUNA_NO_MODIFY_PATH=1, LUNA_NO_GIT=1.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Repo = if ($env:LUNA_REPO) { $env:LUNA_REPO } else { 'https://github.com/Dushe4ka/luna-simple' }
$Ref = if ($env:LUNA_REF) { $env:LUNA_REF } else { 'main' }
$ModifyPath = $env:LUNA_NO_MODIFY_PATH -ne '1'
$WantGit = $env:LUNA_NO_GIT -ne '1'

function Say([string]$Text) { Write-Host "luna-install: $Text" }
function Die([string]$Text) { Write-Host "luna-install: ошибка: $Text" -ForegroundColor Red; exit 1 }

$GitPins = @{
  'X64'   = @{ Name = 'PortableGit-2.56.0.4-64-bit.7z.exe'
               Sha256 = 'b1f3e52216e2ed6882d1bebfd4d3fdf319bec40d2edbbf9fd26ec4fd3fce7ed1' }
  'Arm64' = @{ Name = 'PortableGit-2.56.0.4-arm64.7z.exe'
               Sha256 = '044110f9d951858452ddb5e4b5415ea0f2b5b2598e794d6fec77fe28b9aa8cc0' }
}
$GitRelease = 'https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4'
$LunaLocal = Join-Path $env:LOCALAPPDATA 'luna'
$PortableRoot = Join-Path $LunaLocal 'git'

function Test-NotWsl([string]$Path) {
  $p = $Path.ToLowerInvariant()
  $sys = ($env:SystemRoot).ToLowerInvariant() + '\'
  return -not ($p.StartsWith($sys) -or $p.Contains('\windowsapps\'))
}

function Find-GitBash {
  # Same order as luna_core.shell.git_bash_candidates.
  $c = @()
  if ($env:LUNA_GIT_BASH_PATH) { $c += $env:LUNA_GIT_BASH_PATH }
  $git = Get-Command git -ErrorAction SilentlyContinue
  if ($git) {
    $d = Split-Path -Parent $git.Source
    $c += (Join-Path (Split-Path -Parent $d) 'bin\bash.exe')
    $c += (Join-Path (Split-Path -Parent (Split-Path -Parent $d)) 'bin\bash.exe')
  }
  foreach ($v in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($v) { $c += (Join-Path $v 'Git\bin\bash.exe') }
  }
  $c += (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe')
  $c += (Join-Path $PortableRoot 'bin\bash.exe')
  foreach ($p in $c) { if ((Test-Path -LiteralPath $p -PathType Leaf) -and (Test-NotWsl $p)) { return $p } }
  return $null
}

function Install-PortableGit {
  $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
  $pin = $GitPins[$arch]
  if (-not $pin) { Die "PortableGit для архитектуры $arch нет — поставьте Git вручную: https://git-scm.com/download/win" }
  $tmp = Join-Path ([IO.Path]::GetTempPath()) "luna-git-$PID"
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  try {
    $sfx = Join-Path $tmp $pin.Name
    Say "скачиваю $($pin.Name)…"
    Invoke-WebRequest -Uri "$GitRelease/$($pin.Name)" -OutFile $sfx -UseBasicParsing
    $digest = (Get-FileHash -Path $sfx -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($digest -ne $pin.Sha256) { Die "контрольная сумма PortableGit не совпала ($digest)" }
    $staging = "$PortableRoot.staging-$PID"
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    Unblock-File -Path $sfx -ErrorAction SilentlyContinue
    # GUI self-extractor: Start + WaitForExit, `&` would not wait for it.
    $proc = [System.Diagnostics.Process]::Start($sfx, "-o`"$staging`" -y")
    if (-not $proc.WaitForExit(600000)) { taskkill.exe /T /F /PID $proc.Id | Out-Null; Die 'распаковка PortableGit зависла' }
    if ($proc.ExitCode) { Die "распаковка PortableGit завершилась с кодом $($proc.ExitCode)" }
    if (-not (Test-Path (Join-Path $staging 'bin\bash.exe'))) { Die 'в архиве PortableGit нет bin\bash.exe' }
    if (Test-Path -LiteralPath $PortableRoot) {
      attrib.exe -R "$PortableRoot\*" /S /D | Out-Null
      Remove-Item -LiteralPath $PortableRoot -Recurse -Force
    }
    [IO.Directory]::Move($staging, $PortableRoot)
  } finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# 1. uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say 'ставлю uv (менеджер Python от Astral)…'
  if (-not $ModifyPath) { $env:UV_NO_MODIFY_PATH = '1' }
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die 'uv установлен, но не найден в PATH' }
}

# 2. Git Bash: needed by `uv tool install git+…` and by Luna's execute/undo.
if (-not (Find-GitBash)) {
  if ($WantGit) {
    Say 'Git не найден — ставлю PortableGit в папку Luna (системный Git не трогается)'
    Install-PortableGit
  } else {
    Say 'Git не найден, LUNA_NO_GIT=1: команды агента и установка из git работать не будут'
  }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $PortableRoot 'cmd\git.exe'))) {
  $env:Path = "$(Join-Path $PortableRoot 'cmd');$env:Path"
}

# 3. Luna
Say "ставлю Luna ($Ref)…"
uv tool install --python 3.12 --force "luna-simple[all] @ git+$Repo@$Ref"
if ($LASTEXITCODE) { Die 'uv tool install завершился с ошибкой' }

if ($ModifyPath) { uv tool update-shell | Out-Null }

$Bin = (uv tool dir --bin).Trim()
& (Join-Path $Bin 'luna.exe') doctor
if ($LASTEXITCODE) { Say 'doctor нашёл проблемы — подсказки выше' }
Say 'готово. Откройте новый терминал и наберите: luna'
```

- [ ] **Step 5: README, CHANGELOG, AGENTS.md, спецификация**

Раздел «Установка» в README заменить на:

````markdown
## Установка

macOS и Linux:

```bash
curl -LsSf https://raw.githubusercontent.com/Dushe4ka/luna-simple/main/scripts/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Dushe4ka/luna-simple/main/scripts/install.ps1 | iex"
```

Установщик ставит [uv](https://docs.astral.sh/uv/) (он сам скачает Python 3.12), затем Luna
командой `uv tool install`. На Windows без Git он кладёт PortableGit в
`%LOCALAPPDATA%\luna\git`: Luna выполняет команды через Git Bash. После установки откройте
новый терминал и наберите `luna`.

- Проверка установки: `luna doctor`
- Обновление: `luna update`
- Удаление: `uv tool uninstall luna-simple`
- Конкретная версия: `LUNA_REF=<ветка|тег> …` перед командой установки
- Свой путь к Git Bash: переменная `LUNA_GIT_BASH_PATH`
- Терминалы на Windows: Windows Terminal или консоль Windows 10+
````

Старые строки `uv pip install "luna-simple[all]"` / `uv pip install luna-simple` удалить. Блок для разработки (`uv venv … -e`) оставить под заголовком «Разработка».

В спецификацию, раздел 4: «вместо ruff `PLW1514` — AST-страж `tests/test_portability.py`, потому что `PLW1514` не ловит `Path.read_text`/`write_text` (проверено)». Раздел 8: «PortableGit ставится до `uv tool install` (uv клонирует git-репозиторий системным `git`); Luna сама добавляет `%LOCALAPPDATA%\luna\git\cmd` в `PATH` процесса при старте (`shell.ensure_git_on_path`)»; закреплённая версия `v2.56.0.windows.4`.

CHANGELOG: раздел `## Unreleased`. Windows (Git Bash, PortableGit), установщики, `luna doctor`/`luna update`, сохранение CRLF, правила `execute` устойчивы к `.exe` и регистру, правила путей без учёта регистра на macOS/Windows.

- [ ] **Step 6: Тесты**

Run: `uv run pytest tests/test_install_scripts.py -q -o addopts="" && sh -n scripts/install.sh`
Expected: PASS.

- [ ] **Step 7: Локальная проверка install.sh в изоляции (без изменения PATH пользователя)**

Run:

```bash
S=$(mktemp -d) && UV_TOOL_DIR="$S/tools" UV_TOOL_BIN_DIR="$S/bin" LUNA_NO_MODIFY_PATH=1 LUNA_REF=feat/windows sh scripts/install.sh
```

Expected: до push ветки `feat/windows` на GitHub установка падает на `uv tool install` (ветки нет на remote). Это ожидаемо, полная проверка будет в CI (Task 13). После push в той же изоляции: `luna-install: готово`, вывод `luna doctor`.

- [ ] **Step 8: Коммит**

```bash
git add scripts/install.sh scripts/install.ps1 tests/test_install_scripts.py README.md CHANGELOG.md AGENTS.md docs/superpowers/specs/2026-11-11-luna-windows-design.md
git commit -m "feat: one-line installers (sh/PowerShell) with PortableGit on Windows

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: CI на трёх ОС + сквозной smoke через мок-LLM

**Files:**
- Create: `scripts/ci/mock_llm.py`, `scripts/ci/smoke.py`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_ci_scripts.py` (локальный прогон мока)

**Interfaces:**
- Consumes: `luna --json`, `luna config path`, `luna serve --port --token`, `luna doctor`, кастомный провайдер `[provider.custom.mock]`.
- Produces:
  - `mock_llm.serve(port: int = 0) -> tuple[ThreadingHTTPServer, int]`; запуск `python scripts/ci/mock_llm.py --port-file <path>`;
  - `smoke.py` завершается с кодом 0 или печатает, что сломалось, и выходит с 1.

- [ ] **Step 1: Мок OpenAI-совместимого сервера**

```python
# scripts/ci/mock_llm.py
"""A tiny OpenAI-compatible chat server for CI: one execute call, then a text answer.

Streams the shapes ``luna_core.providers.openai_chat`` parses: ``choices[].delta``
with ``tool_calls`` / ``content``, a ``finish_reason``, a ``usage`` chunk, ``[DONE]``.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COMMAND = "echo привет && pwd"


def _chunks(messages: list[dict]) -> list[dict]:
    tool_results = [m for m in messages if m.get("role") == "tool"]
    if not tool_results:
        args = json.dumps({"command": COMMAND}, ensure_ascii=False)
        return [
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "execute", "arguments": args}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ]
    content = tool_results[-1].get("content")
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return [
        {"choices": [{"index": 0, "delta": {"content": f"Готово. Вывод: {content}"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 7}},
    ]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep CI logs clean
        pass

    def do_GET(self):  # noqa: N802
        body = json.dumps({"data": [{"id": "mock"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in _chunks(request.get("messages", [])):
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    """Start the server on 127.0.0.1 in a daemon thread; return it and its port."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", required=True)
    args = parser.parse_args()
    srv, bound = serve()
    Path(args.port_file).write_text(str(bound), encoding="utf-8")
    threading.Event().wait()
```

- [ ] **Step 2: Smoke-сценарий**

```python
# scripts/ci/smoke.py
"""End-to-end check of an installed `luna` (run after `uv tool install`, `luna` on PATH)."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mock_llm  # noqa: E402


def fail(message: str) -> None:
    print(f"SMOKE FAIL: {message}")
    sys.exit(1)


def luna(*args: str, env: dict, cwd: Path, timeout: float = 120) -> subprocess.CompletedProcess:
    exe = shutil.which("luna")
    if exe is None:
        fail("`luna` is not on PATH")
    return subprocess.run([exe, *args], env=env, cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main() -> None:
    home = Path(tempfile.mkdtemp(prefix="luna-smoke-"))
    project = home / "Мой проект"
    project.mkdir()
    server, port = mock_llm.serve()
    cfg_home = home / "cfg"
    (cfg_home / "luna").mkdir(parents=True)
    (cfg_home / "luna" / "config.toml").write_text(
        f'[provider.custom.mock]\nbase_url = "http://127.0.0.1:{port}/v1"\n'
        'default_model = "mock"\n',
        encoding="utf-8",
    )
    env = {**os.environ, "XDG_CONFIG_HOME": str(cfg_home)}
    env.pop("PYTHONUTF8", None)

    version = luna("--version", env=env, cwd=project)
    if version.returncode or not version.stdout.startswith("luna "):
        fail(f"--version: {version.stdout}{version.stderr}")

    path = luna("config", "path", env=env, cwd=project)
    if str(cfg_home) not in path.stdout:
        fail(f"config path ignores XDG_CONFIG_HOME: {path.stdout}")

    turn = luna("--provider", "mock", "--model", "mock", "--yolo", "--json", "--no-splash",
                "выполни команду", env=env, cwd=project, timeout=180)
    try:
        result = json.loads(turn.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        fail(f"--json output is not JSON:\n{turn.stdout}\n{turn.stderr}")
    if result.get("error"):
        fail(f"turn error: {result['error']}")
    if "execute" not in result.get("tools_used", []):
        fail(f"execute was not used: {result}")
    if "привет" not in result.get("text", ""):
        fail(f"tool output did not reach the answer: {result}")

    token = secrets.token_hex(8)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        serve_port = s.getsockname()[1]
    proc = subprocess.Popen([shutil.which("luna"), "serve", "--port", str(serve_port),
                             "--token", token], env=env, cwd=project)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{serve_port}/health",
                                             headers={"Authorization": f"Bearer {token}"})
                body = json.loads(urllib.request.urlopen(req, timeout=2).read())
                if body.get("ok") and body.get("protocol") == 1:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                fail("luna serve did not answer /health in 60 s")
            time.sleep(0.5)
    finally:
        proc.kill()
        server.shutdown()
    print("SMOKE OK")


if __name__ == "__main__":
    main()
```

Флаг `--no-splash` и порядок аргументов сверить с `luna --help`. Если `luna config path` печатает путь не так, как ожидает проверка, поправить проверку под реальный вывод.

- [ ] **Step 3: Локальный тест мока**

```python
# tests/test_ci_scripts.py
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "ci"))
import mock_llm  # noqa: E402


def _post(port, messages):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps({"messages": messages}).encode(),
        headers={"Content-Type": "application/json"},
    )
    raw = urllib.request.urlopen(req, timeout=5).read().decode("utf-8")
    return [line[6:] for line in raw.splitlines() if line.startswith("data: ")]


def test_mock_calls_execute_then_answers():
    server, port = mock_llm.serve()
    try:
        first = _post(port, [{"role": "user", "content": "go"}])
        assert first[-1] == "[DONE]" and '"execute"' in first[0]
        second = _post(port, [{"role": "tool", "content": "привет"}])
        assert "привет" in json.loads(second[0])["choices"][0]["delta"]["content"]
    finally:
        server.shutdown()
```

Run: `uv run pytest tests/test_ci_scripts.py -q -o addopts=""` — Expected: PASS.

Затем локальный smoke против editable-установки: `uv run python scripts/ci/smoke.py` (здесь `luna` берётся из `.venv/bin` через `uv run`). Expected: `SMOKE OK`. Если провайдер пишет в мок не по адресу `/v1/chat/completions`, поправить `base_url` в smoke под реальный `ProviderSpec` кастомного провайдера.

- [ ] **Step 4: Workflow**

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main, "feat/**"]
  pull_request:

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv venv --python 3.12
      - run: uv pip install -e packages/luna-core -e ".[dev,all]"
      - name: No agent frameworks installed
        shell: bash
        run: |
          if uv pip list | grep -Ei "langchain|langgraph|deepagents"; then exit 1; fi
      - run: uv run ruff check .
      - run: uv run ruff format --check .

  test:
    strategy:
      fail-fast: false
      matrix:
        include:
          - { os: ubuntu-latest, python: "3.11" }
          - { os: ubuntu-latest, python: "3.12" }
          - { os: macos-latest, python: "3.12" }
          - { os: windows-latest, python: "3.12" }
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv venv --python ${{ matrix.python }}
      - run: uv pip install -e packages/luna-core -e ".[dev,all]"
      - run: uv run pytest -q

  smoke:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - name: Build wheels
        run: uv build --all-packages --out-dir dist
      - name: Install like a user (wheels, not editable)
        run: uv tool install --python 3.12 "luna-simple[all]" --find-links dist
      - name: Put uv tools on PATH
        shell: bash
        run: uv tool dir --bin >> "$GITHUB_PATH"
      - name: luna doctor
        run: luna doctor
        continue-on-error: true
      - name: End-to-end turn through the real shell
        run: python scripts/ci/smoke.py

  installer:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    env:
      LUNA_REF: ${{ github.sha }}
      LUNA_REPO: https://github.com/${{ github.repository }}
      LUNA_NO_MODIFY_PATH: "1"
    steps:
      - uses: actions/checkout@v4
      - name: install.sh
        if: runner.os != 'Windows'
        run: sh scripts/install.sh
      - name: install.ps1
        if: runner.os == 'Windows'
        shell: pwsh
        run: ./scripts/install.ps1
      - name: luna from the uv tool dir
        shell: bash
        run: '"$(uv tool dir --bin)/luna" --version'

  installer-windows-no-git:
    runs-on: windows-latest
    env:
      LUNA_REF: ${{ github.sha }}
      LUNA_REPO: https://github.com/${{ github.repository }}
      LUNA_NO_MODIFY_PATH: "1"
    steps:
      - uses: actions/checkout@v4
      - name: Install with Git hidden (PortableGit path)
        shell: pwsh
        run: |
          $env:Path = ($env:Path -split ';' | Where-Object { $_ -notmatch '\\Git\\' }) -join ';'
          $env:ProgramFiles = 'C:\nope'
          ${env:ProgramFiles(x86)} = 'C:\nope'
          ./scripts/install.ps1
          if (-not (Test-Path "$env:LOCALAPPDATA\luna\git\bin\bash.exe")) { throw 'PortableGit not installed' }
          $luna = Join-Path (uv tool dir --bin).Trim() 'luna.exe'
          & $luna doctor
          if ($LASTEXITCODE -ne 0) { Write-Host 'doctor reported problems (path is expected off PATH)' }
          $out = & $luna doctor | Out-String
          if ($out -notmatch 'luna\\git\\bin\\bash.exe') { throw "doctor did not find PortableGit bash:`n$out" }
```

- [ ] **Step 5: YAML валиден и коммит**

Run: `uv run python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml', encoding='utf-8'))"`. Если PyYAML нет: `uvx --from pyyaml python -c "…"`.
Expected: без ошибок.

```bash
git add scripts/ci tests/test_ci_scripts.py .github/workflows/ci.yml
git commit -m "ci: test on ubuntu/macos/windows, end-to-end smoke via mock LLM, installers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Push ветки и разбор падений на Windows/macOS в CI

**Требует явного разрешения пользователя на `git push origin feat/windows`** (внешнее действие). Без него задача останавливается на Step 1.

**Files:** зависят от найденных падений. Каждое исправление — отдельный коммит с тестом.

- [ ] **Step 1: Полный локальный прогон**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: всё зелёное на macOS.

- [ ] **Step 2: Push (после разрешения)**

Run: `git push -u origin feat/windows`

- [ ] **Step 3: Дождаться CI**

Run: `gh run watch --exit-status $(gh run list --branch feat/windows --limit 1 --json databaseId -q '.[0].databaseId')`
Expected: зелёные `lint`, `test` ×4, `smoke` ×3, `installer` ×3, `installer-windows-no-git`.

- [ ] **Step 4: На каждое падение — systematic-debugging**

Для каждого красного job:
1. `gh run view --log-failed --job <id>`;
2. воспроизвести локально, если можно; если нет, записать гипотезу;
3. тест, который ловит причину;
4. исправление;
5. коммит `fix(windows): …`;
6. push и повторный прогон.

Типичные ожидаемые классы:
- тесты, которые пишут кириллицу без `encoding` (исправлять сам тест, не включать `PYTHONUTF8`);
- POSIX-пути в тестах (`/tmp/x`, `"/"` в ожиданиях) → `Path`/`as_posix()`;
- тесты с `os.symlink` (на Windows без режима разработчика) → `skipif(os.name == "nt")` с причиной;
- `test_repl_flow.py:378,429` (`os.kill(os.getpid(), SIGINT)`) → `signal.raise_signal(signal.SIGINT)`.

- [ ] **Step 5: Итог**

Когда CI полностью зелёный: обновить заметку Obsidian `Projects/Luna/luna desktop — исследование и проектирование.md` (что сделано, что осталось: ручной прогон TUI в Windows Terminal) и добавить задачу в Todoist «Проверить Luna вручную на Windows: установка, luna, ход с execute, /theme».
