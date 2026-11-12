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
    argv = shell.find_shell(
        env, windows=True, exists=_exists(r"D:\tools\Git\bin\bash.exe"), which=_no_git
    )
    assert argv == [r"D:\tools\Git\bin\bash.exe", "-c"]


def test_windows_explicit_env_missing_file_is_an_error_not_a_fallback():
    env = {**WIN_ENV, "LUNA_GIT_BASH_PATH": r"D:\nope\bash.exe"}
    std = r"C:\Program Files\Git\bin\bash.exe"
    with pytest.raises(shell.ShellNotFound, match="LUNA_GIT_BASH_PATH"):
        shell.find_shell(env, windows=True, exists=_exists(std), which=_no_git)


def test_windows_finds_bash_next_to_git_on_path():
    git = r"E:\PortableGit\cmd\git.exe"
    bash = r"E:\PortableGit\bin\bash.exe"
    argv = shell.find_shell(
        WIN_ENV,
        windows=True,
        exists=_exists(bash),
        which=lambda name, path=None: git if name == "git" else None,
    )
    assert argv[0] == bash


def test_windows_finds_bash_for_mingw64_git():
    git = r"E:\G\mingw64\bin\git.exe"
    bash = r"E:\G\bin\bash.exe"
    argv = shell.find_shell(
        WIN_ENV,
        windows=True,
        exists=_exists(bash),
        which=lambda name, path=None: git if name == "git" else None,
    )
    assert argv[0] == bash


def test_windows_standard_and_portable_locations():
    std = r"C:\Program Files\Git\bin\bash.exe"
    assert shell.find_shell(WIN_ENV, windows=True, exists=_exists(std), which=_no_git)[0] == std
    portable = r"C:\Users\u\AppData\Local\luna\git\bin\bash.exe"
    assert (
        shell.find_shell(WIN_ENV, windows=True, exists=_exists(portable), which=_no_git)[0]
        == portable
    )


@pytest.mark.parametrize(
    "wsl",
    [
        r"C:\Windows\System32\bash.exe",
        r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\bash.exe",
    ],
)
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


def test_kill_tree_reaches_a_grandchild_behind_an_exec(tmp_path):
    """bash ``exec``s a lone ``sh -c``; on Windows that hid sh from ``taskkill /T``."""
    marker = (tmp_path / "late").as_posix()
    command = f"sh -c \"(sleep 2; touch '{marker}') & sleep 30\""
    proc = subprocess.Popen(
        shell.shell_argv(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **shell.popen_kwargs(),
    )
    shell.track(proc.pid)
    try:
        time.sleep(0.5)
        shell.kill_tree(proc.pid)
        proc.wait(timeout=10)
    finally:
        shell.forget(proc.pid)
    time.sleep(2.5)
    assert not (tmp_path / "late").exists()


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
