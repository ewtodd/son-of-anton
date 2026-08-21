"""Resolve SON_OF_ANTON_HOME for standalone skill scripts.

Skill scripts may run outside the Son of Anton process (e.g. system Python,
nix env, CI) where ``son_of_anton_constants`` is not importable.  This module
provides the same ``get_son_of_anton_home()`` and ``display_son_of_anton_home()``
contracts as ``son_of_anton_constants`` without requiring it on ``sys.path``.

When ``son_of_anton_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``son_of_anton_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``SON_OF_ANTON_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from son_of_anton_constants import display_son_of_anton_home as display_son_of_anton_home
    from son_of_anton_constants import get_son_of_anton_home as get_son_of_anton_home
except (ModuleNotFoundError, ImportError):

    def get_son_of_anton_home() -> Path:
        """Return the Son of Anton home directory (default: ~/.son-of-anton).

        Mirrors ``son_of_anton_constants.get_son_of_anton_home()``."""
        val = os.environ.get("SON_OF_ANTON_HOME", "").strip()
        return Path(val) if val else Path.home() / ".son-of-anton"

    def display_son_of_anton_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``son_of_anton_constants.display_son_of_anton_home()``."""
        home = get_son_of_anton_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
