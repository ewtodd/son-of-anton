"""Resolve gateway ``terminal.cwd`` placeholder values to ``TERMINAL_CWD``.

When ``terminal.cwd`` is unset or a placeholder (``.``, ``auto``, ``cwd``),
the gateway resolves it from the messaging cwd or the configured home
fallback for the local backend; other backends leave ``TERMINAL_CWD``
unset so the backend picks its own default.
"""

from __future__ import annotations

CWD_PLACEHOLDERS = frozenset({".", "auto", "cwd"})


def _truthy_env(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes"}


def resolve_placeholder_terminal_cwd(
    *,
    configured_cwd: str,
    terminal_backend: str,
    messaging_cwd: str | None,
    home_fallback: str,
) -> str | None:
    """Return the ``TERMINAL_CWD`` value to set, or ``None`` to leave it unset.

    Cases:
      - **local** + placeholder → ``MESSAGING_CWD`` or ``home_fallback``
      - other backends + placeholder → ``None`` (backend default)
    """
    if configured_cwd and configured_cwd not in CWD_PLACEHOLDERS:
        return configured_cwd

    backend = (terminal_backend or "local").strip().lower()
    if backend == "local":
        messaging = (messaging_cwd or "").strip()
        return messaging or home_fallback

    return None
