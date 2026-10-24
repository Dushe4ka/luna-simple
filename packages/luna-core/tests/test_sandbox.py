import asyncio
import shutil
import stat
import subprocess
import sys
import time

import pytest
from luna_core.sandbox import DockerSandbox, LocalSandbox


def _docker_daemon_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker_image_available(image: str, pull_timeout: float = 30) -> bool:
    """True when ``image`` is present or can be pulled quickly (skip, not fail, otherwise)."""
    try:
        if (
            subprocess.run(
                ["docker", "image", "inspect", image], capture_output=True, timeout=10
            ).returncode
            == 0
        ):
            return True
        return (
            subprocess.run(
                ["docker", "pull", image], capture_output=True, timeout=pull_timeout
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


async def test_local_runs_and_merges_output(tmp_path):
    r = await LocalSandbox().run("echo out; echo err 1>&2; exit 3", cwd=tmp_path, timeout=10)
    assert r.exit_code == 3 and "out" in r.output and "err" in r.output and not r.timed_out


async def test_local_timeout_kills_the_process_group(tmp_path):
    marker = tmp_path / "late"
    started = time.monotonic()
    r = await LocalSandbox().run(f"(sleep 3; touch {marker}) & sleep 30", cwd=tmp_path, timeout=0.5)
    assert r.timed_out and time.monotonic() - started < 5
    await asyncio.sleep(3.5)
    assert not marker.exists()  # the background child died with the group


async def test_local_cancel_event_stops_the_command(tmp_path):
    cancel = asyncio.Event()
    task = asyncio.create_task(
        LocalSandbox().run("sleep 30", cwd=tmp_path, timeout=60, cancel=cancel)
    )
    await asyncio.sleep(0.2)
    cancel.set()
    r = await asyncio.wait_for(task, 5)
    assert r.cancelled


async def test_local_output_is_capped(tmp_path):
    r = await LocalSandbox(max_output=1000).run(
        f"{sys.executable} -c \"print('x' * 50000)\"", cwd=tmp_path, timeout=10
    )
    assert len(r.output) < 1200 and "truncated" in r.output


@pytest.fixture
def fake_docker(tmp_path):
    log = tmp_path / "docker.log"
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        'case "$1" in\n'
        "  run) echo cid123 ;;\n"
        '  exec) shift; while [ "$1" != "cid123" ]; do shift; done; shift; "$@" ;;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


async def test_docker_sandbox_lifecycle(tmp_path, fake_docker):
    script, log = fake_docker
    box = DockerSandbox(tmp_path, image="img:1", network="none", docker=str(script))
    await box.start()
    assert box.container_id == "cid123"
    r = await box.run("echo inside", cwd=tmp_path / "sub", timeout=10)
    assert r.exit_code == 0 and "inside" in r.output
    await box.close()
    lines = log.read_text().splitlines()
    assert (
        lines[0].startswith("run -d --rm --network none")
        and f"-v {tmp_path}:/workspace" in lines[0]
    )
    assert "img:1 sleep infinity" in lines[0]
    assert lines[1].startswith("exec -w /workspace/sub cid123 sh -lc")
    assert lines[2] == "rm -f cid123"


@pytest.mark.skipif(
    not _docker_daemon_available(), reason="docker not installed or daemon not running"
)
@pytest.mark.live
async def test_docker_sandbox_real(tmp_path):
    if not _docker_image_available("alpine:3.20"):
        pytest.skip("alpine:3.20 is not available locally and could not be pulled quickly")
    (tmp_path / "f.txt").write_text("hi")
    box = DockerSandbox(tmp_path, image="alpine:3.20")
    await box.start()
    try:
        r = await box.run("cat f.txt && ls /", cwd=tmp_path, timeout=60)
        assert "hi" in r.output and "workspace" in r.output
    finally:
        await box.close()


_SECRETS = {
    "OPENAI_API_KEY": "sk-leak-openai",
    "GITHUB_TOKEN": "ghp-leak-token",
    "MY_SECRET": "leak-secret",
    "GIGACHAT_CREDENTIALS": "leak-giga",
    "LUNA_SERVER_TOKEN": "leak-server",
}


async def test_local_scrubs_secret_env_by_default(tmp_path, monkeypatch):
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LUNA_PLAIN_VAR", "visible-value")
    r = await LocalSandbox().run("env", cwd=tmp_path, timeout=10)
    assert r.exit_code == 0
    assert "visible-value" in r.output
    for name, value in _SECRETS.items():
        assert value not in r.output and f"{name}=" not in r.output


async def test_local_env_passthrough_lets_named_vars_through(tmp_path, monkeypatch):
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    box = LocalSandbox(env_passthrough=("GITHUB_TOKEN",))
    r = await box.run("env", cwd=tmp_path, timeout=10)
    assert "ghp-leak-token" in r.output
    assert "sk-leak-openai" not in r.output


def test_scrub_env_default_patterns():
    from luna_core.sandbox import SECRET_ENV_PATTERNS, scrub_env

    assert {"*_API_KEY", "*_TOKEN", "*_SECRET", "GIGACHAT_*", "LUNA_SERVER_TOKEN"} <= set(
        SECRET_ENV_PATTERNS
    )
    env = {**_SECRETS, "PATH": "/bin", "HOME": "/h"}
    assert scrub_env(env) == {"PATH": "/bin", "HOME": "/h"}
    assert scrub_env(env, passthrough=["MY_SECRET"]) == {
        "PATH": "/bin",
        "HOME": "/h",
        "MY_SECRET": "leak-secret",
    }


async def test_docker_passthrough_forwards_names_without_values(tmp_path, fake_docker, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-leak-token")
    script, log = fake_docker
    box = DockerSandbox(tmp_path, docker=str(script), env_passthrough=("GITHUB_TOKEN",))
    await box.run("true", cwd=tmp_path, timeout=10)
    exec_line = log.read_text().splitlines()[1]
    assert "-e GITHUB_TOKEN " in exec_line and "ghp-leak-token" not in exec_line


async def test_docker_scrubs_secrets_from_explicit_env(tmp_path, fake_docker):
    script, log = fake_docker
    box = DockerSandbox(tmp_path, docker=str(script))
    await box.run("true", cwd=tmp_path, timeout=10, env={"OPENAI_API_KEY": "x", "A": "1"})
    exec_line = log.read_text().splitlines()[1]
    assert "OPENAI_API_KEY" not in exec_line and "-e A=1" in exec_line
