"""Static user-facing message catalog (English only).

The highest-impact static strings shown to the user by Son of Anton itself --
approval prompts, a handful of gateway slash command replies, restart-drain
notices -- live in ``locales/en.toml`` at the repo root, a nested TOML file
flattened to dotted keys (e.g. ``approval.choose`` or
``gateway.approval_expired``).  A missing key returns the key path itself so a
broken catalog never crashes the agent.

Usage::

    from agent.i18n import t
    print(t("approval.choose_long"))
    print(t("gateway.draining", count=3))                  # {count} formatted
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_catalog: dict[str, str] | None = None
_catalog_lock = threading.Lock()


def _locales_dir() -> Path:
    """Return the directory containing the locale TOML file.

    ``SON_OF_ANTON_BUNDLED_LOCALES`` (set by the Nix wrapper) wins; otherwise
    ``<repo-root>/locales`` for source checkouts and editable installs.
    """
    override = os.getenv("SON_OF_ANTON_BUNDLED_LOCALES", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_dir():
            return candidate
        logger.warning(
            "SON_OF_ANTON_BUNDLED_LOCALES points to a non-directory path (%s); "
            "falling back to source locale resolution",
            override,
        )
    return Path(__file__).resolve().parent.parent / "locales"


def _flatten_into(node: Any, prefix: str, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            _flatten_into(value, child_key, out)
    elif isinstance(node, str):
        out[prefix] = node


def _load_catalog() -> dict[str, str]:
    """Load and flatten ``en.toml`` once per process."""
    global _catalog
    with _catalog_lock:
        if _catalog is not None:
            return _catalog
    path = _locales_dir() / "en.toml"
    flat: dict[str, str] = {}
    try:
        import tomllib
        with path.open("r", encoding="utf-8") as f:
            _flatten_into(tomllib.loads(f.read()) or {}, "", flat)
    except Exception as exc:
        logger.warning("Failed to load i18n catalog %s: %s", path, exc)
    with _catalog_lock:
        _catalog = flat
    return flat


def t(key: str, **format_kwargs: Any) -> str:
    """Look up a dotted key, ``str.format``-ing it with ``format_kwargs``."""
    value = _load_catalog().get(key)
    if value is None:
        logger.debug("i18n miss: key=%r", key)
        value = key
    if format_kwargs:
        try:
            return value.format(**format_kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("i18n format failed for key=%r kwargs=%r: %s", key, format_kwargs, exc)
            return value
    return value


__all__ = ["t"]
