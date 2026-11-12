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
        Check(
            "path",
            bool(on_path),
            on_path or "luna нет в PATH",
            "" if on_path else "выполните `uv tool update-shell` и откройте новый терминал",
        )
    )
    try:
        cfg = config_dir()
        writable = os.access(cfg if cfg.exists() else cfg.parent, os.W_OK)
        checks.append(Check("config", writable, str(cfg), "" if writable else "нет прав на запись"))
    except Exception as exc:  # noqa: BLE001 - the report must never crash
        checks.append(Check("config", False, f"не удалось определить: {exc}"))
    try:
        find_shell()
        checks.append(Check("shell", True, describe_shell()))
    except ShellNotFound as exc:
        checks.append(Check("shell", False, "оболочка для команд не найдена", str(exc)))
    git = shutil.which("git")
    checks.append(
        Check(
            "git",
            bool(git),
            git or "git не найден",
            "" if git else "нужен для /undo: https://git-scm.com/downloads",
        )
    )
    checks.append(_server_check(read_token_file, _default_is_alive))
    return checks


def _server_check(read_token, is_alive) -> Check:
    """Server status; a corrupt ``server.json`` is a failed check, not a crash."""
    from luna.server.auth import token_path

    try:
        token = read_token()
        if token is None:
            return Check("server", True, "не запущен")
        pid = token["pid"]
        if not isinstance(token, dict) or isinstance(pid, bool) or not isinstance(pid, int):
            raise ValueError("bad token file")
        if is_alive(pid):
            return Check("server", True, f"работает (pid {pid})")
        return Check("server", True, "не запущен")
    except Exception:  # noqa: BLE001 - corrupt file of any shape
        path = token_path()
        return Check(
            "server", False, "server.json повреждён", f"удалите {path} — Luna создаст его заново"
        )


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


def run_update(
    argv,
    *,
    which=shutil.which,
    run=subprocess.run,
    popen=subprocess.Popen,
    windows: bool | None = None,
    stop_server=None,
) -> int:
    """`luna update`: `uv tool upgrade luna-simple`, only for a `uv tool` install."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="luna update",
        description="Обновить Luna: `uv tool upgrade luna-simple` (только для установки "
        "через `uv tool`). На Windows фоновый сервер Luna сначала останавливается.",
    )
    try:
        parser.parse_args(list(argv))
    except SystemExit as exc:  # --help or a bad argument: never go on to upgrade
        return exc.code if isinstance(exc.code, int) else 0
    windows = IS_WINDOWS if windows is None else windows
    uv = which("uv")
    if uv is None:
        print(f"luna: uv не найден — переустановите Luna: {INSTALL_URL}", file=sys.stderr)
        return 1
    try:
        proc = run(
            [uv, "tool", "dir"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise OSError((proc.stderr or "").strip() or f"код {proc.returncode}")
        tool_dir = proc.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"luna: не удалось определить каталог uv tool ({exc})", file=sys.stderr)
        return 1
    ours = Path(tool_dir).resolve() / "luna-simple" if tool_dir else None
    if ours is None or not Path(sys.prefix).resolve().is_relative_to(ours):
        print(
            "luna: эта Luna установлена не через `uv tool` — обновите её тем же способом, "
            "каким ставили",
            file=sys.stderr,
        )
        return 1
    if windows:
        # The background server's python.exe keeps the tool's .pyd files mapped, and
        # Windows refuses to replace a file in use: stop it before uv runs.
        if stop_server is None:
            from luna.server.run import stop_server
        if stop_server():
            print("luna: фоновый сервер Luna остановлен")
        print(
            "luna: закройте другие окна Luna (TUI, REPL, luna acp в редакторе) — "
            "пока они открыты, Windows не даст заменить файлы и uv ответит "
            "«Отказано в доступе»"
        )
        # Windows locks the running luna.exe: upgrade in a new console after we exit.
        # One verbatim string: cmd strips the outer quotes, the inner ones keep spaces safe.
        popen(
            f'cmd /c "timeout /t 2 >nul & "{uv}" tool upgrade luna-simple & pause"',
            creationflags=_CREATE_NEW_CONSOLE,
            # no std handles passed on: the new console's own input serves `pause`
            stdin=None,
        )
        print("luna: обновление запущено в новом окне")
        return 0
    # the user's terminal, as before: uv may print progress and ask nothing
    return run([uv, "tool", "upgrade", "luna-simple"], stdin=None).returncode
