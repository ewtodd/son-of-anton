"""The CLI surface that exists: the problem subcommand, the help text, the gateway verbs.

The spec builder once lived in `scripts/`, which the sealed wheel does not ship
and which is on nobody's PATH; these tests assert it is reachable through the
real parser, and that `--help` only names commands that exist.
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def parser():
    """The real top-level parser, with the problem subcommand attached."""
    from son_of_anton_cli._parser import build_top_level_parser
    from son_of_anton_cli.main import cmd_problem
    from son_of_anton_cli.subcommands.problem import build_problem_parser

    root, subparsers, _chat = build_top_level_parser()
    build_problem_parser(subparsers, cmd_problem=cmd_problem)
    return root


# --- the problem subcommand ------------------------------------------------


def test_problem_create_is_reachable(parser) -> None:
    args = parser.parse_args(
        ["problem", "create", "--data", "/tmp", "--goal", "g", "-o", "/tmp/p.toml"]
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

    from autophysicist.spec_builder import add_arguments

    built = add_arguments(argparse.ArgumentParser())
    flags = {a for action in built._actions for a in action.option_strings}
    assert {"--data", "--goal", "--truth", "--no-llm", "--print-card"} <= flags


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


@pytest.fixture(scope="module")
def main_commands() -> set:
    """Every subcommand the real ``main()`` registers, read from its --help."""
    import contextlib
    import io
    import sys

    from son_of_anton_cli import main as cli_main

    out = io.StringIO()
    argv = sys.argv
    sys.argv = ["son-of-anton", "--help"]
    try:
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
            cli_main.main()
    finally:
        sys.argv = argv
    choices = re.search(r"\{([a-z,-]+)\}", out.getvalue())
    assert choices, "--help printed no subcommand list"
    return set(choices.group(1).split(","))


def test_every_command_in_the_help_text_exists(main_commands) -> None:
    missing = sorted(_epilogue_commands() - main_commands)
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
            "config.toml"
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


def test_the_session_name_splitter_lists_only_real_commands(main_commands) -> None:
    """A phantom name here truncates a session name at that word."""
    import inspect

    from son_of_anton_cli.main import _coalesce_session_name_args

    source = inspect.getsource(_coalesce_session_name_args)
    listed = set(re.findall(r'^\s+"([a-z-]+)",\s*$', source, re.MULTILINE))
    assert listed <= main_commands, (
        f"_SUBCOMMANDS names commands that do not exist: {sorted(listed - main_commands)}"
    )


# --- the spec builder's JSON parsing ---------------------------------------
#
# The task statement is several paragraphs. A model writing it into a JSON
# string emits real newlines rather than \n, and strict JSON rejects that at
# the first line break. The repair round-trip does not save it: re-emitting a
# multi-line string as escaped JSON is exactly what it just failed at, so a
# slow call buys the same error.


def test_raw_newlines_inside_a_string_are_accepted() -> None:
    from autophysicist.spec_builder import parse_json_object

    payload = '{"name": "psd", "problem": "Line one.\nLine two.", "checks": []}'
    parsed = parse_json_object(payload)
    assert parsed["problem"] == "Line one.\nLine two."
    assert parsed["name"] == "psd"


def test_a_fenced_object_with_raw_newlines_is_accepted() -> None:
    from autophysicist.spec_builder import parse_json_object

    payload = '```json\n{"problem": "a\nb", "checks": []}\n```'
    assert parse_json_object(payload)["problem"] == "a\nb"


def test_tabs_and_carriage_returns_too() -> None:
    from autophysicist.spec_builder import parse_json_object

    assert parse_json_object('{"problem": "a\tb\r\nc"}')["problem"] == "a\tb\r\nc"


def test_output_with_no_object_still_raises() -> None:
    from autophysicist.spec_builder import parse_json_object

    with pytest.raises(ValueError, match="no JSON object"):
        parse_json_object("I could not produce a spec for this dataset.")


def test_a_spec_with_no_checks_is_allowed() -> None:
    """Some goals have nothing scoreable without a reference run.

    A calibration gain or a classifier AUC cannot be known from a data
    listing. Forcing a check there yields a made-up expected value that
    reports PASS or FAIL against nothing — worse than admitting it is
    unscored.
    """
    from autophysicist.spec_builder import validate_spec

    spec = {
        "name": "psd_ml",
        "problem": "x" * 100 + " Write auc to RESULTS.txt.",
        "checks": [],
    }
    assert validate_spec(spec) == []


def test_a_check_on_a_key_the_task_never_asks_for_is_still_refused() -> None:
    from autophysicist.spec_builder import validate_spec

    spec = {
        "name": "psd_ml",
        "problem": "x" * 100 + " Write auc to RESULTS.txt.",
        "checks": [{"id": "e", "key": "cs137_kev", "expected": 661.7, "tolerance": 5.0}],
    }
    issues = validate_spec(spec)
    assert any("never asks the agent to write it" in i for i in issues)
