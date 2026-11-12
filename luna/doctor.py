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
    cfg = config_dir()
    writable = os.access(cfg if cfg.exists() else cfg.parent, os.W_OK)
    checks.append(Check("config", writable, str(cfg), "" if writable else "нет прав на запись"))
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
    token = read_token_file()
    alive = bool(token and _default_is_alive(token["pid"]))
    detail = f"работает (pid {token['pid']})" if alive else "не запущен"
    checks.append(Check("server", True, detail))
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


def run_update(
    argv,
    *,
    which=shutil.which,
    run=subprocess.run,
    popen=subprocess.Popen,
    windows: bool | None = None,
) -> int:
    """`luna update`: `uv tool upgrade luna-simple`, only for a `uv tool` install."""
    windows = IS_WINDOWS if windows is None else windows
    uv = which("uv")
    if uv is None:
        print(f"luna: uv не найден — переустановите Luna: {INSTALL_URL}", file=sys.stderr)
        return 1
    tool_dir = run([uv, "tool", "dir"], capture_output=True, text=True, encoding="utf-8").stdout
    tool_dir = tool_dir.strip()
    if not tool_dir or not Path(sys.prefix).resolve().is_relative_to(Path(tool_dir).resolve()):
        print(
            "luna: эта Luna установлена не через `uv tool` — обновите её тем же способом, "
            "каким ставили",
            file=sys.stderr,
        )
        return 1
    if windows:
        # Windows locks the running luna.exe: upgrade in a new console after we exit.
        popen(
            ["cmd", "/c", f'timeout /t 2 >nul & "{uv}" tool upgrade luna-simple & pause'],
            creationflags=_CREATE_NEW_CONSOLE,
        )
        print("luna: обновление запущено в новом окне")
        return 0
    return run([uv, "tool", "upgrade", "luna-simple"]).returncode
