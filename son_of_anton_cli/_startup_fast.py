"""Pre-import startup fast paths — THE canonical lightweight helpers.

This module is imported by ``son_of_anton_cli/main.py`` BEFORE its heavy import
wall (config, argparse tree, logging, providers). Everything here must stay
**stdlib-only and cheap** (os/sys file probes; no yaml, no son_of_anton_cli.config,
no argparse). A guard test (``test_startup_fast_import_weight``) subprocess-
imports this module and fails if any heavy module sneaks into sys.modules.

Why this module exists (the bug class it kills): version-printing kept being
reimplemented as ``*_fast()`` copies at the top of main.py, each duplicating
canonical logic — project-root resolution and profile detection. The copies
drifted: eb4040242 changed the canonical output and referenced
``PROJECT_ROOT`` inside the fast function, which doesn't exist yet on the
fast path → the fast path NameError'd on --version and nobody noticed. One
implementation, imported by both the fast path and the module constants,
makes that drift structurally impossible; the parity guard test would have
caught eb4040242 the day it landed.
"""

from __future__ import annotations

import os
import sys

__all__ = [
    "project_root_str",
    "ensure_project_root_on_path",
    "is_global_fast_version_argv",
    "read_openai_version",
    "print_fast_version_info",
    "try_fast_version",
]


def project_root_str() -> str:
    """Repo root as a str — the single source for main.py's PROJECT_ROOT."""
    return os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir))


def ensure_project_root_on_path() -> None:
    """Put the project root at sys.path[0], deduping realpath-equivalents."""
    project_root = project_root_str()
    normalized_root = os.path.normcase(os.path.realpath(project_root))
    sys.path[:] = [
        entry
        for entry in sys.path
        if not entry
        or os.path.normcase(os.path.realpath(entry)) != normalized_root
    ]
    sys.path.insert(0, project_root)


def is_global_fast_version_argv(argv: list[str]) -> bool:
    return argv in (["--version"], ["-V"])


def read_openai_version() -> str | None:
    """Read OpenAI SDK version without importing ``importlib.metadata``."""
    for base in sys.path:
        if not base:
            base = os.getcwd()
        version_file = os.path.join(base, "openai", "_version.py")
        try:
            with open(version_file, encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped.startswith("__version__"):
                        continue
                    _key, _sep, value = stripped.partition("=")
                    value = value.split("#", 1)[0].strip().strip("\"'")
                    return value or None
        except OSError:
            continue
    return None


def print_fast_version_info() -> None:
    """THE canonical ``son-of-anton --version`` output (also used by /version)."""
    from son_of_anton_cli.banner import format_banner_version_label

    print(format_banner_version_label())
    print(f"Install directory: {project_root_str()}")
    print(f"Python: {sys.version.split()[0]}")
    openai_version = read_openai_version()
    print(f"OpenAI SDK: {openai_version}" if openai_version else "OpenAI SDK: Not installed")


def try_fast_version(argv: list[str] | None = None) -> bool:
    """Handle ``son-of-anton --version`` before the heavy import wall.

    Only ``--version``/``-V`` (the ``version`` subcommand was removed).
    """
    if argv is None:
        argv = sys.argv[1:]
    if not is_global_fast_version_argv(argv):
        return False

    print_fast_version_info()
    return True
