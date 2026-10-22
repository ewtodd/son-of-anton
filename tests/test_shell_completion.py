"""Shell completion: the generated scripts, executed, must agree with the parser.

The scripts are generated from the same parser ``main()`` parses with, so the
expectations here are derived from that parser, not from a list of commands.
The bash script is run in a real bash and its COMPREPLY inspected.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess

import pytest

from son_of_anton_cli.completion import SHELLS, generate


@pytest.fixture(scope="module")
def parser():
    from son_of_anton_cli.main import build_parser

    return build_parser()[0]


@pytest.fixture(scope="module")
def bash_script(parser, tmp_path_factory):
    path = tmp_path_factory.mktemp("completion") / "son-of-anton.bash"
    path.write_text(generate(parser, "bash"), encoding="utf-8")
    return path


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _canonical(action) -> dict[str, argparse.ArgumentParser]:
    """Subcommand name -> parser, aliases dropped (first name per parser)."""
    out: dict[str, argparse.ArgumentParser] = {}
    for name, sub in action.choices.items():
        if all(sub is not seen for seen in out.values()):
            out[name] = sub
    return out


def complete(script, *words: str) -> list[str]:
    """COMPREPLY after pressing Tab with the cursor on the last word."""
    line = " ".join(words)
    cmd = (
        f"source {script}; "
        f"COMP_WORDS=({' '.join(repr(w) for w in words)}); "
        f"COMP_CWORD={len(words) - 1}; COMP_LINE={line!r}; "
        "COMP_POINT=${#COMP_LINE}; _son_of_anton_completion; "
        'printf "%s\\n" "${COMPREPLY[@]}"'
    )
    done = subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, check=True, timeout=30
    )
    return [ln for ln in done.stdout.splitlines() if ln]


def test_top_level_subcommands_complete_on_empty_word(parser, bash_script) -> None:
    expected = set(_canonical(_subparsers(parser)))
    assert expected  # the parser really has subcommands
    assert set(complete(bash_script, "son-of-anton", "")) == expected


def test_prefix_narrows(parser, bash_script) -> None:
    names = sorted(_canonical(_subparsers(parser)))
    prefix = names[0][:2]
    got = complete(bash_script, "son-of-anton", prefix)
    assert got == [n for n in names if n.startswith(prefix)]


def test_every_nested_level_lists_its_subcommands(parser, bash_script) -> None:
    checked = 0

    def visit(p, path):
        nonlocal checked
        sub = _subparsers(p)
        if sub is None:
            return
        children = _canonical(sub)
        if path:
            got = complete(bash_script, "son-of-anton", *path, "")
            assert set(got) == set(children), path
            checked += 1
        for name, child in children.items():
            visit(child, path + [name])

    visit(parser, [])
    assert checked  # at least one command has nested subcommands


def test_dash_lists_options_at_every_level(parser, bash_script) -> None:
    got = complete(bash_script, "son-of-anton", "-")
    assert "--help" in got and "-h" in got
    for name, sub in _canonical(_subparsers(parser)).items():
        opts = {
            o
            for a in sub._actions
            if a.option_strings and a.help != argparse.SUPPRESS
            for o in a.option_strings
        }
        assert set(complete(bash_script, "son-of-anton", name, "-")) == opts, name


def test_choices_complete_positionally_and_after_options(bash_script) -> None:
    assert set(complete(bash_script, "son-of-anton", "completion", "")) == set(SHELLS)
    # a value-taking root option does not hide the subcommand after it
    assert "sessions" in complete(bash_script, "son-of-anton", "-m", "x", "")


def test_option_value_choices(parser, tmp_path) -> None:
    p = argparse.ArgumentParser(prog="son-of-anton")
    sp = p.add_subparsers(dest="command")
    leaf = sp.add_parser("thing")
    leaf.add_argument("--mode", choices=["fast", "slow"])
    leaf.add_argument("--flag", action="store_true")
    leaf.add_argument("kind", choices=["a", "b"])
    path = tmp_path / "c.bash"
    path.write_text(generate(p, "bash"), encoding="utf-8")
    assert set(complete(path, "son-of-anton", "thing", "--mode", "")) == {"fast", "slow"}
    assert set(complete(path, "son-of-anton", "thing", "")) == {"a", "b"}
    # a flag consumes nothing: the choice positional is still next
    assert set(complete(path, "son-of-anton", "thing", "--flag", "")) == {"a", "b"}


def test_plain_positional_falls_back_to_filenames(parser, tmp_path) -> None:
    p = argparse.ArgumentParser(prog="son-of-anton")
    sp = p.add_subparsers(dest="command")
    sp.add_parser("run").add_argument("spec")
    path = tmp_path / "c.bash"
    path.write_text(generate(p, "bash"), encoding="utf-8")
    (tmp_path / "problem.toml").write_text("", encoding="utf-8")
    got = complete(path, "son-of-anton", "run", str(tmp_path / "prob"))
    assert got == [str(tmp_path / "problem.toml")]


def test_completion_subcommand_prints_the_script(parser) -> None:
    from son_of_anton_cli.main import cmd_completion

    args = parser.parse_args(["completion", "zsh"])
    assert args.command == "completion" and args.shell == "zsh"
    assert args.func.keywords["parser"] is parser
    assert cmd_completion  # the handler the parser is bound to exists


def test_bare_completion_argv_is_not_a_chat_prompt(parser) -> None:
    from son_of_anton_cli.main import _rewrite_bare_prompt

    known = set(_subparsers(parser).choices)
    assert _rewrite_bare_prompt(["completion", "bash"], known) == ["completion", "bash"]


def test_unknown_shell_is_rejected(parser) -> None:
    with pytest.raises(ValueError):
        generate(parser, "powershell")


@pytest.mark.parametrize("shell", SHELLS)
def test_scripts_are_nonempty_and_static(parser, shell) -> None:
    script = generate(parser, shell)
    assert script.strip()
    assert "son-of-anton" in script
    assert "python" not in script.lower()


def test_bash_script_parses(bash_script) -> None:
    subprocess.run(["bash", "-n", str(bash_script)], check=True)


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_zsh_script_parses(parser, tmp_path) -> None:
    path = tmp_path / "_son-of-anton"
    path.write_text(generate(parser, "zsh"), encoding="utf-8")
    subprocess.run(["zsh", "-n", str(path)], check=True)


@pytest.mark.skipif(shutil.which("fish") is None, reason="fish not installed")
def test_fish_script_parses(parser, tmp_path) -> None:
    path = tmp_path / "son-of-anton.fish"
    path.write_text(generate(parser, "fish"), encoding="utf-8")
    subprocess.run(["fish", "--no-execute", str(path)], check=True)
