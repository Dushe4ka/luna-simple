"""Owner-only files: 0600 on POSIX; on Windows the user profile's ACL already is."""

from __future__ import annotations

import os

POSIX_PERMISSIONS = os.name != "nt"


def restrict(fd: int) -> None:
    """Make the open file readable and writable by its owner only (no-op on Windows)."""
    if POSIX_PERMISSIONS:
        os.fchmod(fd, 0o600)
