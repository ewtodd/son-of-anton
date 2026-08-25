"""Startup bootstrap for Son of Anton entry points.

Son of Anton is Nix-only (Linux + macOS). The Windows UTF-8 stdio bootstrap
that this module historically applied was stripped; on Nix platforms POSIX
locales are already UTF-8 in the default case and ``PYTHONUTF8`` handling is
left to the user.

This module keeps the two platform-neutral helpers entry points rely on:

- :func:`harden_import_path` — stop a package in the current directory from
  shadowing Son of Anton modules (``utils``, ``proxy``, ``ui``).
- :func:`activate_durable_lazy_target` — wire the durable lazy-install dir
  onto ``sys.path`` when one is configured.

Idempotent: safe to import multiple times.
"""

from __future__ import annotations

import os
import sys

_bootstrap_applied = False


def apply_windows_utf8_bootstrap() -> bool:
    """Legacy Windows UTF-8 bootstrap; always a no-op on Nix platforms.

    Returns False (bootstrap not applied) — entry points import this module
    for its import-time side effects, and the historical call sites that
    branch on the return value expect False on POSIX.
    """
    return False


def suppress_platform_ver_console() -> None:
    """Legacy Windows ``platform._syscmd_ver`` guard; no-op on Nix platforms."""
    return None


def harden_import_path(src_root: str | None = None) -> None:
    """Stop a package in the current directory from shadowing Son of Anton modules.

    Son of Anton ships top-level modules with common names (``utils``, ``proxy``,
    ``ui``).  Python always seeds ``sys.path`` with the current directory, so
    launching an entry point from a project that has its own ``utils/`` package
    makes ``from utils import ...`` resolve to the *user's* package and crash
    with an ImportError before the gateway can even start.

    The current directory reaches ``sys.path`` two ways, and a complete guard
    has to handle both:

      - As the empty string ``""`` (or ``"."``) that Python inserts at
        ``sys.path[0]`` for ``-m`` / script launches.
      - As its own *absolute* path, when a venv activation or a project that
        adds itself to ``PYTHONPATH`` puts the directory there explicitly.

    We drop the relative forms outright, then force the real Son of Anton source root
    to the front — relocating it ahead of any absolute cwd entry rather than
    only inserting when absent, so an absolute cwd path can't keep winning.

    ``src_root`` defaults to the directory this module lives in, which is the
    repository root for every shipped entry point, so the guard is
    self-sufficient and does not depend on the spawner exporting an env var.
    """
    root = src_root or os.environ.get("SON_OF_ANTON_PYTHON_SRC_ROOT") or os.path.dirname(
        os.path.abspath(__file__)
    )

    sys.path[:] = [p for p in sys.path if p not in ("", ".")]

    root_abs = os.path.abspath(root)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != root_abs]
    sys.path.insert(0, root)


def activate_durable_lazy_target() -> None:
    """Put the durable lazy-install dir on ``sys.path`` if one is configured.

    When ``SON_OF_ANTON_LAZY_INSTALL_TARGET`` is set (immutable deployments
    that seal the agent venv and redirect lazy installs to a writable dir on
    the data volume), packages installed there on a previous run must be
    importable on this run, so we activate the dir here — at the very first
    import, before any backend module imports its SDK.

    The activation appends to the END of ``sys.path`` so the core venv
    always wins name collisions (see ``tools.lazy_deps`` for the full
    security rationale). Never raises; a missing/empty target is a no-op.
    """
    if not os.environ.get("SON_OF_ANTON_LAZY_INSTALL_TARGET", "").strip():
        return
    try:
        from tools import lazy_deps
        lazy_deps.activate_durable_lazy_target()
    except Exception:
        # Bootstrap must never crash an entry point. If activation fails the
        # backend simply reports itself unavailable, exactly as before.
        pass


# Apply on import — entry points just need ``import son_of_anton_bootstrap``
# at the very top of their module, before importing anything else.  The
# import side effect does the right thing.
apply_windows_utf8_bootstrap()
suppress_platform_ver_console()

# Activate the durable lazy-install target (immutable deployments) so
# packages installed into the data volume on a previous run are importable
# this run, before any backend module imports its SDK. No-op when unset.
activate_durable_lazy_target()
