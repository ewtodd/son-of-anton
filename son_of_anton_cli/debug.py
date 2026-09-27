"""Paste-lifecycle state for Son of Anton Agent.

This module previously held the ``son-of-anton debug share``/``delete``
command (log capture + redaction + upload to a paste service).  The fork
removed the ``debug`` subcommand entirely, so all of that machinery is gone;
what remains is the pending-deletion tracker for uploaded pastes, which the
gateway's cron ticker drives: ``gateway/run.py::_start_cron_ticker`` calls
:func:`_sweep_expired_pastes` once per hour to DELETE pastes whose
``expire_at`` has passed.

Paste entries are stored in ``~/.son-of-anton/pastes/pending.json``.
"""

import json
import time
import urllib.request
from pathlib import Path
from typing import Optional

from son_of_anton_constants import get_son_of_anton_home
from utils import atomic_replace

# ---------------------------------------------------------------------------
# Paste services — only paste.rs supports unauthenticated DELETE, so it is
# the only service the lifecycle tracker can clean up.
# ---------------------------------------------------------------------------

_PASTE_RS_URL = "https://paste.rs/"

# Auto-delete pastes after this many seconds (6 hours).
_AUTO_DELETE_SECONDS = 21600


# ---------------------------------------------------------------------------
# Pending-deletion tracking (replaces the old fork-and-sleep subprocess).
# ---------------------------------------------------------------------------

def _pending_file() -> Path:
    """Path to ``~/.son-of-anton/pastes/pending.json``.

    Each entry: ``{"url": "...", "expire_at": <unix_ts>}``.  Scheduled
    DELETEs used to be handled by spawning a detached Python process per
    paste that slept for 6 hours; those accumulated forever if the user
    ran a paste upload repeatedly.

    Deletion is now driven by the gateway's cron ticker
    (``gateway/run.py::_start_cron_ticker``) which calls
    ``_sweep_expired_pastes`` once per hour.
    """
    return get_son_of_anton_home() / "pastes" / "pending.json"


def _load_pending() -> list[dict]:
    path = _pending_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            # Filter to well-formed entries only
            return [
                e for e in data
                if isinstance(e, dict) and "url" in e and "expire_at" in e
            ]
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return []


def _save_pending(entries: list[dict]) -> None:
    path = _pending_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        atomic_replace(tmp, path)
    except OSError:
        # Non-fatal — worst case a pending entry is not persisted.
        pass


def _record_pending(urls: list[str], delay_seconds: int = _AUTO_DELETE_SECONDS) -> None:
    """Record *urls* for deletion at ``now + delay_seconds``.

    Only paste.rs URLs are recorded (dpaste.com pastes auto-expire and
    cannot be DELETEd).  Entries are merged into any existing pending.json.
    """
    paste_rs_urls = [u for u in urls if _extract_paste_id(u)]
    if not paste_rs_urls:
        return

    entries = _load_pending()
    # Dedupe by URL: keep the later expire_at if same URL appears twice
    by_url: dict[str, float] = {e["url"]: float(e["expire_at"]) for e in entries}
    expire_at = time.time() + delay_seconds
    for u in paste_rs_urls:
        by_url[u] = max(expire_at, by_url.get(u, 0.0))
    merged = [{"url": u, "expire_at": ts} for u, ts in by_url.items()]
    _save_pending(merged)


def _sweep_expired_pastes(now: Optional[float] = None) -> tuple[int, int]:
    """Synchronously DELETE any pending pastes whose ``expire_at`` has passed.

    Returns ``(deleted, remaining)``.  Best-effort: failed deletes stay in
    the pending file and will be retried on the next sweep.  Silent —
    intended to be called from the gateway's hourly cron ticker with
    minimal noise.
    """
    entries = _load_pending()
    if not entries:
        return (0, 0)

    current = time.time() if now is None else now
    deleted = 0
    remaining: list[dict] = []

    for entry in entries:
        try:
            expire_at = float(entry.get("expire_at", 0))
        except (TypeError, ValueError):
            continue  # drop malformed entries
        if expire_at > current:
            remaining.append(entry)
            continue

        url = entry.get("url", "")
        try:
            if delete_paste(url):
                deleted += 1
                continue
        except Exception:
            # Network hiccup, 404 (already gone), etc. — drop the entry
            # after a grace period; don't retry forever.
            pass

        # Retain failed deletes for up to 24h past expiration, then give up.
        if expire_at + 86400 > current:
            remaining.append(entry)
        else:
            deleted += 1  # count as reaped (paste.rs will GC eventually)

    if deleted:
        _save_pending(remaining)

    return (deleted, len(remaining))


def _best_effort_sweep_expired_pastes() -> None:
    """Attempt pending-paste cleanup without letting the caller fail offline."""
    try:
        _sweep_expired_pastes()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Privacy / delete helpers
# ---------------------------------------------------------------------------

def _extract_paste_id(url: str) -> Optional[str]:
    """Extract the paste ID from a paste.rs URL.

    Returns the ID string, or None if the URL doesn't match a known service.
    """
    url = url.strip().rstrip("/")
    for prefix in ("https://paste.rs/", "http://paste.rs/"):
        if url.startswith(prefix):
            return url[len(prefix):]
    return None


def delete_paste(url: str) -> bool:
    """Delete a paste from paste.rs.  Returns True on success.

    Only paste.rs supports unauthenticated DELETE.  dpaste.com pastes
    expire automatically but cannot be deleted via API.
    """
    paste_id = _extract_paste_id(url)
    if not paste_id:
        raise ValueError(
            f"Cannot delete: only paste.rs URLs are supported.  Got: {url}"
        )

    target = f"{_PASTE_RS_URL}{paste_id}"
    req = urllib.request.Request(
        target, method="DELETE",
        headers={"User-Agent": "son-of-anton/debug-share"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return 200 <= resp.status < 300


def _schedule_auto_delete(urls: list[str], delay_seconds: int = _AUTO_DELETE_SECONDS):
    """Record *urls* for deletion ``delay_seconds`` from now.

    Appends to ``~/.son-of-anton/pastes/pending.json`` and the gateway's
    cron ticker sweeps expired entries once per hour.  If the gateway never
    runs again, paste.rs's own retention policy handles cleanup.
    """
    _record_pending(urls, delay_seconds=delay_seconds)
