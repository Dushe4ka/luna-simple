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
    """Print the failure and exit with code 1."""
    print(f"SMOKE FAIL: {message}")
    sys.exit(1)


def luna(*args: str, env: dict, cwd: Path, timeout: float = 120) -> subprocess.CompletedProcess:
    """Run the installed ``luna`` with the given arguments."""
    exe = shutil.which("luna")
    if exe is None:
        fail("`luna` is not on PATH")
    return subprocess.run(
        [exe, *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def main() -> None:
    """Run version, config, one-shot turn and serve /health checks."""
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

    turn = luna(
        "--provider",
        "mock",
        "--model",
        "mock",
        "--yolo",
        "--json",
        "--no-splash",
        "выполни команду",
        env=env,
        cwd=project,
        timeout=180,
    )
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
    proc = subprocess.Popen(
        [shutil.which("luna"), "serve", "--port", str(serve_port), "--token", token],
        env=env,
        cwd=project,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{serve_port}/health",
                    headers={"Authorization": f"Bearer {token}"},
                )
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
