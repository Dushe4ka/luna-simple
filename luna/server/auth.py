"""Local server auth: a token file the TUI/CLI reads to talk to the server."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from luna.config.config import config_dir


def token_path(env: Mapping[str, str] | None = None) -> Path:
    """Path to the local server's token file."""
    return config_dir(env) / "server.json"


def write_token_file(
    port: int,
    token: str,
    pid: int,
    *,
    fingerprint: float | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Write the token file with mode 0600.

    ``fingerprint`` (see ``luna.server.run._code_fingerprint``) records
    what the server's own on-disk code looked like when it started, so a
    later ``ensure_running`` call from a *different*, newer checkout of
    the code can tell this running server is stale and needs restarting
    rather than being reused forever. ``None`` when the caller has no
    fingerprint to record (e.g. a test double).
    """
    path = token_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"port": port, "token": token, "pid": pid}
    if fingerprint is not None:
        data["fingerprint"] = fingerprint
    path.write_text(json.dumps(data))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def read_token_file(*, env: Mapping[str, str] | None = None) -> dict | None:
    """Return {"port", "token", "pid"} or None if absent/unreadable."""
    path = token_path(env)
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
