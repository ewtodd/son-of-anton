"""Guards for the auto-router excision.

A chat message must never start a physics (Autophysicist) run. Physics is
driven explicitly — ``son-of-anton problem create/run``, or an agent calling
that subcommand through its terminal. The keyword router, the ``/mode`` pin,
and the chat/gateway physics dispatch were the ways a first message could
silently enter the one-shot stateless loop. These tests fail if any of that
machinery comes back.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories with their own dependency story — vendored code and build output.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "result"}


def _python_files() -> list[Path]:
    return [
        path
        for path in REPO_ROOT.rglob("*.py")
        if not SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts)
    ]


def test_router_module_is_gone() -> None:
    """The classifier module must not become importable again."""
    try:
        importlib.import_module("son_of_anton_cli.router")
    except ImportError:
        return
    raise AssertionError("son_of_anton_cli.router is importable again")


def test_router_is_not_in_default_config() -> None:
    """The config section must not come back — it is the router's on/off."""
    from son_of_anton_cli.config_defaults import DEFAULT_CONFIG

    assert "router" not in DEFAULT_CONFIG, (
        "the router config section is back — chat messages would "
        "classify again"
    )


def test_mode_command_is_gone() -> None:
    """Every dispatch surface derives from COMMAND_REGISTRY."""
    from son_of_anton_cli.commands import COMMAND_REGISTRY

    assert not any(c.name == "mode" for c in COMMAND_REGISTRY), (
        "/mode returned — the session agent-mode pin is part of the "
        "auto-router surface"
    )


# Names that only ever belonged to the auto-router or its dispatch sites.
# ``_agent_mode`` is included: that name specifically means the
# standard/physics agent-mode pin, so its return is the router's return.
FORBIDDEN_NAMES = (
    "classify_mode",
    "resolve_mode",
    "resolve_enabled_modes",
    "PHYSICS_KEYWORDS",
    "_run_physics_mode",
    "_run_physics_mode_sync",
    "_run_physics_mode_turn",
    "_resolve_session_agent_mode",
    "_agent_mode",
)


def test_no_source_references_the_router_machinery() -> None:
    """AST scan: no use of the router's symbols anywhere in the tree.

    Scanning parsed ASTs (like test_no_profiles) instead of raw text: a
    docstring mention of a removed symbol must not fail the build. A
    ``getattr(x, "name")`` string is not an AST Name/Attribute, so it is
    not caught — every such use was removed with the excision anyway.
    """
    offenders: list[str] = []
    for path in _python_files():
        if path.name == "test_no_auto_router.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            if name in FORBIDDEN_NAMES:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} {name}"
                )
    assert not offenders, "router machinery reappeared:\n" + "\n".join(offenders)
