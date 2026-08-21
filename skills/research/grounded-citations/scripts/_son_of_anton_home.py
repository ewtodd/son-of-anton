"""Resolve SON_OF_ANTON_HOME for standalone skill scripts.

Skill scripts may run outside the Son of Anton process (system Python, nix env,
CI) where ``son_of_anton_constants`` is not importable.  This module provides the
same ``get_son_of_anton_home()`` contract without requiring it on ``sys.path``.

When ``son_of_anton_constants`` IS available it is used directly so profile
resolution and any future enhancements are picked up automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from son_of_anton_constants import get_son_of_anton_home as get_son_of_anton_home
except (ModuleNotFoundError, ImportError):

    def get_son_of_anton_home() -> Path:
        """Return the Son of Anton home directory (default: ``~/.son-of-anton``)."""
        val = os.environ.get("SON_OF_ANTON_HOME", "").strip()
        return Path(val) if val else Path.home() / ".son-of-anton"
