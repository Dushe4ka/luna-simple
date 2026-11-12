"""Where Luna keeps per-user files on each OS."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def user_config_dir(env: Mapping[str, str] | None = None, *, windows: bool | None = None) -> Path:
    r"""Return Luna's config dir.

    ``$XDG_CONFIG_HOME/luna``, else ``%APPDATA%\luna`` on Windows, else ``~/.config/luna``.
    """
    env = os.environ if env is None else env
    windows = (os.name == "nt") if windows is None else windows
    if env.get("XDG_CONFIG_HOME"):
        return Path(env["XDG_CONFIG_HOME"]) / "luna"
    if windows and env.get("APPDATA"):
        return Path(env["APPDATA"]) / "luna"
    return Path.home() / ".config" / "luna"
