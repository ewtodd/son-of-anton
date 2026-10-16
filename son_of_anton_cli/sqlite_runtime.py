"""Import-safe helpers for inspecting a Python interpreter's linked SQLite.

This module intentionally depends only on the standard library.  Installer and
update code must be able to use it before Son of Anton' third-party dependencies are
healthy.
"""

from __future__ import annotations

from typing import Iterable


def _version_tuple(parts: Iterable[object]) -> tuple[int, int, int]:
    values = [int(part) for part in parts]
    values.extend([0] * (3 - len(values)))
    return tuple(values[:3])


def is_sqlite_wal_reset_vulnerable(
    version_info: tuple[int, ...],
) -> bool:
    """Return whether *version_info* contains SQLite's WAL-reset bug."""
    info = _version_tuple(version_info)
    if info < (3, 7, 0):
        return False
    if info >= (3, 51, 3):
        return False
    if (3, 50, 7) <= info < (3, 51, 0):
        return False
    if (3, 44, 6) <= info < (3, 45, 0):
        return False
    return True

