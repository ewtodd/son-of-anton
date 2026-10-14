"""``son-of-anton mcp`` subcommand parser.

Extracted from ``son_of_anton_cli/main.py:main()`` (god-file Phase 2 follow-up).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable

from son_of_anton_cli.subcommands._shared import add_accept_hooks_flag


def build_mcp_parser(subparsers, *, cmd_mcp: Callable) -> None:
    """Attach the ``mcp`` subcommand to ``subparsers``."""
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Inspect declared MCP servers and run Son of Anton as an MCP server",
        description=(
            "Inspect the MCP servers declared in config.toml and run Son of Anton\n"
            "as an MCP server.\n\n"
            "Servers are declared under mcp_servers in config.toml (e.g. by the Nix\n"
            "module's settings); Son of Anton never installs or adds one itself.\n"
            "Use 'son-of-anton mcp serve' to expose Son of Anton conversations over MCP."
        ),
    )
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_action")

    mcp_serve_p = mcp_sub.add_parser(
        "serve",
        help="Run Son of Anton as an MCP server (expose conversations to other agents)",
    )
    mcp_serve_p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging on stderr",
    )
    add_accept_hooks_flag(mcp_serve_p)

    mcp_sub.add_parser("list", aliases=["ls"], help="List configured MCP servers")

    mcp_test_p = mcp_sub.add_parser("test", help="Test MCP server connection")
    mcp_test_p.add_argument("name", help="Server name to test")

    mcp_login_p = mcp_sub.add_parser(
        "login",
        help="Force re-authentication for an OAuth-based MCP server",
    )
    mcp_login_p.add_argument("name", help="Server name to re-authenticate")

    mcp_reauth_p = mcp_sub.add_parser(
        "reauth",
        help="Re-authenticate one OAuth MCP server, or all of them (--all)",
    )
    mcp_reauth_p.add_argument(
        "name", nargs="?", help="Server name to re-authenticate (omit with --all)"
    )
    mcp_reauth_p.add_argument(
        "--all",
        action="store_true",
        help="Re-authenticate every OAuth server in config, one at a time",
    )

    add_accept_hooks_flag(mcp_parser)
    mcp_parser.set_defaults(func=cmd_mcp)
