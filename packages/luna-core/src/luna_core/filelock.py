"""A small portable inter-process lock on a file (``flock`` / ``msvcrt.locking``).

Thread locks protect one process only; the REPL and the server are two
processes that may share a repository (undo journal, subagent worktrees).
These helpers add an OS-level exclusive lock on a lock file next to the
protected state. If the lock file cannot be created (a read-only tree),
the lock degrades to the in-process one instead of failing the operation.
Closing the descriptor always drops the OS lock, so an unlock error is
ignored rather than allowed to mask the real outcome.
"""

from __future__ import annotations

import contextlib
import errno
import os
import threading
from collections.abc import Iterator
from pathlib import Path

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt

    # ``LK_LOCK`` gives up after ~10 s with one of these; anything else is real.
    _BUSY = {errno.EACCES, errno.EDEADLOCK}

    def _lock(fd: int, blocking: bool) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                return True
            except OSError as exc:
                if exc.errno not in _BUSY:
                    raise
                if not blocking:
                    return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int, blocking: bool) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open(path: Path) -> int | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None


def _acquire(path: Path, blocking: bool) -> tuple[bool, int | None]:
    """Lock ``path``; returns (acquired, fd). ``fd`` is ``None`` when degraded."""
    fd = _open(path)
    if fd is None:
        return True, None
    try:
        if _lock(fd, blocking):
            return True, fd
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return False, None


def _release(fd: int | None) -> None:
    if fd is None:
        return
    try:
        with contextlib.suppress(OSError):
            _unlock(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Hold an exclusive OS lock on ``path`` (created if missing) for the block.

    Not reentrant: within one process, guard it with a thread lock (see
    :class:`InterProcessRLock`) so the same file is not locked twice.
    """
    _acquired, fd = _acquire(Path(path), True)
    try:
        yield
    finally:
        _release(fd)


class InterProcessRLock:
    """A reentrant thread lock that also holds the OS file lock while owned.

    The OS lock is taken by the outermost acquire and released by the
    matching release, so nested ``with`` blocks in one thread are fine.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    def acquire(self, blocking: bool = True) -> bool:
        """Acquire the lock; with ``blocking=False`` return ``False`` when it is held."""
        if not self._rlock.acquire(blocking):
            return False
        try:
            if self._depth == 0:
                acquired, fd = _acquire(self.path, blocking)
                if not acquired:
                    self._rlock.release()
                    return False
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise
        return True

    def release(self) -> None:
        """Release one level; the OS lock goes with the outermost release."""
        try:
            self._depth -= 1
            if self._depth == 0:
                fd, self._fd = self._fd, None
                _release(fd)
        finally:
            self._rlock.release()

    def __enter__(self) -> InterProcessRLock:
        """Acquire (blocking)."""
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        """Release."""
        self.release()


__all__ = ["InterProcessRLock", "file_lock"]
