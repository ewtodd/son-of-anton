"""Slash-command registry invariants — every consumer (CLI dispatch, gateway
dispatch, /help, autocomplete, menus) derives from COMMAND_REGISTRY, so the
registry itself must stay internally consistent.
"""

from __future__ import annotations

from son_of_anton_cli.commands import (
    COMMAND_REGISTRY,
    CommandDef,
    resolve_command,
)

VALID_CATEGORIES = {"Session", "Configuration", "Tools & Skills", "Info", "Exit"}


def test_registry_is_nonempty_and_typed() -> None:
    assert COMMAND_REGISTRY
    assert all(isinstance(cmd, CommandDef) for cmd in COMMAND_REGISTRY)


def test_names_are_unique_without_slash_prefix() -> None:
    names = [cmd.name for cmd in COMMAND_REGISTRY]
    assert len(names) == len(set(names)), "duplicate canonical names"
    for name in names:
        assert name and not name.startswith("/"), f"{name!r} must not carry a slash"


def test_categories_are_valid() -> None:
    for cmd in COMMAND_REGISTRY:
        assert cmd.category in VALID_CATEGORIES, f"{cmd.name}: bad category {cmd.category!r}"


def test_descriptions_are_present_and_bounded() -> None:
    for cmd in COMMAND_REGISTRY:
        assert cmd.description, f"{cmd.name}: missing description"
        # Upper bound keeps /help and menus renderable; exact lengths change
        # freely below it.
        assert len(cmd.description) <= 200, f"{cmd.name}: description too long"


def test_aliases_do_not_shadow_other_commands() -> None:
    canonical = {cmd.name for cmd in COMMAND_REGISTRY}
    for cmd in COMMAND_REGISTRY:
        for alias in cmd.aliases:
            assert alias not in canonical, f"alias {alias!r} collides with a command name"
            assert alias != cmd.name


def test_quit_command_resolves_via_q_and_colon_q() -> None:
    # The :q/q fix from the fork: quit must be reachable with or without
    # the colon prefix, and must never alias to `queue`.
    quit_cmd = resolve_command("q")
    assert quit_cmd is not None
    assert quit_cmd.name in {"quit", "exit", "q"}
    assert resolve_command(":q") is quit_cmd
    assert resolve_command("/q") is quit_cmd
    queue_cmd = resolve_command("queue")
    assert queue_cmd is None or queue_cmd is not quit_cmd


def test_fork_commands_exist() -> None:
    names = {cmd.name for cmd in COMMAND_REGISTRY}
    for expected in ("model", "perm", "skin", "cron", "curator", "help"):
        assert expected in names, f"missing /{expected}"


def test_gateway_commands_derive_from_registry() -> None:
    from son_of_anton_cli.commands import GATEWAY_KNOWN_COMMANDS

    # Every gateway-exposed command must still be a registered CommandDef.
    for name in GATEWAY_KNOWN_COMMANDS:
        assert resolve_command(name) is not None, f"gateway command {name!r} unregistered"


def test_gateway_help_core_is_a_short_subset_of_the_catalog() -> None:
    from son_of_anton_cli.commands import (
        GATEWAY_HELP_CORE,
        gateway_help_lines,
    )

    full = gateway_help_lines()
    core = gateway_help_lines(only=GATEWAY_HELP_CORE)
    assert core
    # The point of the core set: /help fits one message, not four pages.
    assert len(core) < len(full)
    # Every core name must be a real registered command; config gating is
    # applied uniformly by gateway_help_lines itself.
    canonical_names = {cmd.name for cmd in COMMAND_REGISTRY}
    assert GATEWAY_HELP_CORE <= canonical_names
