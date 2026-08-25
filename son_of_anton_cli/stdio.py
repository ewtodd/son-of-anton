"""Stdio configuration helpers.

Son of Anton is Nix-only (Linux + macOS), where Python's stdio already
defaults to UTF-8 — the old Windows console code-page machinery that lived
here was removed. :func:`configure_windows_stdio` is kept as a no-op so
legacy entry points (``gateway/run.py``) can keep calling it unconditionally.

This module is a no-op on every supported platform, and idempotent.
"""

from __future__ import annotations

__all__ = ["configure_windows_stdio"]


def configure_windows_stdio() -> bool:
    """Force UTF-8 stdio on Windows.  No-op on Nix platforms.

    Kept importable for entry points that call it early in startup; returns
    ``False`` to signal that nothing was changed.
    """
    return False
