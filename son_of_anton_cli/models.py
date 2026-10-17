"""The custom endpoint's model catalog.

``/models`` probing, the disk cache the ``/model`` picker reads
(``provider_models_cache.json``, keyed ``custom:<base_url>``), and validation
of a typed model name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.request
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Optional

from son_of_anton_cli.urllib_security import open_credentialed_url

logger = logging.getLogger(__name__)

_PROVIDER_MODELS_CACHE_TTL = 3600  # 1h
# Stale-while-revalidate: an expired same-credential entry is served at once
# while a daemon thread refreshes it; beyond this bound it is a miss.
_PROVIDER_MODELS_STALE_SERVE_MAX = 7 * 24 * 3600  # 7d
# A catalog that reports live statuses (maitred) is asked again once its
# statuses are older than this, so a mode switch shows on the next open.
_LIVE_STATUS_TTL = 10

_swr_refresh_inflight: set = set()
_swr_refresh_lock = threading.Lock()


def _user_agent() -> str:
    from agent.transports.chat_completions import user_agent

    return user_agent()


def probe_api_models(
    api_key: Optional[str],
    base_url: Optional[str],
    timeout: float = 5.0,
    api_mode: Optional[str] = None,
    request_headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Probe ``<base_url>/models``, retrying with ``/v1`` toggled.

    Returns ``models`` (ids, or None when unreachable), whether the endpoint
    ``reports_status`` and which ids it marks ``unavailable``, plus the URL
    probed and the base URL that answered.
    """
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        return {"models": None, "probed_url": None, "resolved_base_url": "",
                "suggested_base_url": None, "used_fallback": False}
    alternate_base = normalized[:-3].rstrip("/") if normalized.endswith("/v1") else normalized + "/v1"
    headers: dict[str, str] = {"User-Agent": _user_agent()}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if isinstance(request_headers, dict):
        headers.update({str(k): str(v) for k, v in request_headers.items() if v is not None})
    tried: list[str] = []
    for candidate_base, is_fallback in ((normalized, False), (alternate_base, True)):
        url = candidate_base.rstrip("/") + "/models"
        tried.append(url)
        try:
            with open_credentialed_url(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
                entries = json.loads(resp.read().decode()).get("data", [])
            return {
                "models": [m.get("id", "") for m in entries],
                # A gateway in front of servers that come and go (maitred)
                # lists every model it fronts with a live ``status``.
                "reports_status": any("status" in m for m in entries),
                "unavailable": [m.get("id", "") for m in entries if m.get("status") == "unavailable"],
                "probed_url": url,
                "resolved_base_url": candidate_base.rstrip("/"),
                "suggested_base_url": alternate_base if alternate_base != candidate_base else normalized,
                "used_fallback": is_fallback,
            }
        except Exception:
            continue
    return {"models": None, "probed_url": tried[0], "resolved_base_url": normalized,
            "suggested_base_url": alternate_base, "used_fallback": False}


def fetch_api_models(
    api_key: Optional[str],
    base_url: Optional[str],
    timeout: float = 5.0,
    api_mode: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> Optional[list[str]]:
    """Model ids the endpoint lists, or None when it could not be reached."""
    return probe_api_models(api_key, base_url, timeout=timeout, request_headers=headers).get("models")


def _provider_models_cache_path() -> Path:
    from son_of_anton_constants import get_son_of_anton_home

    return get_son_of_anton_home() / "provider_models_cache.json"


def _load_provider_models_cache() -> dict:
    try:
        path = _provider_models_cache_path()
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_provider_models_cache(data: dict) -> None:
    try:
        from utils import atomic_json_write

        path = _provider_models_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, data, indent=None)
    except Exception:
        pass


def clear_provider_models_cache(provider: Optional[str] = None) -> None:
    """Wipe the picker's model cache (``/model --refresh``)."""
    try:
        path = _provider_models_cache_path()
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _custom_endpoint_fingerprint(
    api_key: Optional[str], api_mode: Optional[str], headers: Optional[dict[str, str]]
) -> str:
    """Hash of what a catalog probe sends: a rotated key busts the cache entry."""
    blob = "|".join((api_key or "", api_mode or "", json.dumps(headers or {}, sort_keys=True)))
    return hashlib.blake2b(blob.encode("utf-8", errors="replace"), digest_size=8).hexdigest()


def _cache_entry_valid(entry: Any, fp: str) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("fp") == fp
        and isinstance(entry.get("models"), list)
        and bool(entry["models"])
        and isinstance(entry.get("at"), (int, float))
        and not isinstance(entry.get("at"), bool)
    )


