"""Tests for ``luna doctor`` and ``luna update``."""

import subprocess
import sys

import pytest
from luna_core import shell

from luna import doctor


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


UV_WIN = r"C:\Users\Имя Фамилия\.local\bin\uv.exe"


def _uv_dir(prefix):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, f"{prefix}\n", "")


def test_update_refuses_when_not_a_uv_tool(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    code = doctor.run_update(
        [], which=lambda n: "/bin/uv", run=_uv_dir(tmp_path / "tools"), windows=False
    )
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
        [],
        which=lambda n: UV_WIN,
        run=_uv_dir(tools),
        popen=lambda argv, **kw: started.append((argv, kw)),
        windows=True,
        stop_server=lambda: False,
    )
    assert code == 0
    cmd, kw = started[0]
    assert isinstance(cmd, str) and cmd.startswith('cmd /c "')
    assert f'"{UV_WIN}" tool upgrade luna-simple' in cmd
    assert kw["creationflags"] & 0x00000010  # NEW_CONSOLE


def test_update_on_windows_stops_the_background_server_first(monkeypatch, tmp_path, capsys):
    """The server's python.exe keeps the tool venv's .pyd files mapped: uv cannot
    replace them ("Access is denied") while it runs (final review I2)."""
    tools = tmp_path / "tools"
    monkeypatch.setattr(sys, "prefix", str(tools / "luna-simple"))
    order = []
    code = doctor.run_update(
        [],
        which=lambda n: UV_WIN,
        run=_uv_dir(tools),
        popen=lambda argv, **kw: order.append("upgrade"),
        windows=True,
        stop_server=lambda: order.append("stop") or True,
    )
    assert code == 0 and order == ["stop", "upgrade"]
    out = capsys.readouterr().out
    assert "сервер" in out and "закройте" in out.lower()


def test_update_on_posix_leaves_the_server_running(monkeypatch, tmp_path):
    tools = tmp_path / "tools"
    monkeypatch.setattr(sys, "prefix", str(tools / "luna-simple"))

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, f"{tools}\n" if "dir" in argv else "", "")

    code = doctor.run_update(
        [],
        which=lambda n: "/bin/uv",
        run=run,
        windows=False,
        stop_server=lambda: pytest.fail("POSIX can replace files in use"),
    )
    assert code == 0


def test_update_reports_uv_tool_dir_failure(capsys):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 2, "", "boom")

    assert doctor.run_update([], which=lambda n: "/bin/uv", run=run, windows=False) == 1
    assert "не удалось определить каталог uv tool" in capsys.readouterr().err

    def raising(argv, **kw):
        raise OSError("нет uv")

    assert doctor.run_update([], which=lambda n: "/bin/uv", run=raising, windows=False) == 1


def test_update_refuses_another_uv_tool(monkeypatch, tmp_path, capsys):
    tools = tmp_path / "tools"
    monkeypatch.setattr(sys, "prefix", str(tools / "other-tool"))
    code = doctor.run_update([], which=lambda n: "/bin/uv", run=_uv_dir(tools), windows=False)
    assert code == 1 and "uv tool" in capsys.readouterr().err


@pytest.mark.parametrize("content", [b"{}", b"[]", b'{"pid": "x"}', b"\xff\xfe\x00bad"])
def test_doctor_survives_a_corrupt_server_json(content):
    from luna.server.auth import token_path

    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    check = next(c for c in doctor.collect() if c.name == "server")
    assert check.ok is False and "повреждён" in check.detail
