"""``son-of-anton config`` subcommand parser (read-only: Nix owns config.toml).

Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_config_parser(subparsers, *, cmd_config: Callable) -> None:
    """Attach the ``config`` subcommand to ``subparsers``."""
    config_parser = subparsers.add_parser(
        "config",
        help="View configuration",
        description="Show Son of Anton Agent configuration",
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("show", help="Show current configuration")
    config_get = config_subparsers.add_parser(
        "get", help="Print a resolved configuration value"
    )
    config_get.add_argument("key", nargs="?", help="Configuration key (e.g., model)")
    config_get.add_argument("--json", action="store_true", help="Print value as JSON")
    config_subparsers.add_parser("path", help="Print config file path")
    config_subparsers.add_parser("env-path", help="Print .env file path")
    config_parser.set_defaults(func=cmd_config)
