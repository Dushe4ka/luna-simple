import os

import pytest
from luna_core import filelock
from luna_core.filelock import InterProcessRLock, file_lock


def test_an_unlock_error_does_not_mask_the_original_exception(tmp_path, monkeypatch):
    def broken(fd):
        raise OSError("unlock failed")

    monkeypatch.setattr(filelock, "_unlock", broken)
    with pytest.raises(ValueError, match="body"):
        with file_lock(tmp_path / "x.lock"):
            raise ValueError("body")
    lock = InterProcessRLock(tmp_path / "y.lock")
    with pytest.raises(ValueError, match="body"):
        with lock:
            raise ValueError("body")
    with lock:  # released despite the unlock error
        pass


def test_non_blocking_acquire_reports_a_held_lock(tmp_path):
    path = tmp_path / "z.lock"
    lock = InterProcessRLock(path)
    # another holder: a separate descriptor locked with the OS primitive (flock / msvcrt)
    held = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        assert filelock._lock(held, blocking=False) is True
        assert lock.acquire(blocking=False) is False
        filelock._unlock(held)
    finally:
        os.close(held)
    assert lock.acquire(blocking=False) is True
    lock.release()
