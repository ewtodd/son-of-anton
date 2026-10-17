"""``son-of-anton status`` subcommand parser.

Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_status_parser(subparsers, *, cmd_status: Callable) -> None:
    """Attach the ``status`` subcommand to ``subparsers``."""
    status_parser = subparsers.add_parser(
        "status",
        help="Show status of all components",
        description="Display status of Son of Anton Agent components",
    )
    status_parser.set_defaults(func=cmd_status)
