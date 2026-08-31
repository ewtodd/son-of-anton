"""Two CLI surfaces that existed but could not be reached.

`son_of_anton_cli/completion.py` generated bash/zsh/fish scripts by walking the
live argparse tree, and `cmd_completion` was there to print them — but nothing
registered a `completion` subparser, so the command answered
``invalid choice: 'completion'`` and the whole feature was dead. The spec
builder had the mirror-image problem: it worked, but it lived in `scripts/`,
which the sealed wheel does not ship and which is on nobody's PATH.

These tests assert both are reachable through the real parser, and that the
generated scripts track the command tree rather than a hardcoded list.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

import pytest

from son_of_anton_cli.completion import generate_bash, generate_fish, generate_zsh
from son_of_anton_cli.subcommands.completion import SHELLS


@pytest.fixture(scope="module")
def parser():
    """The real top-level parser, with every subcommand attached."""
    from son_of_anton_cli._parser import build_top_level_parser
    from son_of_anton_cli.main import cmd_completion, cmd_problem
    from son_of_anton_cli.subcommands.completion import build_completion_parser
    from son_of_anton_cli.subcommands.problem import build_problem_parser

    root, subparsers, _chat = build_top_level_parser()
    build_problem_parser(subparsers, cmd_problem=cmd_problem)
    build_completion_parser(subparsers, root, cmd_completion=cmd_completion)
    return root


def test_completion_is_a_real_subcommand(parser) -> None:
    args = parser.parse_args(["completion", "zsh"])
    assert args.command == "completion"
    assert args.shell == "zsh"
    assert callable(args.func)


def test_completion_defaults_to_bash(parser) -> None:
    assert parser.parse_args(["completion"]).shell == "bash"


def test_completion_rejects_an_unknown_shell(parser) -> None:
    with pytest.raises(SystemExit):
        parser.parse_args(["completion", "powershell"])


@pytest.mark.parametrize("shell", SHELLS)
def test_every_shell_generates_a_non_empty_script(parser, shell) -> None:
    script = {"bash": generate_bash, "zsh": generate_zsh, "fish": generate_fish}[
        shell
    ](parser)
    assert script.strip(), f"{shell} completion generated nothing"
    assert "son-of-anton" in script


def test_the_generated_script_tracks_the_live_command_tree(parser) -> None:
    """A hardcoded list goes stale the first time a subcommand is added.

    Asserted against the parser's own subcommand table rather than a list
    written here, which would be the very thing this is guarding against.
    """
    import argparse

    script = generate_bash(parser)
    commands = [
        choice
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for choice in action.choices
    ]
    assert commands, "the fixture attached no subcommands"
    for command in commands:
        assert command in script, f"{command} missing from the completion script"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_the_bash_script_is_valid_bash(parser, tmp_path) -> None:
    path = tmp_path / "son-of-anton"
    path.write_text(generate_bash(parser), encoding="utf-8")
    result = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


# --- the problem subcommand ------------------------------------------------


def test_problem_create_is_reachable(parser) -> None:
    args = parser.parse_args(
        ["problem", "create", "--data", "/tmp", "--goal", "g", "-o", "/tmp/p.yaml"]
    )
    assert args.command == "problem"
    assert args.problem_action == "create"
    assert args.data == ["/tmp"]
    assert callable(args.func)


def test_problem_create_requires_its_inputs(parser) -> None:
    with pytest.raises(SystemExit):
        parser.parse_args(["problem", "create"])


def test_bare_problem_explains_itself(parser, capsys) -> None:
    from son_of_anton_cli.main import cmd_problem

    args = parser.parse_args(["problem"])
    assert cmd_problem(args) == 2
    assert "problem create" in capsys.readouterr().out


def test_the_options_come_from_the_builder_itself() -> None:
    """One definition, so the subcommand and `python -m` cannot drift apart."""
    import argparse

    from physics_intern.spec_builder import add_arguments

    built = add_arguments(argparse.ArgumentParser())
    flags = {a for action in built._actions for a in action.option_strings}
    assert {"--data", "--goal", "--truth", "--no-llm", "--print-card"} <= flags


def test_the_builder_still_runs_standalone() -> None:
    """`python -m physics_intern.spec_builder --help` must keep working."""
    result = subprocess.run(
        [sys.executable, "-m", "physics_intern.spec_builder", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--data" in result.stdout


# --- the help text must describe the CLI that exists ------------------------
#
# `--help` advertised sixteen commands this fork does not have — setup, logout,
# auth, fallback, dashboard, update, console, debug, logs, import, backup,
# memory, security, plugins, claw, dump — because the epilogue was inherited
# from upstream and never trimmed. Every one answered `invalid choice`.


def _registered_commands(parser) -> set[str]:
    import argparse

    return {
        choice
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for choice in action.choices
    }


def _epilogue_commands() -> set[str]:
    """The subcommand named in each `son-of-anton <word>` example line."""
    from son_of_anton_cli._parser import _EPILOGUE

    found = set()
    for line in _EPILOGUE.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != "son-of-anton":
            continue
        word = parts[1]
        # Flags, the "son-of-anton <command> --help" placeholder, and the bare
        # "son-of-anton   Start interactive chat" line are not subcommands.
        if word.startswith(("-", "<")) or not word.islower():
            continue
        found.add(word)
    return found


def test_every_command_in_the_help_text_exists(parser) -> None:
    from son_of_anton_cli.main import _BUILTIN_SUBCOMMANDS

    # The fixture attaches a subset; check against everything main() registers.
    known = _registered_commands(parser) | set(_BUILTIN_SUBCOMMANDS)
    missing = sorted(_epilogue_commands() - known)
    assert not missing, (
        f"--help advertises commands that do not exist: {missing}. Every one "
        "answers 'invalid choice' when a user tries it."
    )


def test_the_help_text_does_not_advertise_installing_anything() -> None:
    """This is not an imperative program — the deployment is declared."""
    from son_of_anton_cli._parser import _EPILOGUE

    lowered = _EPILOGUE.lower()
    for banned in ("setup", "install", "uninstall", "update", "logout"):
        assert banned not in lowered, (
            f"--help still offers {banned!r}; the NixOS/Home Manager module "
            "owns the install, systemctl owns the service, and `settings` owns "
            "config.yaml"
        )


def test_the_gateway_has_no_service_lifecycle_verbs() -> None:
    import argparse

    from son_of_anton_cli.subcommands.gateway import build_gateway_parser

    root = argparse.ArgumentParser()
    build_gateway_parser(root.add_subparsers(dest="command"), cmd_gateway=lambda a: 0)
    actions = _registered_commands(root)
    assert "gateway" in actions
    gateway = [
        a for a in root._actions if isinstance(a, argparse._SubParsersAction)
    ][0].choices["gateway"]
    verbs = _registered_commands(gateway)
    assert verbs == {"run", "status"}, (
        f"gateway offers {sorted(verbs)}; install/uninstall/start/stop/restart/"
        "setup/migrate-legacy are the module's and systemctl's job"
    )


def test_the_session_name_splitter_lists_only_real_commands(parser) -> None:
    """A phantom name here truncates a session name at that word."""
    import inspect

    from son_of_anton_cli.main import _BUILTIN_SUBCOMMANDS, _coalesce_session_name_args

    source = inspect.getsource(_coalesce_session_name_args)
    listed = set(re.findall(r'^\s+"([a-z-]+)",\s*$', source, re.MULTILINE))
    known = _registered_commands(parser) | set(_BUILTIN_SUBCOMMANDS)
    assert listed <= known, (
        f"_SUBCOMMANDS names commands that do not exist: {sorted(listed - known)}"
    )
