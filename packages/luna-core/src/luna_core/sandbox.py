"""Where shell commands run: on the host or in a per-session Docker container."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RunResult:
    """Outcome of one command."""

    exit_code: int
    output: str
    timed_out: bool = False
    cancelled: bool = False


class Sandbox(Protocol):
    """Runs shell commands for the ``execute`` tool."""

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        env: dict | None = None,
        cancel: asyncio.Event | None = None,
    ) -> RunResult:
        """Run ``command`` and return its merged stdout/stderr."""
        ...

    async def close(self) -> None:
        """Release resources (containers)."""
        ...


def _cap(data: bytes, limit: int) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    head, tail = text[: limit // 2], text[-limit // 2 :]
    return f"{head}\n… [{len(text) - limit} characters truncated] …\n{tail}"


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
    kwargs = dict(
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    if shell:
        proc = await asyncio.create_subprocess_shell(argv_or_cmd, **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*argv_or_cmd, **kwargs)
    reader = asyncio.ensure_future(proc.stdout.read())
    cancel_wait = asyncio.ensure_future(cancel.wait()) if cancel else None
    waiters = {reader} | ({cancel_wait} if cancel_wait else set())
    timed_out = cancelled = False
    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if reader not in done:
            cancelled = cancel_wait is not None and cancel_wait in done
            timed_out = not cancelled
            _kill_group(proc)
    except asyncio.CancelledError:
        _kill_group(proc)
        reader.cancel()
        raise
    finally:
        if cancel_wait:
            cancel_wait.cancel()
    if timed_out or cancelled:
        try:
            data = await asyncio.wait_for(reader, 2)
        except (TimeoutError, asyncio.CancelledError):
            data = b""
    else:
        data = reader.result()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), 5)
    code = proc.returncode if proc.returncode is not None else -1
    output = _cap(data, limit)
    if timed_out:
        output += f"\n[timed out after {timeout:g}s]"
    if cancelled:
        output += "\n[cancelled]"
    return RunResult(code, output, timed_out, cancelled)


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


class LocalSandbox:
    """Runs commands on the host in their own process group (killed on timeout/cancel)."""

    def __init__(self, max_output: int = 200_000) -> None:
        self.max_output = max_output

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        env: dict | None = None,
        cancel: asyncio.Event | None = None,
    ) -> RunResult:
        """Run ``command`` with ``sh`` in ``cwd``."""
        full_env = {**os.environ, **(env or {})}
        return await _run_process(
            command,
            shell=True,
            cwd=cwd,
            timeout=timeout,
            env=full_env,
            cancel=cancel,
            limit=self.max_output,
        )

    async def close(self) -> None:
        """Nothing to release."""


class DockerSandbox:
    """One container per session; the project is mounted at ``/workspace``."""

    def __init__(
        self,
        root: str | os.PathLike,
        *,
        image: str = "python:3.12-slim",
        network: str = "none",
        docker: str = "docker",
        extra_args: tuple[str, ...] = (),
        max_output: int = 200_000,
    ) -> None:
        if network not in ("none", "bridge"):
            raise ValueError("network must be 'none' or 'bridge'")
        self.root = Path(root).resolve()
        self.image = image
        self.network = network
        self.docker = docker
        self.extra_args = tuple(extra_args)
        self.max_output = max_output
        self.container_id: str | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the container (idempotent)."""
        async with self._lock:
            if self.container_id:
                return
            argv = [
                self.docker,
                "run",
                "-d",
                "--rm",
                "--network",
                self.network,
                "-v",
                f"{self.root}:/workspace",
                "-w",
                "/workspace",
                *self.extra_args,
                self.image,
                "sleep",
                "infinity",
            ]
            result = await _run_process(
                argv, shell=False, cwd=None, timeout=120, env=None, cancel=None, limit=10_000
            )
            if result.exit_code != 0:
                raise RuntimeError(f"docker run failed: {result.output.strip()}")
            self.container_id = result.output.strip().splitlines()[-1]

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        env: dict | None = None,
        cancel: asyncio.Event | None = None,
    ) -> RunResult:
        """Run ``command`` inside the container, in the matching ``/workspace`` subdirectory."""
        await self.start()
        try:
            rel = Path(cwd).resolve().relative_to(self.root).as_posix()
        except ValueError:
            rel = "."
        workdir = "/workspace" if rel == "." else f"/workspace/{rel}"
        env_args = [arg for k, v in (env or {}).items() for arg in ("-e", f"{k}={v}")]
        argv = [
            self.docker,
            "exec",
            "-w",
            workdir,
            *env_args,
            self.container_id,
            "sh",
            "-lc",
            command,
        ]
        return await _run_process(
            argv,
            shell=False,
            cwd=None,
            timeout=timeout,
            env=None,
            cancel=cancel,
            limit=self.max_output,
        )

    async def close(self) -> None:
        """Remove the container."""
        if self.container_id:
            cid, self.container_id = self.container_id, None
            await _run_process(
                [self.docker, "rm", "-f", cid],
                shell=False,
                cwd=None,
                timeout=60,
                env=None,
                cancel=None,
                limit=10_000,
            )

    def __repr__(self) -> str:
        """Readable form for notices."""
        return (
            f"DockerSandbox(image={self.image!r}, network={self.network!r}, "
            f"container={self.container_id!r})"
        )
