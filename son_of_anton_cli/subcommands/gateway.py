"""``son-of-anton gateway`` subcommand parser.

Two actions: ``run`` — the foreground gateway, which is also what a bare
``son-of-anton gateway`` does and what the systemd unit's ExecStart invokes —
and ``status``. The unit is declared by the NixOS or Home Manager module and
``systemctl`` starts and stops it, so there are no service lifecycle verbs.
"""

from __future__ import annotations

from typing import Callable

from son_of_anton_cli.subcommands._shared import add_accept_hooks_flag


def build_gateway_parser(
    subparsers, *, cmd_gateway: Callable
) -> None:
    """Attach the ``gateway`` subcommand to ``subparsers``."""
    gateway_parser = subparsers.add_parser(
        "gateway",
        help="Messaging gateway management",
        description="Run or inspect the messaging gateway (Signal + cron)",
    )
    gateway_subparsers = gateway_parser.add_subparsers(dest="gateway_command")

    # gateway run (default)
    gateway_run = gateway_subparsers.add_parser(
        "run", help="Run gateway in foreground"
    )
    gateway_run.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase stderr log verbosity (-v=INFO, -vv=DEBUG)",
    )
    gateway_run.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress all stderr log output"
    )
    gateway_run.add_argument(
        "--replace",
        action="store_true",
        help="Replace any existing gateway instance (useful for systemd)",
    )
    add_accept_hooks_flag(gateway_run)
    add_accept_hooks_flag(gateway_parser)

    gateway_subparsers.add_parser("status", help="Show gateway status")

    gateway_parser.set_defaults(func=cmd_gateway)
