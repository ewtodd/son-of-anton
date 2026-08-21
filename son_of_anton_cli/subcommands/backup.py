"""``son-of-anton backup`` subcommand parser.

Extracted verbatim from ``son_of_anton_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_backup_parser(subparsers, *, cmd_backup: Callable) -> None:
    """Attach the ``backup`` subcommand to ``subparsers``."""
    # =========================================================================
    # backup command
    # =========================================================================
    backup_parser = subparsers.add_parser(
        "backup",
        help="Back up Son of Anton home directory to a zip file",
        description="Create a zip archive of your entire Son of Anton configuration, "
        "skills, sessions, and data (excludes the son-of-anton codebase). "
        "Use --quick for a fast snapshot of just critical state files.",
    )
    backup_parser.add_argument(
        "-o",
        "--output",
        help="Output path for the zip file (default: ~/son-of-anton-backup-<timestamp>.zip)",
    )
    backup_parser.add_argument(
        "-q",
        "--quick",
        action="store_true",
        help="Quick snapshot: only critical state files (config, state.db, .env, auth, cron)",
    )
    backup_parser.add_argument(
        "-l", "--label", help="Label for the snapshot (only used with --quick)"
    )
    backup_parser.set_defaults(func=cmd_backup)
