"""Skills are not slash commands, and nothing else lets the user invoke one.

An installed skill used to become ``/<skill-name>`` (and stacked
``/a /b ...`` invocations, bundles, ``/learn``): typing it expanded the turn
into the skill's whole body. That surface is gone, and so is ``/skills``
(hub search/install, staged skill-write review) with the code behind it. A
slash word that happens to name a skill on disk is now just an unknown
command — nothing is injected into the conversation, and no command list
offers it.
"""

from __future__ import annotations

import pytest


def _install_skill(home, name: str) -> None:
    skill_dir = home / "skills" / "demo" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name = \"{name}\"\n"
        "description = \"Demo skill for the slash-command guard.\"\n"
        "---\n\n"
        f"# {name}\n\nDo the demo thing.\n",
        encoding="utf-8",
    )


@pytest.fixture
def cli(monkeypatch, son_of_anton_home):
    _install_skill(son_of_anton_home, "demo-skill")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    # No startup prewarms: background threads would write under the temp home
    # and race its cleanup.
    monkeypatch.setenv("SON_OF_ANTON_DEFER_AGENT_STARTUP", "1")
    from cli import SonOfAntonCLI

    instance = SonOfAntonCLI(
        model="test-model",
        provider="openai",
        api_key="sk-test",
        base_url="https://example.invalid/v1",
    )
    try:
        yield instance
    finally:
        db = getattr(instance, "_session_db", None)
        if db is not None:
            db.close()


def test_a_skill_name_typed_as_a_slash_command_is_unknown(cli, capsys) -> None:
    assert cli.process_command("/demo-skill fix the title leak") is True

    assert cli._pending_input.empty(), "a skill body was queued as a user turn"
    out = capsys.readouterr().out
    assert "Unknown command" in out
    assert "Do the demo thing" not in out


def test_the_command_palette_never_offers_a_skill(cli) -> None:
    offered = {entry[0] for entry in cli._build_command_palette_entries()}
    assert offered, "the palette lists the built-in commands"
    assert "/demo-skill" not in offered


# Modules that only existed to invoke, install, sync, scan or manage skills.
REMOVED_MODULES = (
    "agent.skill_commands",
    "agent.skill_bundles",
    "agent.skill_preprocessing",
    "agent.learn_prompt",
    "agent.prompt_cache_boundary",
    "agent.skill_utils",
    "tools.skills_tool",
    "tools.skill_manager_tool",
    "tools.skills_hub",
    "tools.skills_sync",
    "tools.skills_guard",
    "tools.skills_ast_audit",
    "tools.skillevaluator_scan",
    "tools.skill_usage",
    "tools.skill_ledger",
    "tools.skill_linter",
    "tools.skill_provenance",
    "son_of_anton_cli.skills_hub",
    "son_of_anton_cli.skills_config",
    "son_of_anton_cli.subcommands.skills",
    "son_of_anton_cli.callbacks",
    "son_of_anton_cli.prompt_size",
)


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_skill_modules_are_gone(module: str) -> None:
    import importlib

    try:
        importlib.import_module(module)
    except ImportError:
        return
    raise AssertionError(f"{module} is importable again")


@pytest.mark.parametrize(
    "name", ("bundles", "learn", "reload-skills", "reload_skills", "skills")
)
def test_no_command_invokes_rescans_or_manages_skills(name: str) -> None:
    from son_of_anton_cli.commands import resolve_command

    assert resolve_command(name) is None, f"/{name} is registered again"


@pytest.mark.parametrize(
    "argv", (["--skills", "demo-skill"], ["chat", "--skills", "demo-skill", "-q", "hi"])
)
def test_no_skills_preload_flag(argv: list) -> None:
    from son_of_anton_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(argv)
