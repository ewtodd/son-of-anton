"""Shared Rich console with optional tee-to-file logging."""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Any

from rich.console import Console


class LoggingConsole(Console):
    """Console that optionally tees output to a log file."""

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self._log_console: Console | None = None
        self._log_file = None

    def setup_log(self, path: str | Path) -> None:
        """Start tee-ing to *path* (append mode). Idempotent."""
        if self._log_console is not None:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(path, "a", encoding="utf-8")  # noqa: SIM115
        self._log_console = Console(
            file=self._log_file,
            force_terminal=True,
            width=120,
            color_system="standard",
        )
        atexit.register(self._close_log)

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
            self._log_console = None

    def print(self, *args: Any, **kwargs: Any) -> None:
        super().print(*args, **kwargs)
        if self._log_console is not None:
            self._log_console.print(*args, **kwargs)

    def rule(self, *args: Any, **kwargs: Any) -> None:
        super().rule(*args, **kwargs)
        if self._log_console is not None:
            self._log_console.rule(*args, **kwargs)


# Module-level singleton — every module imports this.
console = LoggingConsole()
