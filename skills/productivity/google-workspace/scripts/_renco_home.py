"""Resolve RENCO_HOME for standalone skill scripts.

Skill scripts may run outside the Renco process (e.g. system Python,
nix env, CI) where ``renco_constants`` is not importable.  This module
provides the same ``get_renco_home()`` and ``display_renco_home()``
contracts as ``renco_constants`` without requiring it on ``sys.path``.

When ``renco_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``renco_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``RENCO_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from renco_constants import display_renco_home as display_renco_home
    from renco_constants import get_renco_home as get_renco_home
except (ModuleNotFoundError, ImportError):

    def get_renco_home() -> Path:
        """Return the Renco home directory (default: ~/.renco).

        Mirrors ``renco_constants.get_renco_home()``."""
        val = os.environ.get("RENCO_HOME", "").strip()
        return Path(val) if val else Path.home() / ".renco"

    def display_renco_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``renco_constants.display_renco_home()``."""
        home = get_renco_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
