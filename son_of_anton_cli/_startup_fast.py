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
    "active_profile_may_override_home",
    "read_openai_version",
    "read_install_method",
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


def active_profile_may_override_home(son_of_anton_root: str) -> bool:
    """Cheap probe: does an active non-default profile redirect SON_OF_ANTON_HOME?"""
    active_profile = os.path.join(son_of_anton_root, "active_profile")
    try:
        if os.path.exists(active_profile):
            with open(active_profile, encoding="utf-8") as handle:
                active = handle.read().strip()
            return bool(active and active != "default")
    except (OSError, UnicodeDecodeError):
        pass
    return False


def _resolved_home() -> str:
    son_of_anton_home = os.environ.get("SON_OF_ANTON_HOME", "").strip()
    if son_of_anton_home:
        return son_of_anton_home
    return os.path.join(os.path.expanduser("~"), ".son-of-anton")


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


def read_install_method() -> str | None:
    """Read the installer's ``.install_method`` stamp, if present.

    Only the stamp (step 1 of ``config.detect_install_method``'s resolution
    order) — the managed/git/pip fallbacks need heavier imports and stay on
    the slow path.
    """
    stamp = os.path.join(_resolved_home(), ".install_method")
    try:
        with open(stamp, encoding="utf-8") as handle:
            method = handle.read().strip().lower()
        return method or None
    except OSError:
        return None


def print_fast_version_info(*, check_updates: bool = True) -> None:
    """THE canonical ``son-of-anton --version`` output (also used by /version).

    The static lines print instantly from stdlib-only probes; everything
    heavier (upstream SHA in the version line, authoritative install-method
    detection, the update-status check) is lazy-imported AFTER the first
    line is already on screen, so perceived latency stays instant while the
    output carries the full information that used to require the (removed)
    ``son-of-anton version`` subcommand. Every lazy block degrades gracefully —
    a broken/heavy import can never take the basic version output down.
    """
    # Line 1: registry-owned banner label (includes "· upstream <sha>" for
    # git installs). banner.py keeps rich/prompt_toolkit lazy, so this
    # import is light; fall back to the plain label if anything fails.
    try:
        from son_of_anton_cli.banner import format_banner_version_label

        print(format_banner_version_label())
    except Exception:
        from son_of_anton_cli import __release_date__, __version__

        print(f"Son of Anton Agent v{__version__} ({__release_date__})")

    print(f"Install directory: {project_root_str()}")

    # Install method: authoritative resolver first (code-scoped stamp →
    # managed → nix → git → pip; also self-heals poisoned shared-home
    # 'docker' stamps). Fall back to the cheap stdlib stamp probe only if
    # the resolver import/run fails.
    try:
        from pathlib import Path

        from son_of_anton_cli.config import detect_install_method

        install_method = detect_install_method(Path(project_root_str()))
    except Exception:
        install_method = read_install_method()
    if install_method:
        print(f"Install method: {install_method}")

    print(f"Python: {sys.version.split()[0]}")

    openai_version = read_openai_version()
    print(f"OpenAI SDK: {openai_version}" if openai_version else "OpenAI SDK: Not installed")

    if not check_updates:
        return

    # Update status (synchronous — acceptable since the user asked for
    # version info). Bounded by check_for_updates' own subprocess/network
    # timeouts and its 6-hour cache; any failure prints nothing.
    try:
        from son_of_anton_cli.banner import UPDATE_AVAILABLE_NO_COUNT, check_for_updates
        from son_of_anton_cli.config import recommended_update_command

        behind = check_for_updates()
        if behind == UPDATE_AVAILABLE_NO_COUNT:
            print(f"Update available — run '{recommended_update_command()}'")
        elif behind and behind > 0:
            commits_word = "commit" if behind == 1 else "commits"
            print(
                f"Update available: {behind} {commits_word} behind — "
                f"run '{recommended_update_command()}'"
            )
        elif behind == 0:
            print("Up to date")
    except Exception:
        pass


def try_fast_version(argv: list[str] | None = None) -> bool:
    """Handle ``son-of-anton --version`` before the heavy import wall.

    Only ``--version``/``-V`` (the ``version`` subcommand was removed —
    ``--version`` now carries the full output incl. update status).
    """
    if argv is None:
        argv = sys.argv[1:]
    if not is_global_fast_version_argv(argv):
        return False

    print_fast_version_info()
    return True
