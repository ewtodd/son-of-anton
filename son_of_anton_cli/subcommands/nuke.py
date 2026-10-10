"""``son-of-anton nuke`` — delete ALL local state and start over.

Removes the entire ``SON_OF_ANTON_HOME`` tree: sessions (``state.db`` +
``sessions/``), logs, cron jobs, memory, skills, plugins, skins, config.toml
and the ``.env`` secrets. There is no ``--yes``: the user must type a full
confirmation sentence, so it cannot be triggered by a stray keystroke or a
script.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from son_of_anton_constants import display_son_of_anton_home, get_son_of_anton_home

CONFIRM_PHRASE = "Yes, I really want to delete everything."


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    size = float(num_bytes)
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024.0
        if size < 1024.0 or unit == "GiB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} GiB"


def _measure(root: Path) -> tuple[int, int]:
    files = 0
    total = 0
    for child in root.rglob("*"):
        try:
            if child.is_file():
                files += 1
                total += child.stat().st_size
        except OSError:
            continue
    return files, total


def cmd_nuke(args: argparse.Namespace) -> int:
    """Delete the whole Son of Anton home after an explicit confirmation."""
    home = Path(get_son_of_anton_home())
    shown = display_son_of_anton_home()
    if not home.exists():
        print(f"Nothing to nuke — {shown} does not exist.")
        return 0
    files, total = _measure(home)
    print("This deletes ALL local Son of Anton state:")
    print("  sessions, logs, cron jobs, memory, skills, plugins, skins, config and secrets.")
    print(
        f"Target: {shown}  ({files} file{'s' if files != 1 else ''}, {_format_size(total)})"
    )
    print()
    print("Type this exactly to proceed:")
    print(f"  {CONFIRM_PHRASE}")
    if not sys.stdin.isatty():
        print("Refusing to nuke without an interactive terminal — nothing was deleted.")
        return 1
    try:
        answer = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("Cancelled — nothing was deleted.")
        return 1
    if answer != CONFIRM_PHRASE:
        print("Phrase did not match — nothing was deleted.")
        return 1
    try:
        shutil.rmtree(home)
    except OSError as exc:
        print(f"Failed to delete {shown}: {exc}")
        return 1
    print(f"Deleted {shown}. A fresh home is created on the next launch.")
    return 0


def build_nuke_parser(subparsers) -> None:
    """Attach the ``nuke`` subcommand to ``subparsers``."""
    nuke_parser = subparsers.add_parser(
        "nuke",
        help="Delete ALL local Son of Anton state (sessions, config, secrets, ...)",
        description=(
            "Delete the entire SON_OF_ANTON_HOME tree — sessions, logs, cron "
            "jobs, memory, skills, plugins, skins, config.toml and .env. "
            "Requires typing a full confirmation sentence; there is no --yes."
        ),
    )
    nuke_parser.set_defaults(func=cmd_nuke)
