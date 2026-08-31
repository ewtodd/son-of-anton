"""``son-of-anton completion`` subcommand parser.

``son_of_anton_cli/completion.py`` has generated bash/zsh/fish scripts by
walking the live argparse tree for some time, and ``cmd_completion`` has been
there to print them — but no subparser ever registered the command, so
``son-of-anton completion bash`` answered ``invalid choice: 'completion'`` and
the whole feature was unreachable. Registering it is also what lets the package
generate the scripts at build time and install them.

The generator needs the ROOT parser, not this subparser — that is the tree it
walks — so the handler is bound to it here rather than relying on the
dispatcher, which calls ``args.func(args)`` and has no parser to give.
"""

from __future__ import annotations

import argparse
import functools
from typing import Callable

SHELLS = ("bash", "zsh", "fish")


def build_completion_parser(
    subparsers,
    root_parser: argparse.ArgumentParser,
    *,
    cmd_completion: Callable,
) -> None:
    """Attach the ``completion`` subcommand to *subparsers*."""
    completion_parser = subparsers.add_parser(
        "completion",
        help="Print a shell completion script (bash, zsh, or fish)",
        description=(
            "Print a completion script for your shell.\n\n"
            "The script is generated from the live command tree, so it never "
            "goes stale against the CLI it completes.\n\n"
            "  son-of-anton completion bash > "
            "~/.local/share/bash-completion/completions/son-of-anton\n"
            "  son-of-anton completion zsh  > "
            "~/.zsh/completions/_son-of-anton\n"
            "  son-of-anton completion fish > "
            "~/.config/fish/completions/son-of-anton.fish\n\n"
            "Nix installs all three with the package; this is for other setups "
            "and for regenerating after a local change."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    completion_parser.add_argument(
        "shell",
        nargs="?",
        default="bash",
        choices=list(SHELLS),
        help="Shell to generate for (default: bash)",
    )
    completion_parser.set_defaults(
        func=functools.partial(cmd_completion, parser=root_parser)
    )
