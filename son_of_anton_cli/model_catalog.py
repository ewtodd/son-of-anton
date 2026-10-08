"""Remote model catalog fetcher.

The Son of Anton docs site hosts a JSON manifest of curated models for providers
we want to update without shipping a release (currently OpenRouter and
Nous Portal). This module fetches, validates, and caches that manifest,
falling back to the in-repo hardcoded lists when the network is unavailable.

Pipeline
--------
1. ``get_catalog()`` — returns a parsed manifest dict.
   - Checks in-process cache (invalidated by TTL).
   - Reads disk cache at ``~/.son-of-anton/cache/model_catalog.json``.
   - Fetches the master URL if disk cache is stale or missing.
   - On any fetch failure, keeps using the stale cache (or empty dict).

2. ``get_curated_openrouter_models()`` / ``get_curated_nous_models()`` —
   thin accessors returning the shapes existing callers expect. Each
   falls back to the in-repo hardcoded list on any lookup failure.

Schema (version 1)
------------------
::

    {
      "version": 1,
      "updated_at": "2026-04-25T22:00:00Z",
      "metadata": {...},                # free-form
      "providers": {
        "openrouter": {
          "metadata": {...},            # free-form
          "models": [
            {"id": "vendor/model", "description": "recommended",
             "metadata": {...}}          # free-form, model-level
          ]
        },
        "nous": {...}
      }
    }

Unknown fields are ignored — extra metadata can be added at either level
without bumping ``version``. ``version`` bumps are reserved for
breaking changes (renaming ``providers``, changing ``models`` shape).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from son_of_anton_cli import __version__ as _SON_OF_ANTON_VERSION

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fallback fetch chain. The Docusaurus site is served through Vercel, which
# occasionally returns HTTP 403 + x-vercel-mitigated: challenge for non-
# browser clients (urllib, curl). When that happens the disk cache goes
# stale and new model releases never reach the picker. The raw GitHub URL
# is the same manifest published from the same repo and is not bot-gated,
# so we fall through to it whenever the primary URL fails.
SUPPORTED_SCHEMA_VERSION = 1

_SON_OF_ANTON_USER_AGENT = f"son-of-anton-cli/{_SON_OF_ANTON_VERSION}"

# In-process cache to avoid repeated disk + parse work across multiple
# calls within the same session. Invalidated by TTL against the disk file's
# mtime, so calling code never has to think about this.
_catalog_cache: dict[str, Any] | None = None
_catalog_cache_source_mtime: float = 0.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    """Return the disk cache path. Import lazily so tests can monkeypatch home."""
    from son_of_anton_constants import get_son_of_anton_home
    return get_son_of_anton_home() / "cache" / "model_catalog.json"


# ---------------------------------------------------------------------------
# Fetch + validate + cache
# ---------------------------------------------------------------------------


def _validate_manifest(data: Any) -> bool:
    """Return True when ``data`` matches the minimum manifest shape."""
    if not isinstance(data, dict):
        return False
    version = data.get("version")
    if not isinstance(version, int) or version > SUPPORTED_SCHEMA_VERSION:
        # Future schema version we don't understand — refuse rather than
        # guess. Older schemas (version < 1) aren't supported either.
        return False
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return False
    for pname, pblock in providers.items():
        if not isinstance(pname, str) or not isinstance(pblock, dict):
            return False
        models = pblock.get("models")
        if not isinstance(models, list):
            return False
        for m in models:
            if not isinstance(m, dict):
                return False
            if not isinstance(m.get("id"), str) or not m["id"].strip():
                return False
    return True


# Stale-while-revalidate machinery: at most one background manifest refresh
# in flight per process. The refreshed manifest lands on disk; the NEXT
# get_catalog() call picks it up via the mtime check.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reset_cache() -> None:
    """Clear the in-process cache. Used by tests and ``son-of-anton model --refresh``."""
    global _catalog_cache, _catalog_cache_source_mtime
    _catalog_cache = None
    _catalog_cache_source_mtime = 0.0
