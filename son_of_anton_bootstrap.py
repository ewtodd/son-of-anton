"""Startup bootstrap for Son of Anton entry points.

Son of Anton is Nix-only (Linux + macOS). The Windows UTF-8 stdio bootstrap
that this module historically applied was stripped; on Nix platforms POSIX
locales are already UTF-8 in the default case and ``PYTHONUTF8`` handling is
left to the user.

This module keeps the two platform-neutral helpers entry points rely on:

- :func:`harden_import_path` — stop a package in the current directory from
  shadowing Son of Anton modules (``utils``, ``proxy``, ``ui``).

Idempotent: safe to import multiple times.
"""

from __future__ import annotations


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


# Apply on import — entry points just need ``import son_of_anton_bootstrap``
# at the very top of their module, before importing anything else.  The
# import side effect does the right thing.
apply_windows_utf8_bootstrap()
suppress_platform_ver_console()
