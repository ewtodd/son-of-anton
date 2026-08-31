"""``son-of-anton gateway`` subcommand parser.

Two actions: ``run`` — the foreground gateway, which is also what a bare
``son-of-anton gateway`` does and what the systemd unit's ExecStart invokes —
and ``status``.

The service lifecycle verbs (``install``, ``uninstall``, ``start``, ``stop``,
``restart``, ``setup``, ``migrate-legacy``) are deliberately gone. This is not
an imperative program: the unit is declared by the NixOS or Home Manager
module, ``systemctl`` starts and stops it, and ``settings`` configures the
platforms. They already refused to run under managed mode — but leaving them in
the parser meant they were still advertised in ``--help``, offered by shell
completion, and read as things the CLI can do.
"""

from __future__ import annotations

import argparse
from typing import Callable

from son_of_anton_cli.subcommands._shared import add_accept_hooks_flag


def _add_compat_platform_flag(parser: argparse.ArgumentParser) -> None:
    """Accept stale `gateway <verb> --platform X` docs without advertising it.

    Gateway commands operate on the gateway process, not a single messaging
    adapter.  Keep the flag parseable so users following the old hint don't get
    blocked by argparse before the gateway can start.
    """
    parser.add_argument(
        "--platform",
        dest="platform",
        help=argparse.SUPPRESS,
    )


def build_gateway_parser(
    subparsers, *, cmd_gateway: Callable
) -> None:
    """Attach the ``gateway`` and ``proxy`` subcommands to ``subparsers``."""
    # =========================================================================
    # gateway command
    # =========================================================================
    gateway_parser = subparsers.add_parser(
        "gateway",
        help="Messaging gateway management",
        description="Manage the messaging gateway (Discord, Slack, Signal)",
    )
    gateway_subparsers = gateway_parser.add_subparsers(dest="gateway_command")

    # gateway run (default)
    gateway_run = gateway_subparsers.add_parser(
        "run", help="Run gateway in foreground (recommended for WSL, Docker, Termux)"
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
    gateway_run.add_argument(
        "--force",
        action="store_true",
        help=(
            "Start a foreground gateway even when a systemd/launchd/s6 service "
            "already supervises this profile. Without --force, the command "
            "refuses because a second dispatcher escapes the service and can "
            "corrupt shared gateway state."
        ),
    )
    gateway_run.add_argument(
        "--no-supervise",
        action="store_true",
        help=(
            "Inside the s6-overlay Docker image, normally `gateway run` is "
            "automatically redirected to the supervised s6 service (so the "
            "gateway gets auto-restart on crash, plus a supervised dashboard "
            "if SON_OF_ANTON_DASHBOARD is set). Pass --no-supervise to opt out and "
            "get the historical pre-s6 foreground behavior: the gateway is "
            "the container's main process and the container exits with the "
            "gateway's exit code. No effect outside an s6 container."
        ),
    )
    gateway_run.add_argument(
        "--external-supervisor",
        action="store_true",
        help=(
            "Declare that an external process manager owns this foreground "
            "gateway. In-chat restarts and updates exit back to that manager "
            "instead of spawning a detached replacement. Use this when a "
            "launchd/systemd wrapper strips its native environment markers."
        ),
    )
    add_accept_hooks_flag(gateway_run)
    add_accept_hooks_flag(gateway_parser)

    # gateway status
    gateway_status = gateway_subparsers.add_parser("status", help="Show gateway status")
    gateway_status.add_argument("--deep", action="store_true", help="Deep status check")
    gateway_status.add_argument(
        "-l",
        "--full",
        action="store_true",
        help="Show full, untruncated service/log output where supported",
    )
    gateway_status.add_argument(
        "--system",
        action="store_true",
        help="Target the Linux system-level gateway service",
    )
    _add_compat_platform_flag(gateway_status)

    gateway_parser.set_defaults(func=cmd_gateway)
