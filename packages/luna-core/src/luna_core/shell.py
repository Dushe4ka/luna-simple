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
    """Report whether ``bash.exe`` is a stub that starts WSL instead of Git Bash."""
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
    r"""Return ``%LOCALAPPDATA%\luna\git`` when the installer put PortableGit there."""
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
