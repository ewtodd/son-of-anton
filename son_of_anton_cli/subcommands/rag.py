"""``son-of-anton rag`` subcommand parser (retrieval index management).

The handler is injected so this module does not import ``main`` (cycle
avoidance), matching the cron/skills pattern.
"""

from __future__ import annotations

from typing import Callable


def build_rag_parser(subparsers, *, cmd_rag: Callable) -> None:
    """Attach the ``rag`` subcommand (and its sub-actions) to ``subparsers``."""
    rag_parser = subparsers.add_parser(
        "rag",
        help="Manage the retrieval index",
        description=(
            "Index session journals and configured note files into a local "
            "vector index for retrieval-augmented recall "
            "(memory.rag in config.yaml)."
        ),
    )
    rag_parser.set_defaults(func=cmd_rag)
    rag_subparsers = rag_parser.add_subparsers(dest="rag_action")

    rag_index = rag_subparsers.add_parser(
        "index", help="Embed new journal/note content into the index"
    )
    rag_index.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete the existing index before re-embedding everything.",
    )
    rag_index.set_defaults(func=cmd_rag)
