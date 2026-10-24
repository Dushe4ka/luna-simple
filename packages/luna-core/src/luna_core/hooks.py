"""Hook points around the loop: Python callables and user command hooks."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from luna_core.sandbox import scrub_env

POINTS = ("session_start", "pre_tool", "post_tool", "turn_end", "pre_compact")


@dataclass
class HookResult:
    """What a hook asks for."""

    block: bool = False
    message: str = ""
    args: dict | None = None
    context: str = ""
    notices: tuple[str, ...] | list[str] = field(default_factory=tuple)


HookFn = Callable[[dict], Awaitable[HookResult | None]]


@dataclass
class _Entry:
    """A registered hook: the callable plus its tool filter and timeout."""

    fn: HookFn
    match: str
    timeout: float


class Hooks:
    """An ordered registry of hooks per point."""

    def __init__(self) -> None:
        """Start with an empty hook list for every point in :data:`POINTS`."""
        self._hooks: dict[str, list[_Entry]] = {p: [] for p in POINTS}

    def add(self, point: str, fn: HookFn, *, match: str = "*", timeout: float = 60.0) -> None:
        """Register ``fn`` at ``point`` (``match`` filters tool names for tool points)."""
        if point not in POINTS:
            raise ValueError(f"unknown hook point {point!r}; expected one of {POINTS}")
        self._hooks[point].append(_Entry(fn, match, timeout))

    def extend(self, other: Hooks) -> None:
        """Append every hook of ``other``."""
        for point, entries in other._hooks.items():
            self._hooks[point].extend(entries)

    def __len__(self) -> int:
        """Count registered hooks across all points."""
        return sum(len(v) for v in self._hooks.values())

    async def run(self, point: str, payload: dict, *, tool: str | None = None) -> HookResult:
        """Run the hooks for ``point`` in order and merge their results.

        Hooks see ``payload["args"]`` as modified by earlier hooks. The first
        hook that blocks stops the chain. An exception or timeout in a hook
        becomes a notice and never blocks the chain.
        """
        merged = HookResult(notices=[])
        payload = {**payload, "event": point}
        contexts: list[str] = []
        for entry in self._hooks[point]:
            if tool is not None and not fnmatch(tool, entry.match):
                continue
            name = getattr(entry.fn, "__name__", None) or repr(entry.fn)
            try:
                result = await asyncio.wait_for(entry.fn(payload), entry.timeout)
            except TimeoutError:
                merged.notices.append(f"hook {name} timed out after {entry.timeout:g}s")
                continue
            except Exception as exc:  # noqa: BLE001 - a hook must never break a turn
                merged.notices.append(f"hook {name} failed: {type(exc).__name__}: {exc}")
                continue
            if result is None:
                continue
            merged.notices.extend(result.notices)
            if result.context:
                contexts.append(result.context)
            if result.args is not None:
                payload["args"] = result.args
                merged.args = result.args
            if result.block:
                merged.block = True
                merged.message = result.message
                break
        merged.context = "\n\n".join(contexts)
        merged.notices = tuple(merged.notices)
        return merged


class CommandHook:
    """A user hook: a shell command fed the JSON payload on stdin."""

    def __init__(
        self,
        command: str,
        *,
        cwd: str | Path,
        timeout: float = 60.0,
        env_passthrough: Iterable[str] = (),
    ) -> None:
        """Run ``command`` (via the shell) in ``cwd``, killing it after ``timeout`` seconds.

        Like ``execute``, the command gets the environment without secret
        variables (:data:`luna_core.sandbox.SECRET_ENV_PATTERNS`), except the
        names in ``env_passthrough``.
        """
        self.command = command
        self.env_passthrough = tuple(env_passthrough)
        self.cwd = str(cwd)
        self.timeout = timeout
        self.__name__ = command.split()[0] if command.split() else "command"

    async def __call__(self, payload: dict) -> HookResult:
        """Run the command; exit 0 continues, 2 blocks, anything else warns.

        The process starts its own session so a timeout can kill the whole
        process group, never leaving orphaned children behind.
        """
        proc = await asyncio.create_subprocess_shell(
            self.command,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=scrub_env(os.environ, passthrough=self.env_passthrough),
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps(payload, ensure_ascii=False).encode()),
                self.timeout,
            )
        except TimeoutError:
            self._kill_group(proc.pid)
            with contextlib.suppress(Exception):
                await proc.wait()
            return HookResult(notices=(f"hook `{self.command}` timed out after {self.timeout:g}s",))
        stdout = out.decode("utf-8", errors="replace")
        stderr = err.decode("utf-8", errors="replace")
        if proc.returncode == 0:
            return HookResult(context=stdout.strip())
        if proc.returncode == 2:
            return HookResult(
                block=True,
                message=(stderr or stdout).strip() or f"blocked by hook `{self.command}`",
            )
        return HookResult(
            notices=(f"hook `{self.command}` exited {proc.returncode}: {stderr.strip()[:300]}",)
        )

    @staticmethod
    def _kill_group(pid: int) -> None:
        """Kill the whole process group rooted at ``pid`` (started via start_new_session)."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)


def load_command_hooks(
    entries: list[dict], cwd: str | Path, *, env_passthrough: Iterable[str] = ()
) -> Hooks:
    """Build :class:`Hooks` from config ``[[hooks]]`` tables (invalid entries are skipped)."""
    hooks = Hooks()
    for entry in entries or []:
        point = entry.get("event")
        command = entry.get("command")
        if point not in POINTS or not isinstance(command, str) or not command.strip():
            continue
        timeout = float(entry.get("timeout", 60))
        hooks.add(
            point,
            CommandHook(command, cwd=cwd, timeout=timeout, env_passthrough=env_passthrough),
            match=str(entry.get("match", "*")),
            timeout=timeout + 5,
        )
    return hooks
