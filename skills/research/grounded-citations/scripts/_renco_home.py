"""Resolve RENCO_HOME for standalone skill scripts.

Skill scripts may run outside the Renco process (system Python, nix env,
CI) where ``renco_constants`` is not importable.  This module provides the
same ``get_renco_home()`` contract without requiring it on ``sys.path``.

When ``renco_constants`` IS available it is used directly so profile
resolution and any future enhancements are picked up automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from renco_constants import get_renco_home as get_renco_home
except (ModuleNotFoundError, ImportError):

    def get_renco_home() -> Path:
        """Return the Renco home directory (default: ``~/.renco``)."""
        val = os.environ.get("RENCO_HOME", "").strip()
        return Path(val) if val else Path.home() / ".renco"