def _probe_custom_catalog(api_key, base_url, *, timeout, api_mode, headers, fp):
    """The endpoint's catalog and its cache row (None when it listed nothing).

    A catalog that reports live statuses keeps the ids it marks unavailable
    under ``unavailable``; ``models`` stays complete, since naming a model
    that is not being served is still valid.
    """
    probe = probe_api_models(api_key, base_url, timeout=timeout, request_headers=headers)
    live = probe.get("models")
    if not live:
        return live, None
    row: dict[str, Any] = {"fp": fp, "at": time.time(), "models": list(live)}
    if probe.get("reports_status"):
        row["unavailable"] = list(probe.get("unavailable") or [])
    return live, row


def _spawn_swr_refresh(cache_key: str, refresh_fn) -> None:
    """Refresh one cache entry on a daemon thread, at most one in flight per key."""
    with _swr_refresh_lock:
        if cache_key in _swr_refresh_inflight:
            return
        _swr_refresh_inflight.add(cache_key)

    def _refresh() -> None:
        try:
            entry = refresh_fn()
            if entry:
                cache = _load_provider_models_cache()
                cache[cache_key] = entry
                _save_provider_models_cache(cache)
        except Exception:
            logger.debug("SWR refresh failed for %s", cache_key, exc_info=True)
        finally:
            with _swr_refresh_lock:
                _swr_refresh_inflight.discard(cache_key)

    threading.Thread(target=_refresh, daemon=True, name=f"model-cache-swr-{cache_key}").start()


def cached_fetch_api_models(
    api_key: Optional[str],
    base_url: Optional[str],
    *,
    timeout: float = 5.0,
    api_mode: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
    force_refresh: bool = False,
    cache_only: bool = False,
    ttl_seconds: int = _PROVIDER_MODELS_CACHE_TTL,
) -> Optional[list[str]]:
    """Disk-cached :func:`fetch_api_models` (stale-while-revalidate).

    ``cache_only`` serves a previously discovered catalog without touching
    the network and returns None when nothing usable is cached. A failed
    live fetch serves the last same-fingerprint result.
    """
    normalized_url = str(base_url or "").strip().rstrip("/").lower()
    if not normalized_url:
        return None if cache_only else fetch_api_models(api_key, base_url, timeout=timeout, headers=headers)
    cache_key = f"custom:{normalized_url}"
    fp = _custom_endpoint_fingerprint(api_key, api_mode, headers)
    cache = _load_provider_models_cache()
    entry = cache.get(cache_key)
    now = time.time()
    if cache_only:
        if force_refresh or not _cache_entry_valid(entry, fp):
            return None
        return None if now - entry["at"] >= _PROVIDER_MODELS_STALE_SERVE_MAX else list(entry["models"])
    if not force_refresh and _cache_entry_valid(entry, fp):
        age = now - entry["at"]
        if age < ttl_seconds:
            return list(entry["models"])
        if age < _PROVIDER_MODELS_STALE_SERVE_MAX:
            _spawn_swr_refresh(cache_key, lambda: _probe_custom_catalog(
                api_key, base_url, timeout=timeout, api_mode=api_mode, headers=headers, fp=fp
            )[1])
            return list(entry["models"])
    live, row = _probe_custom_catalog(api_key, base_url, timeout=timeout, api_mode=api_mode, headers=headers, fp=fp)
    if row:
        cache[cache_key] = row
        _save_provider_models_cache(cache)
        return list(live)
    if _cache_entry_valid(entry, fp):
        return list(entry["models"])
    return live


