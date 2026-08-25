"""
Unified self-relaunch for Son of Anton CLI.

Preserves critical flags (--profile, --model, etc.) across
process replacement so that ``son-of-anton sessions browse`` or post-setup relaunch
doesn't silently drop the user's UI mode or other preferences.

Also works when ``son-of-anton`` is not on PATH (e.g. ``nix run`` or ``python -m``).
"""

import os
import shutil
import sys
from typing import Optional, Sequence

from son_of_anton_cli._parser import (
    PRE_ARGPARSE_INHERITED_FLAGS,
    build_top_level_parser,
)


def _build_inherited_flag_table() -> list[tuple[str, bool]]:
    """Build the ``(option_string, takes_value)`` table of flags that must
    survive a self-relaunch, by introspecting the real parser used by
    ``son-of-anton`` itself.

    A flag participates if its argparse Action carries
    ``inherit_on_relaunch = True`` — set by ``_parser._inherited_flag``.
    """
    parser, _subparsers, chat_parser = build_top_level_parser()

    table: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()
    for p in (parser, chat_parser):
        for action in p._actions:
            if not action.option_strings:
                continue  # positional / no flag form
            if not getattr(action, "inherit_on_relaunch", False):
                continue
            takes_value = action.nargs != 0  # store_true/false set nargs=0
            for opt in action.option_strings:
                key = (opt, takes_value)
                if key not in seen:
                    seen.add(key)
                    table.append(key)

    table.extend(PRE_ARGPARSE_INHERITED_FLAGS)
    return table


_INHERITED_FLAGS_TABLE = _build_inherited_flag_table()


def _extract_inherited_flags(argv: Sequence[str]) -> list[str]:
    """Pull out flags that should carry over into a self-relaunched son-of-anton."""
    flags: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if "=" in arg:
            key = arg.split("=", 1)[0]
            for flag, _ in _INHERITED_FLAGS_TABLE:
                if key == flag:
                    flags.append(arg)
                    break
            i += 1
            continue

        for flag, takes_value in _INHERITED_FLAGS_TABLE:
            if arg == flag:
                flags.append(arg)
                if takes_value and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                    flags.append(argv[i + 1])
                    i += 1
                break
        i += 1
    return flags


def resolve_son_of_anton_bin() -> Optional[str]:
    """Find the son-of-anton entry point.

    Priority:
      1. ``sys.argv[0]`` if it resolves to a real executable.
      2. ``shutil.which("son-of-anton")`` on PATH.
      3. ``None`` → caller should fall back to ``python -m son_of_anton_cli.main``.
    """
    argv0 = sys.argv[0]

    # Absolute path to an executable (covers nix store, venv wrappers, etc.)
    if os.path.isabs(argv0) and os.path.isfile(argv0) and os.access(argv0, os.X_OK):
        return argv0

    # Relative path — resolve against CWD
    if not argv0.startswith("-") and os.path.isfile(argv0):
        abs_path = os.path.abspath(argv0)
        if os.access(abs_path, os.X_OK):
            return abs_path

    # PATH lookup
    path_bin = shutil.which("son-of-anton")
    if path_bin:
        return path_bin

    return None


def build_relaunch_argv(
    extra_args: Sequence[str],
    *,
    preserve_inherited: bool = True,
    original_argv: Optional[Sequence[str]] = None,
) -> list[str]:
    """Construct an argv list for replacing the current process with son-of-anton.

    Args:
        extra_args: Arguments to append (e.g. ``["--resume", id]``).
        preserve_inherited: Whether to carry over UI / behaviour flags
            tagged with ``inherit_on_relaunch`` in the parser.
        original_argv: The original argv to scan for flags (defaults to
            ``sys.argv[1:]``).
    """
    bin_path = resolve_son_of_anton_bin()

    if bin_path:
        argv = [bin_path]
    else:
        argv = [sys.executable, "-m", "son_of_anton_cli.main"]

    src = list(original_argv) if original_argv is not None else list(sys.argv[1:])

    if preserve_inherited:
        argv.extend(_extract_inherited_flags(src))

    argv.extend(extra_args)
    return argv


def relaunch(
    extra_args: Sequence[str],
    *,
    preserve_inherited: bool = True,
    original_argv: Optional[Sequence[str]] = None,
) -> None:
    """Replace the current process with a fresh son-of-anton invocation.

    Uses ``os.execvp`` which replaces the running process with the new one
    in place — same PID, no double-fork.  That's what the relaunch contract
    wants: "run son-of-anton again as if the user had typed the new argv".
    """
    new_argv = build_relaunch_argv(
        extra_args, preserve_inherited=preserve_inherited, original_argv=original_argv
    )
    os.execvp(new_argv[0], new_argv)