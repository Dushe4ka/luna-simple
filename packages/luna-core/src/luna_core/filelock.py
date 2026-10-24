"""A small portable inter-process lock on a file (``flock`` / ``msvcrt.locking``).

Thread locks protect one process only; the REPL and the server are two
processes that may share a repository (undo journal, subagent worktrees).
These helpers add an OS-level exclusive lock on a lock file next to the
protected state. If the lock file cannot be created (a read-only tree),
the lock degrades to the in-process one instead of failing the operation.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator
from pathlib import Path

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:  # LK_LOCK gives up after ~10 s; keep waiting
                continue

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open(path: Path) -> int | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None


@contextlib.contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Hold an exclusive OS lock on ``path`` (created if missing) for the block.

    Not reentrant: within one process, guard it with a thread lock (see
    :class:`InterProcessRLock`) so the same file is not locked twice.
    """
    fd = _open(Path(path))
    if fd is None:
        yield
        return
    try:
        _lock(fd)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


class InterProcessRLock:
    """A reentrant thread lock that also holds :func:`file_lock` while owned.

    The OS lock is taken by the outermost acquire and released by the
    matching release, so nested ``with`` blocks in one thread are fine.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._rlock = threading.RLock()
        self._depth = 0
        self._held: contextlib.AbstractContextManager | None = None

    def __enter__(self) -> InterProcessRLock:
        """Acquire the thread lock, and the file lock on the outermost entry."""
        self._rlock.acquire()
        try:
            if self._depth == 0:
                held = file_lock(self.path)
                held.__enter__()
                self._held = held
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        """Release; the file lock goes with the outermost exit."""
        try:
            self._depth -= 1
            if self._depth == 0 and self._held is not None:
                held, self._held = self._held, None
                held.__exit__(None, None, None)
        finally:
            self._rlock.release()


__all__ = ["InterProcessRLock", "file_lock"]