def unavailable_models(
    api_key: Optional[str],
    base_url: Optional[str],
    *,
    timeout: float = 5.0,
    api_mode: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> frozenset[str]:
    """The models the endpoint marks ``status: "unavailable"`` right now.

    Only an endpoint whose cached catalog carried statuses is asked again
    (skipped while its statuses are seconds old). Any failure marks nothing.
    """
    normalized_url = str(base_url or "").strip().rstrip("/").lower()
    if not normalized_url:
        return frozenset()
    cache_key = f"custom:{normalized_url}"
    cache = _load_provider_models_cache()
    entry = cache.get(cache_key)
    if not isinstance(entry, dict) or not isinstance(entry.get("unavailable"), list):
        return frozenset()
    at = entry.get("at")
    if isinstance(at, (int, float)) and not isinstance(at, bool) and time.time() - at < _LIVE_STATUS_TTL:
        return frozenset(entry["unavailable"])
    fp = _custom_endpoint_fingerprint(api_key, api_mode, headers)
    _, row = _probe_custom_catalog(api_key, base_url, timeout=timeout, api_mode=api_mode, headers=headers, fp=fp)
    if not row:
        return frozenset()
    if entry.get("fp") == fp:
        cache[cache_key] = row
        _save_provider_models_cache(cache)
    return frozenset(row.get("unavailable") or ())


def validate_requested_model(
    model_name: str,
    provider: Optional[str],
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    api_mode: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Validate a ``/model`` value against the endpoint's ``/models`` listing.

    Returns ``accepted`` / ``persist`` / ``recognized`` / ``message`` (and
    ``corrected_model`` for a near-certain typo). A name the listing lacks is
    accepted with a note (hidden or aliased models); an unreachable listing
    rejects it.
    """
    requested = (model_name or "").strip()
    if not requested:
        return {"accepted": False, "persist": False, "recognized": False, "message": "Model name cannot be empty."}
    if any(ch.isspace() for ch in requested):
        return {"accepted": False, "persist": False, "recognized": False,
                "message": "Model names cannot contain spaces."}
    probe = probe_api_models(api_key, base_url, request_headers=headers)
    api_models = probe.get("models")
    if api_models is None:
        message = (
            f"Note: could not reach this custom endpoint's model listing at `{probe.get('probed_url')}`. "
            f"Son of Anton will still save `{requested}`, but the endpoint should expose `/models` for verification."
        )
        if probe.get("suggested_base_url"):
            message += f"\n  If this server expects `/v1`, try base URL: `{probe.get('suggested_base_url')}`"
        return {"accepted": False, "persist": True, "recognized": False, "message": message}
    if requested in set(api_models):
        return {"accepted": True, "persist": True, "recognized": True, "message": None}
    auto = get_close_matches(requested, api_models, n=1, cutoff=0.9)
    if auto:
        return {"accepted": True, "persist": True, "recognized": True, "corrected_model": auto[0],
                "message": f"Auto-corrected `{requested}` → `{auto[0]}`"}
    suggestions = get_close_matches(requested, api_models, n=3, cutoff=0.5)
    message = (
        f"Note: `{requested}` was not found in this custom endpoint's model listing "
        f"({probe.get('probed_url')}). It may still work if the server supports hidden or aliased models."
    )
    if suggestions:
        message += "\n  Similar models: " + ", ".join(f"`{s}`" for s in suggestions)
    if probe.get("used_fallback"):
        message += (
            f"\n  Endpoint verification succeeded after trying `{probe.get('resolved_base_url')}`. "
            f"Consider saving that as your base URL."
        )
    return {"accepted": True, "persist": True, "recognized": False, "message": message}
