"""``son-of-anton "prompt"`` routes to single-query mode.

The parser declares ``chat`` as a subcommand and ``-q`` as its query flag; a
bare first positional was documented but never wired, so argparse rejected it
with "invalid choice". ``_rewrite_bare_prompt`` injects the subcommand and
query flag while leaving real subcommand invocations and session-name flags
alone.
"""

from __future__ import annotations

from son_of_anton_cli._parser import build_top_level_parser
from son_of_anton_cli.main import (
    _first_positional_index,
    _rewrite_bare_prompt,
)

KNOWN = {"chat", "sessions", "model", "cron", "completion"}


def _parse(argv: list):
    parser, _subparsers, _chat = build_top_level_parser()
    return parser.parse_args(argv)


def test_bare_prompt_becomes_a_chat_query() -> None:
    rewritten = _rewrite_bare_prompt(["hello"], KNOWN)
    assert rewritten == ["chat", "-q", "hello"]
    args = _parse(rewritten)
    assert args.command == "chat"
    assert args.query == "hello"


def test_unquoted_multiword_prompt_is_joined() -> None:
    assert _rewrite_bare_prompt(["hello", "there"], KNOWN) == [
        "chat",
        "-q",
        "hello there",
    ]


def test_flags_before_and_after_the_prompt_are_preserved() -> None:
    rewritten = _rewrite_bare_prompt(["-m", "qwen", "hello", "--yolo"], KNOWN)
    assert rewritten == ["-m", "qwen", "chat", "-q", "hello", "--yolo"]
    args = _parse(rewritten)
    assert args.command == "chat"
    assert args.query == "hello"
    assert args.model == "qwen"
    assert args.yolo is True


def test_subcommands_and_session_flags_are_untouched() -> None:
    assert _rewrite_bare_prompt(["sessions", "list"], KNOWN) == ["sessions", "list"]
    assert _rewrite_bare_prompt(["-c", "auth notes"], KNOWN) == ["-c", "auth notes"]
    assert _rewrite_bare_prompt(["--resume", "latest"], KNOWN) == [
        "--resume",
        "latest",
    ]


def test_first_positional_index_skips_flag_values() -> None:
    assert _first_positional_index(["-m", "gpt5", "--provider", "openai", "chat"]) == 4
    assert _first_positional_index(["--version"]) is None
    assert _first_positional_index(["-c", "some name", "prompt"]) == 2
