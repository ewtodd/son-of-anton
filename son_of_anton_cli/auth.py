"""
Multi-provider authentication system for Son of Anton Agent.

Supports OAuth device code flows (Nous Portal, xAI Grok) and
traditional API key providers (OpenRouter, custom endpoints). Auth state
is persisted in ~/.son-of-anton/auth.json with cross-process file locking.

Architecture:
- ProviderConfig registry defines known OAuth providers
- Auth store (auth.json) holds per-provider credential state
- resolve_provider() picks the active provider via priority chain
- resolve_*_runtime_credentials() handles token refresh and runtime keys
- logout_command() is the CLI entry point for clearing auth

Nous authentication paths:
- Invoke JWT (preferred): use a scoped access_token directly for inference.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import base64
import hashlib
import subprocess
import threading
import time
import uuid

# httpx is imported lazily: it costs ~30ms at import time and son_of_anton_cli.auth
# is on the interactive-CLI startup path via credential_pool → auxiliary_client
# → cli_commands_mixin, where no HTTP request is ever made before first use.
# The proxy resolves to the real module on first attribute access; every
# consumer in this file uses `httpx.<attr>` so the swap is transparent.
# Annotations like ``httpx.Client`` stay valid: `from __future__ import
# annotations` (above) keeps them unevaluated at runtime, and the
# TYPE_CHECKING import gives static checkers the real module.
import importlib as _importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx
else:
    class _LazyHttpx:
        __slots__ = ("_mod",)

        def __init__(self) -> None:
            object.__setattr__(self, "_mod", None)

        def _resolve(self):
            mod = object.__getattribute__(self, "_mod")
            if mod is None:
                mod = _importlib.import_module("httpx")
                object.__setattr__(self, "_mod", mod)
            return mod

        def __getattr__(self, name):
            return getattr(self._resolve(), name)

        # Forward set/del to the real module so monkeypatch.setattr
        # ("son_of_anton_cli.auth.httpx.Client", ...) keeps working in tests.
        def __setattr__(self, name, value):
            setattr(self._resolve(), name, value)

        def __delattr__(self, name):
            delattr(self._resolve(), name)

    httpx = _LazyHttpx()
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from son_of_anton_cli.config import (
    get_son_of_anton_home,
)
from son_of_anton_constants import secure_parent_dir
from agent.credential_persistence import sanitize_borrowed_credential_payload
from utils import atomic_replace

logger = logging.getLogger(__name__)

try:
    import fcntl
except Exception:
    fcntl = None

# =============================================================================
# Constants
# =============================================================================

AUTH_STORE_VERSION = 1
AUTH_LOCK_TIMEOUT_SECONDS = 15.0

# =============================================================================
# Provider Registry
# =============================================================================

@dataclass
class ProviderConfig:
    """Describes a known inference provider."""
    id: str
    name: str
    auth_type: str  # "oauth_device_code", "oauth_external", "oauth_minimax", or "api_key"
    portal_base_url: str = ""
    inference_base_url: str = ""
    client_id: str = ""
    scope: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    # For API-key providers: env vars to check (in priority order)
    api_key_env_vars: tuple = ()
    # Optional env var for base URL override
    base_url_env_var: str = ""


# The fork's provider surface: OpenAI-compatible API-key providers only.
# Local / self-hosted endpoints (llama-swap, ollama, vllm, ...) are custom
# providers declared in config.toml ``custom_providers`` and are NOT registry
# entries — see son_of_anton_cli/providers.py for their resolution.
PROVIDER_REGISTRY: Dict[str, ProviderConfig] = {
    "openai-api": ProviderConfig(
        id="openai-api",
        name="OpenAI API",
        auth_type="api_key",
        inference_base_url="https://api.openai.com/v1",
        api_key_env_vars=("OPENAI_API_KEY",),
        base_url_env_var="OPENAI_BASE_URL",
    ),
}

# Auto-extend PROVIDER_REGISTRY with any api-key provider registered in
# providers/ that is not already declared above.  New providers only need a
# plugins/model-providers/<name>/ plugin — no edits to this file required.
try:
    from providers import list_providers as _list_providers_for_registry
    for _pp in _list_providers_for_registry():
        if _pp.name in PROVIDER_REGISTRY:
            continue
        if _pp.auth_type != "api_key" or not _pp.env_vars:
            continue
        # Skip providers that need custom token resolution or are special-cased
        # in resolve_provider() (copilot/kimi/zai have bespoke token refresh;
        # custom is user-supplied and handled outside the registry — adding
        # them here breaks runtime_provider resolution that relies on
        # `custom not in PROVIDER_REGISTRY`).
        if _pp.name in {"copilot", "kimi-coding", "kimi-coding-cn", "zai", "custom"}:
            continue
        _api_key_vars = tuple(v for v in _pp.env_vars if not v.endswith("_BASE_URL") and not v.endswith("_URL"))
        _base_url_var = next((v for v in _pp.env_vars if v.endswith("_BASE_URL") or v.endswith("_URL")), None)
        PROVIDER_REGISTRY[_pp.name] = ProviderConfig(
            id=_pp.name,
            name=_pp.display_name or _pp.name,
            auth_type="api_key",
            inference_base_url=_pp.base_url,
            api_key_env_vars=_api_key_vars or _pp.env_vars,
            base_url_env_var=_base_url_var or "",
        )
        # Also register aliases so resolve_provider() resolves them
        for _alias in _pp.aliases:
            if _alias not in PROVIDER_REGISTRY:
                PROVIDER_REGISTRY[_alias] = PROVIDER_REGISTRY[_pp.name]
except Exception:
    pass


# =============================================================================
# Anthropic Key Helper
# =============================================================================

# =============================================================================
# Kimi Code Endpoint Detection
# =============================================================================

# Kimi Code (kimi.com/code) issues keys prefixed "sk-kimi-" that only work
# on api.kimi.com/coding.  Legacy keys from platform.moonshot.ai work on
# api.moonshot.ai/v1 (the old default).  Auto-detect when user hasn't set
# KIMI_BASE_URL explicitly.
#
# Note: the base URL intentionally has NO /v1 suffix.  The /coding endpoint
# speaks the Anthropic Messages protocol, and the anthropic SDK appends
# "/v1/messages" internally — so "/coding" + SDK suffix → "/coding/v1/messages"
# (the correct target). Using "/coding/v1" here would produce
# "/coding/v1/v1/messages" (a 404).


_PLACEHOLDER_SECRET_VALUES = {
    "*",
    "**",
    "***",
    "changeme",
    "your_api_key",
    "your_api_key_here",
    "your-api-key",
    "placeholder",
    "example",
    "dummy",
    "null",
    "none",
}


def has_usable_secret(value: Any, *, min_length: int = 4) -> bool:
    """Return True when a configured secret looks usable, not empty/placeholder."""
    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    if len(cleaned) < min_length:
        return False
    if cleaned.lower() in _PLACEHOLDER_SECRET_VALUES:
        return False
    return True


def _resolve_api_key_provider_secret(
    provider_id: str, pconfig: ProviderConfig
) -> tuple[str, str]:
    """Resolve an API-key provider's token and indicate where it came from."""
    from son_of_anton_cli.config import get_env_value_prefer_dotenv
    for env_var in pconfig.api_key_env_vars:
        # Prefer ~/.son-of-anton/.env over os.environ so a deliberate key rotation
        # in the user's .env file isn't shadowed by a stale shell export
        # inherited from a parent process (Codex CLI, test runners, etc.).
        val = (get_env_value_prefer_dotenv(env_var) or "").strip()
        if has_usable_secret(val):
            return val, env_var

    # Fallback: try credential pool (e.g. zai key stored via auth.json)
    try:
        from agent.credential_pool import load_pool
        pool = load_pool(provider_id)
        if pool and pool.has_credentials():
            entry = pool.peek()
            if entry:
                key = getattr(entry, "access_token", "") or getattr(entry, "runtime_api_key", "")
                key = str(key).strip()
                if has_usable_secret(key):
                    return key, f"credential_pool:{provider_id}"
    except Exception:
        pass

    return "", ""


# =============================================================================
# Error Types
# =============================================================================


class AuthError(RuntimeError):
    """Structured auth error with UX mapping hints."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        code: Optional[str] = None,
        relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.relogin_required = relogin_required


def format_auth_error(error: Exception) -> str:
    """Map auth failures to concise user-facing guidance."""
    if not isinstance(error, AuthError):
        return str(error)

    if error.relogin_required:
        return f"{error} Run `son-of-anton model` to re-authenticate."

    if error.code == "subscription_required":
        return "No active paid subscription found. Please purchase/activate a subscription, then retry."

    if error.code == "insufficient_credits":
        return "Subscription credits are exhausted. Top up/renew credits, then retry."

    if error.code == "temporarily_unavailable":
        return f"{error} Please retry in a few seconds."

    return str(error)


def _token_fingerprint(token: Any) -> Optional[str]:
    """Return a short hash fingerprint for telemetry without leaking token bytes."""
    if not isinstance(token, str):
        return None
    cleaned = token.strip()
    if not cleaned:
        return None
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]


# =============================================================================
# Auth Store — persistence layer for ~/.son-of-anton/auth.json
# =============================================================================

def _auth_file_path() -> Path:
    path = get_son_of_anton_home() / "auth.json"
    # Seat belt: if pytest is running and SON_OF_ANTON_HOME resolves to the real
    # user's auth store, refuse rather than silently corrupt it. This catches
    # tests that forgot to monkeypatch SON_OF_ANTON_HOME, tests invoked without the
    # hermetic conftest, or sandbox escapes via threads/subprocesses. In
    # production (no PYTEST_CURRENT_TEST) this is a single dict lookup.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        real_home_auth = (Path.home() / ".son-of-anton" / "auth.json").resolve(strict=False)
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            resolved = path
        if resolved == real_home_auth:
            raise RuntimeError(
                f"Refusing to touch real user auth store during test run: {path}. "
                "Set SON_OF_ANTON_HOME to a tmp_path in your test fixture, or run "
                "via scripts/run_tests.sh for hermetic CI-parity env."
            )
    return path


def _global_auth_file_path() -> Optional[Path]:
    """Return the global-root auth.json when the process is in profile mode.

    Returns ``None`` when the profile and global root resolve to the same
    directory (classic mode, or custom SON_OF_ANTON_HOME that is not a profile).
    Used by read-only fallback paths so providers authed at the root are
    visible to profile processes that haven't configured them locally.

    See issue #18594 follow-up (credential_pool shadowing).
    """
    try:
        from son_of_anton_constants import get_default_son_of_anton_root
        global_root = get_default_son_of_anton_root()
    except Exception:
        return None
    profile_home = get_son_of_anton_home()
    try:
        if profile_home.resolve(strict=False) == global_root.resolve(strict=False):
            return None
    except Exception:
        if profile_home == global_root:
            return None
    # No pytest seat belt here: this is a pure read-only path, and
    # ``_load_global_auth_store()`` wraps the read in a try/except so an
    # unreadable global file can never break the profile process.  The
    # write-side seat belt still lives on ``_auth_file_path()`` where it
    # belongs (that's what protects the real user's auth store from being
    # corrupted by a mis-configured test).
    return global_root / "auth.json"


def _load_global_auth_store() -> Dict[str, Any]:
    """Load the global-root auth store (read-only fallback).

    Returns an empty dict when no global fallback exists (classic mode,
    or the global auth.json is absent). Never raises on missing file.

    Memoised keyed on the global auth file's path + mtime (same pattern as
    ``_nous_auth_status_cache``): read_credential_pool() -> load_pool() runs
    this once per provider row in the /model picker, and the path resolution
    (``_global_auth_file_path()`` -> ``get_default_son_of_anton_root()``) + JSON
    parse cost ~105us+ per call even when nothing changed. The global
    store only changes when the user authenticates at global scope (writes
    always go through _save_auth_store, which touches the file), so the mtime
    key keeps the memo freshness-correct. Callers must treat the returned
    store as read-only (all current callers do — .get / dict() / list()
    copies only).
    """
    global _global_auth_store_cache
    global_path = _global_auth_file_path()
    if global_path is None or not global_path.exists():
        _global_auth_store_cache = None
        return {}
    try:
        resolved_path = str(global_path.resolve(strict=False))
        mtime_ns = global_path.stat().st_mtime_ns
        cache_key: Optional[Tuple[str, int]] = (resolved_path, mtime_ns)
    except Exception:
        cache_key = None
    if cache_key is not None and _global_auth_store_cache is not None:
        cached_path, cached_mtime, cached_store = _global_auth_store_cache
        if cached_path == cache_key[0] and cached_mtime == cache_key[1]:
            return cached_store
    if os.environ.get("PYTEST_CURRENT_TEST"):
        real_home_env = os.environ.get("HOME", "")
        if real_home_env:
            real_root = Path(real_home_env) / ".son-of-anton" / "auth.json"
            try:
                if global_path.resolve(strict=False) == real_root.resolve(strict=False):
                    _global_auth_store_cache = None
                    return {}
            except Exception:
                pass
    try:
        store = _load_auth_store(global_path)
    except Exception:
        # A malformed global store must not break profile reads. The
        # profile's own auth store is still authoritative.
        _global_auth_store_cache = None
        return {}
    if cache_key is not None:
        _global_auth_store_cache = (cache_key[0], cache_key[1], store)
    return store


def _auth_lock_path() -> Path:
    return _auth_file_path().with_suffix(".lock")


_auth_target_lock_holders: Dict[str, threading.local] = {}
_auth_target_lock_holders_guard = threading.Lock()


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except Exception:
        return left == right


def _auth_lock_holder_for(target_path: Path) -> threading.local:
    """Return a reentrancy tracker keyed to one canonical auth-store path."""
    try:
        key = str(target_path.resolve(strict=False))
    except Exception:
        key = str(target_path)
    with _auth_target_lock_holders_guard:
        return _auth_target_lock_holders.setdefault(key, threading.local())


@contextmanager
def _file_lock(
    lock_path: Path,
    holder: threading.local,
    timeout_seconds: float,
    timeout_message: str,
):
    """Cross-process advisory flock helper.

    Reentrant per-thread via ``holder.depth``. Falls back to a depth-only
    guard when ``fcntl`` is unavailable (rare).
    Callers supply their own ``threading.local`` so independent locks
    (e.g. profile auth.json vs shared Nous store) don't share reentrancy
    state — that would let one lock's reentrant acquisition silently skip
    the other's kernel-level flock.
    """
    if getattr(holder, "depth", 0) > 0:
        holder.depth += 1
        try:
            yield
        finally:
            holder.depth -= 1
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None:
        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
        return

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(timeout_message)
                time.sleep(0.05)

        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except (OSError, IOError):
                pass


@contextmanager
def _auth_store_lock(
    timeout_seconds: float = AUTH_LOCK_TIMEOUT_SECONDS,
    *,
    target_path: Optional[Path] = None,
):
    """Cross-process advisory lock for one auth.json read/write transaction.

    ``target_path`` is required for profile-to-global write-throughs. A profile
    lock does not protect the distinct global auth store; each path therefore
    uses its own reentrancy tracker and kernel lock.

    Lock ordering invariant: when this lock is held together with
    ``_nous_shared_store_lock``, acquire ``_auth_store_lock`` FIRST
    (outer) and the shared Nous lock SECOND (inner). All runtime
    refresh paths follow this order; violating it risks deadlock
    against a concurrent import on the shared store.
    """
    auth_path = target_path if target_path is not None else _auth_file_path()
    lock_path = auth_path.with_suffix(".lock") if target_path is not None else _auth_lock_path()
    with _file_lock(
        lock_path,
        _auth_lock_holder_for(auth_path),
        timeout_seconds,
        "Timed out waiting for auth store lock",
    ):
        yield


def _load_auth_store(auth_file: Optional[Path] = None) -> Dict[str, Any]:
    auth_file = auth_file or _auth_file_path()
    if not auth_file.exists():
        return {"version": AUTH_STORE_VERSION, "providers": {}}

    try:
        raw = json.loads(auth_file.read_text(encoding="utf-8-sig"))
    except OSError:
        # The file exists (checked above) but could not be READ: EMFILE under
        # fd exhaustion, EACCES, EIO, a stalled network mount. None of those
        # mean the contents are bad, and this module does read-modify-write in
        # ~15 places, so degrading to an empty store here is one
        # _save_auth_store() away from erasing every stored credential.
        # Fail loudly instead and leave the file on disk untouched.
        logger.warning(
            "auth: could not read %s, leaving the store on disk untouched "
            "rather than degrading to an empty one",
            auth_file, exc_info=True,
        )
        raise
    except Exception as exc:
        # Genuine corruption: unparseable JSON, or bytes that are not UTF-8.
        corrupt_path = auth_file.with_suffix(".json.corrupt")
        preserved = False
        try:
            import shutil
            shutil.copy2(auth_file, corrupt_path)
            preserved = True
        except Exception:
            logger.debug(
                "auth: could not preserve a copy of the corrupt store at %s",
                corrupt_path, exc_info=True,
            )
        if preserved:
            logger.warning(
                "auth: failed to parse %s (%s), starting with empty store. "
                "Corrupt file preserved at %s",
                auth_file, exc, corrupt_path,
            )
        else:
            # Do not advertise a backup that was never written.
            logger.warning(
                "auth: failed to parse %s (%s), starting with empty store. "
                "A copy could NOT be preserved at %s",
                auth_file, exc, corrupt_path,
            )
        return {"version": AUTH_STORE_VERSION, "providers": {}}

    if isinstance(raw, dict) and (
        isinstance(raw.get("providers"), dict)
        or isinstance(raw.get("credential_pool"), dict)
    ):
        raw.setdefault("providers", {})
        return raw

    # Migrate from PR's "systems" format if present
    if isinstance(raw, dict) and isinstance(raw.get("systems"), dict):
        systems = raw["systems"]
        providers = {}
        if "nous_portal" in systems:
            providers["nous"] = systems["nous_portal"]
        return {"version": AUTH_STORE_VERSION, "providers": providers,
                "active_provider": "nous" if providers else None}

    return {"version": AUTH_STORE_VERSION, "providers": {}}


def _save_auth_store(auth_store: Dict[str, Any], target_path: Optional[Path] = None) -> Path:
    # target_path=None preserves the existing contract (write the active
    # store at _auth_file_path()). An explicit path lets callers persist a
    # specific store — e.g. the global-root write-through for rotating xAI
    # OAuth grants (#43589) — reusing this function's atomic O_EXCL + 0o600
    # write so the root auth.json gets the same TOCTOU-safe treatment.
    auth_file = target_path if target_path is not None else _auth_file_path()
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    # Tighten parent dir to 0o700 so siblings can't traverse to creds.
    # No-op on Windows (POSIX mode bits not enforced); ignore failures.
    # secure_parent_dir refuses to chmod / or top-level dirs (#25821).
    secure_parent_dir(auth_file)
    auth_store["version"] = AUTH_STORE_VERSION
    auth_store["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(auth_store, indent=2) + "\n"
    tmp_path = auth_file.with_name(f"{auth_file.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        # Create with 0o600 atomically via os.open(O_EXCL) + fdopen to close
        # the TOCTOU window where default umask (often 0o644) briefly exposed
        # OAuth tokens to other local users between open() and chmod().
        # Mirrors agent/google_oauth.py (#19673) and tools/mcp_oauth.py (#21148).
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, auth_file)
        try:
            dir_fd = os.open(str(auth_file.parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    # Restrict file permissions to owner only
    try:
        auth_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return auth_file


def is_runtime_provider_routable(provider_id: str) -> bool:
    """Return whether runtime resolution recognizes a provider identity.

    This is a capability check, not a credential check. It follows the same
    alias/plugin-aware normalization as ``resolve_provider`` while preserving
    special runtime identities that intentionally live outside the registry.
    """
    normalized = (provider_id or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"auto", "openrouter", "custom", "moa"}:
        return True
    if normalized.startswith("custom:"):
        return True
    try:
        resolve_provider(normalized)
    except AuthError:
        return False
    return True


def read_credential_pool(provider_id: Optional[str] = None) -> Dict[str, Any]:
    """Return the persisted credential pool, or one provider slice.

    In profile mode, the profile's credential pool is authoritative. If a
    provider has no entries in the profile, entries from the global-root
    ``auth.json`` are used as a read-only fallback — so workers spawned in a
    profile can see providers that were only authenticated at global scope.

    Profile entries always win: the global fallback only applies per-provider
    when the profile has zero entries for that provider. Once the user runs
    ``son-of-anton auth add <provider>`` inside the profile, profile entries
    fully shadow global for that provider on the next read.

    Writes always go to the profile (``write_credential_pool`` is unchanged).
    See issue #18594 follow-up.
    """
    auth_store = _load_auth_store()
    pool = auth_store.get("credential_pool")
    if not isinstance(pool, dict):
        pool = {}

    global_pool: Dict[str, Any] = {}
    global_store = _load_global_auth_store()
    maybe_global_pool = global_store.get("credential_pool") if global_store else None
    if isinstance(maybe_global_pool, dict):
        global_pool = maybe_global_pool

    if provider_id is None:
        merged = dict(pool)
        for gp_key, gp_entries in global_pool.items():
            if not isinstance(gp_entries, list) or not gp_entries:
                continue
            # Per-provider shadowing: profile wins whenever it has ANY entries.
            existing = merged.get(gp_key)
            if isinstance(existing, list) and existing:
                continue
            merged[gp_key] = list(gp_entries)
        return merged

    provider_entries = pool.get(provider_id)
    if isinstance(provider_entries, list) and provider_entries:
        return list(provider_entries)
    # Profile has no entries for this provider — fall back to global.
    global_entries = global_pool.get(provider_id)
    return list(global_entries) if isinstance(global_entries, list) else []


_POOL_STATUS_FIELDS = (
    "last_status",
    "last_status_at",
    "last_error_code",
    "last_error_reason",
    "last_error_message",
    "last_error_reset_at",
)


def _merge_disk_cooldown_state(
    entry: Dict[str, Any],
    disk_entry: Optional[Dict[str, Any]],
    provider_id: str,
) -> Dict[str, Any]:
    """Keep a newer on-disk cooldown/quarantine over a stale in-memory one.

    ``write_credential_pool`` callers persist an in-memory snapshot that may
    predate another process marking the same credential exhausted or dead
    (last-writer-wins lost update).  Without this merge, process B's later
    rewrite resurrects a rate-limited key as healthy and both processes
    resume hammering it.  Adopt the on-disk status fields only when they are
    strictly more recent (by ``last_status_at``) AND still binding — a DEAD
    marker, or an EXHAUSTED cooldown that has not yet expired.  Expired
    cooldowns are not resurrected, so the pool's own expiry-clear (which
    resets ``last_status_at`` to None) is never overridden.
    """
    if not isinstance(disk_entry, dict):
        return entry
    try:
        from agent.credential_pool import (
            PooledCredential,
            STATUS_DEAD,
            STATUS_EXHAUSTED,
            _exhausted_until,
            _parse_absolute_timestamp,
        )

        disk_status = disk_entry.get("last_status")
        if disk_status not in (STATUS_DEAD, STATUS_EXHAUSTED):
            return entry
        # A token change means the caller re-authed/refreshed this entry and
        # intentionally cleared its status (e.g. the xAI auth-store sync
        # auth_store after a fresh device-code login) — never resurrect the
        # old cooldown onto fresh credentials.
        mem_access = entry.get("access_token") or ""
        disk_access = disk_entry.get("access_token") or ""
        if mem_access and disk_access and mem_access != disk_access:
            return entry
        disk_ts = _parse_absolute_timestamp(disk_entry.get("last_status_at")) or 0.0
        mem_ts = _parse_absolute_timestamp(entry.get("last_status_at")) or 0.0
        if disk_ts <= mem_ts:
            return entry
        if disk_status == STATUS_EXHAUSTED:
            until = _exhausted_until(
                PooledCredential.from_dict(provider_id, disk_entry)
            )
            if until is None or until <= time.time():
                return entry
        merged_entry = dict(entry)
        for status_field in _POOL_STATUS_FIELDS:
            merged_entry[status_field] = disk_entry.get(status_field)
        return merged_entry
    except Exception:  # pragma: no cover - best-effort merge
        return entry


def write_credential_pool(
    provider_id: str,
    entries: List[Dict[str, Any]],
    *,
    removed_ids: Optional[Iterable[str]] = None,
) -> Path:
    """Persist one provider's credential pool under auth.json.

    This is the final disk-boundary guard for borrowed/reference-only
    credentials. Callers may pass raw dictionaries, so sanitize here even when
    ``PooledCredential.to_dict()`` already did the same work upstream.

    Re-read the on-disk pool under the same lock and merge entries present on
    disk but missing from ``entries``. Those were added by another process after
    the caller loaded its in-memory snapshot; without this merge a later
    rotation/exhaustion rewrite drops the concurrent credential.

    For entries present on BOTH sides, status fields are merged by
    ``last_status_at`` recency via ``_merge_disk_cooldown_state`` so a stale
    snapshot cannot erase a cooldown/quarantine another process just wrote.

    Pass ``removed_ids`` for entries the caller intentionally removed, so the
    merge does not resurrect them from the on-disk copy.
    """
    removed = {rid for rid in (removed_ids or ()) if rid}
    with _auth_store_lock():
        auth_store = _load_auth_store()
        pool = auth_store.get("credential_pool")
        if not isinstance(pool, dict):
            pool = {}
            auth_store["credential_pool"] = pool
        sanitized_entries = [
            sanitize_borrowed_credential_payload(entry, provider_id)
            if isinstance(entry, dict) else entry
            for entry in entries
        ]
        existing = pool.get(provider_id)
        existing_list = existing if isinstance(existing, list) else []
        existing_by_id = {
            entry.get("id"): entry
            for entry in existing_list
            if isinstance(entry, dict) and entry.get("id")
        }
        new_ids = {
            entry.get("id")
            for entry in sanitized_entries
            if isinstance(entry, dict) and entry.get("id")
        }
        merged: List[Dict[str, Any]] = [
            _merge_disk_cooldown_state(
                entry, existing_by_id.get(entry.get("id")), provider_id
            )
            if isinstance(entry, dict)
            else entry
            for entry in sanitized_entries
        ]
        for disk_entry in existing_list:
            if not isinstance(disk_entry, dict):
                continue
            disk_id = disk_entry.get("id")
            if not disk_id or disk_id in new_ids or disk_id in removed:
                continue
            merged.append(sanitize_borrowed_credential_payload(disk_entry, provider_id))
        pool[provider_id] = merged
        return _save_auth_store(auth_store)


def suppress_credential_source(provider_id: str, source: str) -> None:
    """Mark a credential source as suppressed so it won't be re-seeded.

    Older auth stores may represent a provider's suppressed sources as a
    mapping.  Treat its keys as source names and migrate the value to the
    canonical list form before appending the requested source.
    """
    with _auth_store_lock():
        auth_store = _load_auth_store()
        suppressed = auth_store.get("suppressed_sources")
        if not isinstance(suppressed, dict):
            suppressed = {}
            auth_store["suppressed_sources"] = suppressed

        raw_sources = suppressed.get(provider_id)
        if isinstance(raw_sources, list):
            provider_list = raw_sources
        elif isinstance(raw_sources, dict):
            provider_list = [str(name) for name in raw_sources]
            suppressed[provider_id] = provider_list
        else:
            provider_list = []
            suppressed[provider_id] = provider_list

        if source not in provider_list:
            provider_list.append(source)
        _save_auth_store(auth_store)


def is_source_suppressed(provider_id: str, source: str) -> bool:
    """Check if a credential source has been suppressed by the user."""
    try:
        auth_store = _load_auth_store()
        suppressed = auth_store.get("suppressed_sources", {})
        return source in suppressed.get(provider_id, [])
    except Exception:
        return False


def unsuppress_credential_source(provider_id: str, source: str) -> bool:
    """Clear a suppression marker so the source will be re-seeded on the next load.

    Returns True if a marker was cleared, False if no marker existed.
    """
    with _auth_store_lock():
        auth_store = _load_auth_store()
        suppressed = auth_store.get("suppressed_sources")
        if not isinstance(suppressed, dict):
            return False
        raw_sources = suppressed.get(provider_id)
        if isinstance(raw_sources, dict):
            provider_list = [str(name) for name in raw_sources]
            suppressed[provider_id] = provider_list
        elif isinstance(raw_sources, list):
            provider_list = raw_sources
        else:
            return False
        if source not in provider_list:
            return False
        provider_list.remove(source)
        if not provider_list:
            suppressed.pop(provider_id, None)
        if not suppressed:
            auth_store.pop("suppressed_sources", None)
        _save_auth_store(auth_store)
        return True


def get_active_provider() -> Optional[str]:
    """Return the currently active provider ID from auth store."""
    auth_store = _load_auth_store()
    return auth_store.get("active_provider")


def is_provider_explicitly_configured(provider_id: str) -> bool:
    """Return True only if the user has explicitly configured this provider.

    Checks:
      1. active_provider in auth.json matches
      2. model.provider in config.toml matches
      3. Provider-specific env vars are set (e.g. ANTHROPIC_API_KEY)

    This is used to gate auto-discovery of external credentials (e.g.
    Claude Code's ~/.claude/.credentials.json) so they are never used
    without the user's explicit choice.  See PR #4210 for the same
    pattern applied to the setup wizard gate.
    """
    normalized = (provider_id or "").strip().lower()

    # 1. Check auth.json active_provider
    try:
        auth_store = _load_auth_store()
        active = (auth_store.get("active_provider") or "").strip().lower()
        if active and active == normalized:
            return True
    except Exception:
        pass

    # 2. Check config.toml model.provider and other explicit provider slots.
    try:
        from son_of_anton_cli.config import load_config
        cfg = load_config()
        model_cfg = cfg.get("model")
        if isinstance(model_cfg, dict):
            cfg_provider = (model_cfg.get("provider") or "").strip().lower()
            if cfg_provider == normalized:
                return True

        # MoA presets are explicit model selections too.  A user who configured
        # ``provider: anthropic`` as a MoA advisor/aggregator has opted Son of Anton
        # into using Anthropic credentials for that slot even when the main
        # session model is another provider.  Without this, Claude Code OAuth
        # entries are pruned/ignored by credential_pool.load_pool("anthropic"),
        # so MoA Anthropic advisors fail with "no ANTHROPIC_API_KEY" while the
        # normal model picker says Anthropic is logged in.
        def _slot_matches_provider(slot):
            return (
                isinstance(slot, dict)
                and (slot.get("provider") or "").strip().lower() == normalized
            )

        moa_cfg = cfg.get("moa")
        if isinstance(moa_cfg, dict):
            for slot in moa_cfg.get("reference_models") or []:
                if _slot_matches_provider(slot):
                    return True
            if _slot_matches_provider(moa_cfg.get("aggregator")):
                return True
            presets = moa_cfg.get("presets")
            if isinstance(presets, dict):
                for preset in presets.values():
                    if not isinstance(preset, dict):
                        continue
                    for slot in preset.get("reference_models") or []:
                        if _slot_matches_provider(slot):
                            return True
                    if _slot_matches_provider(preset.get("aggregator")):
                        return True
    except Exception:
        pass

    # 3. Check provider-specific env vars
    # Exclude CLAUDE_CODE_OAUTH_TOKEN — it's set by Claude Code itself,
    # not by the user explicitly configuring anthropic in Son of Anton.
    _IMPLICIT_ENV_VARS = {"CLAUDE_CODE_OAUTH_TOKEN"}
    pconfig = PROVIDER_REGISTRY.get(normalized)
    # Fallback to ProviderDef from models.dev catalog when the provider
    # isn't in the manually-maintained PROVIDER_REGISTRY (e.g. openrouter).
    # Both expose .auth_type and .api_key_env_vars with the same shape.
    if pconfig is None:
        from son_of_anton_cli.providers import get_provider
        pconfig = get_provider(normalized)
    if pconfig and pconfig.auth_type == "api_key":
        for env_var in pconfig.api_key_env_vars:
            if env_var in _IMPLICIT_ENV_VARS:
                continue
            if has_usable_secret(os.getenv(env_var, "")):
                return True

    # 4. Check persisted credential-pool entries that came from EXPLICIT flows
    # the user initiated inside Son of Anton (manual add / device-code / PKCE), plus
    # env-backed pool entries. This intentionally excludes ambient borrowed
    # sources like gh_cli / claude_code / qwen-cli.
    try:
        for entry in read_credential_pool(normalized):
            if not isinstance(entry, dict):
                continue
            source = str(entry.get("source") or "").strip().lower()
            if not source:
                continue
            if source.startswith("env:"):
                # A stale env-seeded pool entry survives in auth.json after
                # the user deletes the env var (#55790) — only count it when
                # the referenced var still resolves to a usable secret NOW.
                env_var = entry.get("source", "").split(":", 1)[1].strip()
                if env_var and has_usable_secret(os.getenv(env_var, "")):
                    return True
                continue
            if (
                source in {"device_code", "loopback_pkce", "son_of_anton_pkce", "manual"}
                or source.startswith("manual:")
            ):
                return True
    except Exception:
        pass

    return False


def deactivate_provider() -> None:
    """
    Clear active_provider in auth.json without deleting credentials.
    Used when the user switches to a non-OAuth provider (OpenRouter, custom)
    so auto-resolution doesn't keep picking the OAuth provider.
    """
    with _auth_store_lock():
        auth_store = _load_auth_store()
        auth_store["active_provider"] = None
        _save_auth_store(auth_store)


# =============================================================================
# Provider Resolution — picks which provider to use
# =============================================================================


def _get_config_hint_for_unknown_provider(provider_name: str) -> str:
    """Return a helpful hint string when provider resolution fails.

    Checks for common config.toml mistakes (malformed custom_providers, etc.)
    and returns a human-readable diagnostic, or empty string if nothing found.
    """
    try:
        from son_of_anton_cli.config import validate_config_structure
        issues = validate_config_structure()
        if not issues:
            return ""

        lines = ["Config issue detected — run 'son-of-anton doctor' for full diagnostics:"]
        for ci in issues:
            prefix = "ERROR" if ci.severity == "error" else "WARNING"
            lines.append(f"  [{prefix}] {ci.message}")
            # Show first line of hint
            first_hint = ci.hint.splitlines()[0] if ci.hint else ""
            if first_hint:
                lines.append(f"    → {first_hint}")
        return "\n".join(lines)
    except Exception:
        return ""


def resolve_provider(
    requested: Optional[str] = None,
    *,
    explicit_api_key: Optional[str] = None,
    explicit_base_url: Optional[str] = None,
) -> str:
    """
    Determine which inference provider to use.

    Priority (when requested="auto" or None) — explicit user intent wins over a
    stale logged-in OAuth provider (#29285):
    1. Explicit CLI api_key/base_url -> "custom"
    2. config.toml `model.provider`
    3. OPENAI_API_KEY env var -> "custom"
    4. Provider-specific API keys (GLM, Kimi, MiniMax, ...) -> that provider
    5. auth.json `active_provider` (logged-in OAuth) — last-resort fallback
    6. AWS Bedrock credential chain
    7. Error (no provider configured)
    """
    normalized = (requested or "auto").strip().lower()

    # Normalize provider aliases
    _PROVIDER_ALIASES = {
        "glm": "zai", "z-ai": "zai", "z.ai": "zai", "zhipu": "zai",
        "google": "gemini", "google-gemini": "gemini", "google-ai-studio": "gemini",
        "kimi": "kimi-coding", "kimi-for-coding": "kimi-coding", "moonshot": "kimi-coding",
        "kimi-cn": "kimi-coding-cn", "moonshot-cn": "kimi-coding-cn",
        "step": "stepfun", "stepfun-coding-plan": "stepfun",
        "arcee-ai": "arcee", "arceeai": "arcee",
        "gmi-cloud": "gmi", "gmicloud": "gmi",
        "actual-computer": "actual", "actualcomputer": "actual", "aci": "actual",
        "minimax-china": "minimax-cn", "minimax_cn": "minimax-cn",
        "minimax-portal": "minimax-oauth", "minimax-global": "minimax-oauth", "minimax_oauth": "minimax-oauth",
        "alibaba_coding": "alibaba-coding-plan", "alibaba-coding": "alibaba-coding-plan",
        "alibaba_coding_plan": "alibaba-coding-plan",
        "claude": "anthropic", "claude-code": "anthropic",
        "github": "copilot", "github-copilot": "copilot",
        "github-models": "copilot", "github-model": "copilot",
        "github-copilot-acp": "copilot-acp", "copilot-acp-agent": "copilot-acp",
        "aigateway": "ai-gateway", "vercel": "ai-gateway", "vercel-ai-gateway": "ai-gateway",
        "opencode": "opencode-zen", "zen": "opencode-zen",
        "free": "opencode-free", "opencode_free": "opencode-free",
        "hf": "huggingface", "hugging-face": "huggingface", "huggingface-hub": "huggingface",
        "mimo": "xiaomi", "xiaomi-mimo": "xiaomi",
        "tencent": "tencent-tokenhub", "tokenhub": "tencent-tokenhub",
        "tencent-cloud": "tencent-tokenhub", "tencentmaas": "tencent-tokenhub",
        "go": "opencode-go", "opencode-go-sub": "opencode-go",
        "kilo": "kilocode", "kilo-code": "kilocode", "kilo-gateway": "kilocode",
        "lmstudio": "lmstudio", "lm-studio": "lmstudio", "lm_studio": "lmstudio",
        # Local server aliases — route through the generic custom provider
        "local": "custom",
        "vllm": "custom", "llamacpp": "custom",
        "llama.cpp": "custom", "llama-cpp": "custom",
    }
    # Extend with aliases declared in plugins/model-providers/<name>/ that aren't already mapped.
    # This keeps providers/ as the single source for new aliases while the
    # hardcoded dict above remains authoritative for existing ones.
    try:
        from providers import list_providers as _lp
        for _pp in _lp():
            for _alias in _pp.aliases:
                if _alias not in _PROVIDER_ALIASES:
                    _PROVIDER_ALIASES[_alias] = _pp.name
    except Exception:
        pass
    normalized = _PROVIDER_ALIASES.get(normalized, normalized)

    if normalized == "custom":
        return "custom"
    if normalized in PROVIDER_REGISTRY:
        return normalized
    if normalized != "auto":
        # Check for common config.toml issues that cause this error
        _config_hint = _get_config_hint_for_unknown_provider(normalized)
        msg = f"Unknown provider '{normalized}'."
        if _config_hint:
            msg += f"\n\n{_config_hint}"
        else:
            msg += " Check 'son-of-anton model' for available providers, or run 'son-of-anton doctor' to diagnose config issues."
        raise AuthError(msg, code="invalid_provider")

    # Explicit one-off CLI creds always mean the standard endpoint
    if explicit_api_key or explicit_base_url:
        return "custom"

    # Provider precedence for the auto-path (#29285): explicit user intent must
    # win over a stale logged-in OAuth `active_provider`. Order matches the
    # docstring: 1. explicit CLI creds  2. config.toml `model.provider`
    # 3. OPENAI env key  4. provider-specific env keys
    # 5. auth.json `active_provider` (OAuth)  6. Bedrock  7. error.
    # The normal chat/gateway path resolves config.provider upstream in
    # resolve_requested_provider() before ever reaching "auto"; this duplicate
    # check is the safety net for the lone direct caller (main.py resolve_provider
    # ("auto")) and any future bypass of that stage.
    _model_cfg: Any = None
    try:
        from son_of_anton_cli.config import load_config

        _model_cfg = (load_config() or {}).get("model")
        if isinstance(_model_cfg, dict):
            _cfg_provider = _model_cfg.get("provider")
            if isinstance(_cfg_provider, str) and _cfg_provider.strip().lower() in PROVIDER_REGISTRY:
                return _cfg_provider.strip().lower()
    except Exception as e:
        logger.debug("Could not read config.toml model.provider for auto-resolution: %s", e)

    # Scope-aware key reads: under multiplex a secondary profile's API keys
    # live only in its secret scope, not os.environ — a bare getenv here
    # would find nothing and auto-resolution would report "No LLM provider
    # configured" for every secondary profile (same class as #86905).
    # Catch ONLY ImportError: any other failure inside auxiliary_client must
    # propagate — silently falling back to os.getenv would reintroduce the
    # very fail-open this PR removes, with zero trace.
    try:
        from agent.auxiliary_client import _scoped_key_env
    except ImportError:
        logger.warning(
            "agent.auxiliary_client unavailable (%s); provider auto-detection "
            "will read keys from the process environment only — under "
            "multiplex, secondary profiles may report 'No LLM provider'.",
            "import failed",
        )

        def _scoped_key_env(name: str) -> str:
            return os.getenv(name) or ""

    if has_usable_secret(_scoped_key_env("OPENAI_API_KEY")):
        return "custom"

    # Determine the logged-in OAuth provider up front so the env-key loop below
    # can WARN when an exported API key preempts it (#29285 transparency). The
    # actual OAuth fallback (tier 6) still happens later if nothing else matches.
    _oauth_active: Optional[str] = None
    try:
        _store = _load_auth_store()
        _maybe = _store.get("active_provider")
        if _maybe and _maybe in PROVIDER_REGISTRY and get_auth_status(_maybe).get("logged_in"):
            _oauth_active = _maybe
    except Exception as e:
        logger.debug("Could not pre-read active auth provider: %s", e)

    # Auto-detect API-key providers by checking their env vars
    for pid, pconfig in PROVIDER_REGISTRY.items():
        if pconfig.auth_type != "api_key":
            continue
        # GitHub tokens are commonly present for repo/tool access but should not
        # hijack inference auto-selection unless the user explicitly chooses
        # Copilot/GitHub Models as the provider. LM Studio is a local server
        # whose availability isn't implied by LM_API_KEY presence (it may be
        # offline, and the no-auth setup uses a placeholder value), so it
        # also requires explicit selection.
        if pid in {"copilot", "lmstudio"}:
            continue
        for env_var in pconfig.api_key_env_vars:
            if has_usable_secret(_scoped_key_env(env_var)):
                # An exported API key now wins over a logged-in OAuth provider
                # (the #29285 fix). Surface that so a user who deliberately uses
                # OAuth but has a stale key in ~/.son-of-anton/.env isn't silently
                # switched without knowing why.
                if _oauth_active and _oauth_active != pid:
                    logger.warning(
                        "Provider resolved to %r via %s, preempting your "
                        "logged-in OAuth provider %r. If you meant to use the "
                        "OAuth login, unset %s or set `model.provider` "
                        "explicitly.",
                        pid, env_var, _oauth_active, env_var,
                    )
                return pid

    # Logged-in OAuth provider (auth.json `active_provider`) — a LAST-RESORT
    # fallback, chosen only when the user expressed no other preference above.
    # Previously this sat ABOVE the env-var/config checks, so a stale OAuth
    # login silently overrode an explicit `model.provider` or an exported API
    # key (#29285). Demoted here so explicit intent always wins.
    if _oauth_active:
        # Surface the silent-override case the issue reported: a populated
        # `model` config that lacks a `provider` key falls through to OAuth.
        if isinstance(_model_cfg, dict) and _model_cfg and not _model_cfg.get("provider"):
            logger.warning(
                "Provider resolved to logged-in OAuth provider %r because "
                "config.toml `model` has no `provider` key. If you meant a "
                "different provider, set `model.provider` explicitly.",
                _oauth_active,
            )
        return _oauth_active

    raise AuthError(
        "No inference provider configured. Run 'son-of-anton model' to choose a "
        "provider and model, or set an API key (OPENROUTER_API_KEY, "
        "OPENAI_API_KEY, etc.) in ~/.son-of-anton/.env.",
        code="no_provider_configured",
    )


# =============================================================================
# Timestamp / TTL helpers
# =============================================================================

def _parse_iso_timestamp(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# Allowlist of valid Nous Portal hosts. A portal_base_url outside this
# set is treated as a misconfiguration and falls back to the default.
# "localhost" / "127.0.0.1" are valid for local development and testing.


# Allowlist of hosts the Nous Portal proxy is willing to forward inference
# JWTs to. Sending a bearer anywhere else would leak it.
#
# This is consulted only for URLs coming from the NETWORK side (Portal
# refresh responses). User-controlled env-var overrides
# (NOUS_INFERENCE_BASE_URL) bypass validation — that's the documented
# dev/staging escape hatch and the env source is already trusted (the
# user set it themselves).


def _decode_jwt_claims(token: Any) -> Dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


# =============================================================================
# Spotify auth — PKCE tokens stored in ~/.son-of-anton/auth.json
# =============================================================================


def _is_remote_session() -> bool:
    """Detect environments where loopback OAuth can't reach the local browser.

    Historically only SSH was checked, but #26923 surfaced that
    **browser-only remote consoles** (GCP Cloud Shell, GitHub
    Codespaces, AWS EC2 Instance Connect, Gitpod, Replit, etc.) hit
    the exact same problem — the user has a browser on their laptop
    but the loopback listener is bound on the remote VM that the
    laptop's browser can't reach.  These environments typically don't
    set ``SSH_CLIENT`` / ``SSH_TTY``, so the SSH-only check left
    them with no guidance and no fallback.
    """
    if os.getenv("SSH_CLIENT") or os.getenv("SSH_TTY"):
        return True
    # Browser-only remote IDEs / cloud shells.  Keep this list narrow
    # (well-known, documented env vars set by the host platform) so
    # we don't falsely trip on a developer's local shell.
    for var in (
        "CLOUD_SHELL",         # GCP Cloud Shell
        "CODESPACES",          # GitHub Codespaces
        "CODESPACE_NAME",      # GitHub Codespaces (alt)
        "GITPOD_WORKSPACE_ID", # Gitpod
        "REPL_ID",             # Replit
        "STACKBLITZ",          # StackBlitz
    ):
        if os.getenv(var):
            return True
    return False


# Console/text-mode browsers that ``webbrowser`` will happily launch INSIDE
# the terminal.  Opening one of these is worse than not opening anything —
# it hijacks the user's TTY with an unusable text browser (the xAI OAuth
# "Account Management" page rendered in w3m, reported May 2026) instead of
# letting them copy the URL to a real browser.  When the resolved browser is
# one of these we refuse to auto-open and fall back to the print-the-URL
# path, same as a remote session.
_CONSOLE_BROWSER_NAMES: FrozenSet[str] = frozenset(
    {
        "w3m",
        "lynx",
        "links",
        "links2",
        "elinks",
        "www-browser",
        "browsh",  # TUI browser — still hijacks the terminal
    }
)


def _can_open_graphical_browser() -> bool:
    """Return True only when a *graphical* browser is likely to open.

    ``webbrowser.open()`` resolves to whatever the platform offers, and on a
    headless / CLI-only Linux box with no GUI browser installed that is often
    a text-mode browser (w3m/lynx/links) which launches inside the terminal
    and takes over the user's session.  This guard distinguishes "a real
    windowed browser will pop up" from "a console browser will hijack the
    TTY", so callers can fall back to printing the URL instead.

    Heuristics:
      * Respect ``$BROWSER`` — if it names a known console browser, refuse.
      * On Linux, require a display server (``$DISPLAY`` / ``$WAYLAND_DISPLAY``)
        unless ``$BROWSER`` points at something graphical; no display server
        almost always means no GUI browser.
      * Ask ``webbrowser.get()`` what it resolved to and refuse when the
        underlying command is a known console browser.
      * macOS and Windows always have a usable default GUI browser.
    """
    import webbrowser as _webbrowser

    def _names_console_browser(value: str) -> bool:
        token = value.strip().split()[0] if value.strip() else ""
        base = os.path.basename(token).lower()
        return base in _CONSOLE_BROWSER_NAMES

    browser_env = os.environ.get("BROWSER", "")
    if browser_env and _names_console_browser(browser_env):
        return False

    if sys.platform.startswith("linux"):
        has_display = bool(
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        )
        # An explicit graphical $BROWSER can work without $DISPLAY in odd
        # setups, but a console $BROWSER already returned False above, so the
        # only way to reach here with a $BROWSER set is a graphical one.
        if not has_display and not browser_env:
            return False

    try:
        controller = _webbrowser.get()
    except Exception:
        # No browser resolvable at all → definitely don't auto-open.
        return False

    candidate = (
        getattr(controller, "name", "")
        or getattr(controller, "basename", "")
        or ""
    )
    if candidate and _names_console_browser(candidate):
        return False

    return True


# =============================================================================
# xAI Grok OAuth — tokens stored in ~/.son-of-anton/auth.json
# =============================================================================

def get_api_key_provider_status(provider_id: str) -> Dict[str, Any]:
    """Status snapshot for API-key providers (z.ai, Kimi, MiniMax)."""
    pconfig = PROVIDER_REGISTRY.get(provider_id)
    if not pconfig or pconfig.auth_type != "api_key":
        return {"configured": False}

    # Keyless providers (opencode-free) are served anonymously: no credential
    # exists, so every install counts as configured/logged in. Derived from
    # the SonOfAntonOverlay keyless flag — the same source the provider catalog
    # and GUI contract tests use.
    try:
        from son_of_anton_cli.providers import SON_OF_ANTON_OVERLAYS
        _overlay = SON_OF_ANTON_OVERLAYS.get(provider_id)
    except Exception:
        _overlay = None
    if _overlay is not None and getattr(_overlay, "keyless", False):
        return {
            "configured": True,
            "provider": provider_id,
            "name": pconfig.name,
            "key_source": "keyless",
            "base_url": pconfig.inference_base_url,
            "logged_in": True,
        }

    api_key, key_source = _resolve_api_key_provider_secret(provider_id, pconfig)

    env_url = ""
    if pconfig.base_url_env_var:
        env_url = os.getenv(pconfig.base_url_env_var, "").strip()
    base_url = env_url or pconfig.inference_base_url

    return {
        "configured": bool(api_key),
        "provider": provider_id,
        "name": pconfig.name,
        "key_source": key_source,
        "base_url": base_url,
        "logged_in": bool(api_key),
    }


def get_auth_status(provider_id: Optional[str] = None) -> Dict[str, Any]:
    """Generic auth status dispatcher."""
    target = (provider_id or get_active_provider() or "").strip().lower()
    if not target:
        return {"logged_in": False}
    # API-key providers
    pconfig = PROVIDER_REGISTRY.get(target)
    if pconfig and pconfig.auth_type == "api_key":
        return get_api_key_provider_status(target)
    return {"logged_in": False}


def resolve_api_key_provider_credentials(provider_id: str) -> Dict[str, Any]:
    """Resolve API key and base URL for an API-key provider.

    Returns dict with: provider, api_key, base_url, source.
    """
    pconfig = PROVIDER_REGISTRY.get(provider_id)
    if not pconfig or pconfig.auth_type != "api_key":
        raise AuthError(
            f"Provider '{provider_id}' is not an API-key provider.",
            provider=provider_id,
            code="invalid_provider",
        )

    api_key, key_source = _resolve_api_key_provider_secret(provider_id, pconfig)

    env_url = ""
    if pconfig.base_url_env_var:
        env_url = os.getenv(pconfig.base_url_env_var, "").strip()
    base_url = env_url.rstrip("/") if env_url else pconfig.inference_base_url

    # Last-resort guard: an API-key provider must never hand back an empty
    # base URL (a set-but-empty <PROVIDER>_BASE_URL env override otherwise
    # wedges chat inference — #50252).
    if not (isinstance(base_url, str) and base_url.strip()):
        base_url = pconfig.inference_base_url

    return {
        "provider": provider_id,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "source": key_source or "default",
    }


# =============================================================================
# CLI Commands — login / logout
# =============================================================================


def _confirm_selection_guards(
    model_id: str,
    *,
    provider: str = "",
    base_url: str = "",
    api_key: str = "",
    include_kinds: Optional[List[str]] = None,
) -> bool:
    """Prompt before saving a model that trips any selection guard.

    Runs the unified guard registry (cost + data-policy + future guards) via
    :mod:`son_of_anton_cli.model_selection_guards` and shows one [y/N] confirm with
    every warning that fired. Returns True to proceed, False to cancel.
    """
    try:
        from son_of_anton_cli.model_selection_guards import (
            combined_message,
            selection_warnings,
        )

        warnings = selection_warnings(
            model_id,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            include_kinds=include_kinds,
        )
    except Exception:
        warnings = []
    if not warnings:
        return True

    print()
    print("=" * 72)
    print(combined_message(warnings))
    print("=" * 72)
    try:
        response = input("Switch anyway? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    return response in {"y", "yes"}


def _prompt_model_selection(
    model_ids: List[str],
    current_model: str = "",
    pricing: Optional[Dict[str, Dict[str, str]]] = None,
    unavailable_models: Optional[List[str]] = None,
    portal_url: str = "",
    unavailable_message: str = "",
    confirm_provider: str = "",
    confirm_base_url: str = "",
    confirm_api_key: str = "",
) -> Optional[str]:
    """Interactive model selection. Puts current_model first with a marker. Returns chosen model ID or None.

    If *pricing* is provided (``{model_id: {prompt, completion}}``), a compact
    price indicator is shown next to each model in aligned columns.

    If *unavailable_models* is provided, those models are shown grayed out
    and unselectable, with an upgrade link to *portal_url*.
    """
    from son_of_anton_cli.cli_output import line_input
    from son_of_anton_cli.models import (
        _format_price_per_mtok,
        compute_sale_discount,
    )

    _unavailable = unavailable_models or []
    # Sale chrome (★ / -N% / was) is Nous Portal-only — never for OpenRouter
    # or other providers even if pricing.original is somehow present.
    sale_chrome = (confirm_provider or "").strip().lower() == "nous"

    def _confirmed_selection(mid: str) -> Optional[str]:
        if not mid:
            return None
        # Unified guard registry (son_of_anton_cli.model_selection_guards): the cost
        # guard only runs when a provider is known (pricing lookups need one);
        # id-keyed guards like the data-policy guard always run — they must
        # fire even via a custom endpoint or gateway.
        _kinds = None if confirm_provider else ["data_policy"]
        if not _confirm_selection_guards(
            mid,
            provider=confirm_provider,
            base_url=confirm_base_url,
            api_key=confirm_api_key,
            include_kinds=_kinds,
        ):
            return None
        return mid

    # Reorder: current model first, then the rest (deduplicated)
    ordered = []
    if current_model and current_model in model_ids:
        ordered.append(current_model)
    for mid in model_ids:
        if mid not in ordered:
            ordered.append(mid)

    # All models for column-width computation (selectable + unavailable)
    all_models = list(ordered) + list(_unavailable)

    # Column-aligned labels when pricing is available
    has_pricing = bool(pricing and any(pricing.get(m) for m in all_models))
    # Leave room for a leading "★ " on sale rows (Nous only).
    name_pad = 3 if sale_chrome else 2
    name_col = (
        max((len(m) for m in all_models), default=0) + name_pad
        if has_pricing
        else 0
    )

    # Pre-compute formatted prices and sale chrome.
    # (inp, out, cache, pct|None, was_inp, was_out)
    # Sale chrome is drawn as curses/ANSI segments (yellow % / dim "was"),
    # not baked into a single plain string — curses addnstr would otherwise
    # render escape bytes literally.
    _price_cache: dict[str, tuple[str, str, str, int | None, str, str]] = {}
    price_col = 3  # minimum width
    cache_col = 0  # only set if any model has cache pricing
    has_cache = False
    any_on_sale = False
    _DIM = "\033[2m"
    _RESET = "\033[0m"
    if has_pricing:
        for mid in all_models:
            p = pricing.get(mid)  # type: ignore[union-attr]
            pct: int | None = None
            was_inp = was_out = ""
            if p:
                inp = _format_price_per_mtok(p.get("prompt", ""))
                out = _format_price_per_mtok(p.get("completion", ""))
                cache_read = p.get("input_cache_read", "")
                cache = _format_price_per_mtok(cache_read) if cache_read else ""
                if cache:
                    has_cache = True
                if sale_chrome:
                    sale = compute_sale_discount(
                        p.get("prompt", ""),
                        p.get("completion", ""),
                        p.get("original"),
                    )
                    if sale is not None:
                        any_on_sale = True
                        pct, was_prompt_raw, was_out_raw = sale
                        was_inp = (
                            _format_price_per_mtok(was_prompt_raw)
                            if was_prompt_raw != ""
                            else "?"
                        )
                        was_out = (
                            _format_price_per_mtok(was_out_raw)
                            if was_out_raw != ""
                            else "?"
                        )
            else:
                inp, out, cache = "", "", ""
            _price_cache[mid] = (inp, out, cache, pct, was_inp, was_out)
            price_col = max(price_col, len(inp), len(out))
            cache_col = max(cache_col, len(cache))
        if has_cache:
            cache_col = max(cache_col, 5)  # minimum: "Cache" header

    def _label_segments(mid):
        """Build a rich radiolist row: yellow ★/% , dim was, plain prices."""
        if not has_pricing:
            segs: list[tuple[str, str | None]] = [(mid, None)]
            if mid == current_model:
                segs.append(("  ← currently in use", None))
            return segs

        inp, out, cache, pct, was_inp, was_out = _price_cache.get(
            mid, ("", "", "", None, "", "")
        )
        on_sale = pct is not None
        # Reserve 2 columns for "★ " so sale and non-sale names share alignment.
        star_w = 2
        if on_sale:
            name_segs: list[tuple[str, str | None]] = [
                ("★ ", "yellow"),
                (f"{mid:<{name_col - star_w}}", None),
            ]
        else:
            name_segs = [(f"{mid:<{name_col}}", None)]

        price_part = f" {inp:>{price_col}}  {out:>{price_col}}"
        if has_cache:
            price_part += f"  {cache:>{cache_col}}"
        segs = [*name_segs, (price_part, None)]
        if on_sale:
            segs.append((f"  -{pct}%", "yellow"))
            segs.append((f"  was {was_inp}/{was_out}", "dim"))
        if mid == current_model:
            segs.append(("  ← currently in use", None))
        return segs

    def _label(mid):
        return "".join(text for text, _style in _label_segments(mid))

    # Default cursor on the current model (index 0 if it was reordered to top)
    default_idx = 0

    # Build a pricing header hint for the menu title
    menu_title = "Select default model:"
    if has_pricing:
        # Align the header with the model column.
        # Each choice is "  {label}" (2 spaces) and we prepend
        # a 3-char cursor region ("-> " or "   "), so content starts at col 5.
        pad = " " * 5
        header = f"\n{pad}{'':>{name_col}} {'In':>{price_col}}  {'Out':>{price_col}}"
        if has_cache:
            header += f"  {'Cache':>{cache_col}}"
        # Legend lives on the column-header line so it reads as a key
        # (★ = on sale), not a fake menu row.
        menu_title += header + "  $/Mtok"
        if any_on_sale:
            menu_title += "  ★ = on sale"

    # Try arrow-key menu first, fall back to number input.
    try:
        from son_of_anton_cli.curses_ui import curses_radiolist

        choices = [_label_segments(mid) for mid in ordered]
        choices.append("Enter custom model name")
        choices.append("Skip (keep current)")

        _upgrade_url = (portal_url or "").rstrip("/")
        unavailable_footer = unavailable_message.strip()
        if not unavailable_footer and _unavailable:
            unavailable_footer = f"Upgrade at {_upgrade_url} for paid models"

        # The pricing column header (and any unavailable-models block) is shown
        # as a multi-line description above the list so it survives the curses
        # screen clear. menu_title already embeds the aligned price header.
        desc_lines: list[str] = []
        if has_pricing:
            # menu_title is "Select default model:\n<pad><header>  $/Mtok\n…"
            # Keep only the header/legend portion for the description.
            header_part = menu_title.split("\n", 1)
            if len(header_part) > 1:
                desc_lines.extend(header_part[1].splitlines())
        if _unavailable:
            for mid in _unavailable:
                desc_lines.append(f"   {_label(mid)}")
            desc_lines.append(f"  ── {unavailable_footer} ──")
        description = "\n".join(desc_lines) if desc_lines else None

        # Search haystacks keep pricing labels visible while adding aliases
        # for brand-less wire ids (e.g. Kimi Coding `k3` ↔ query "kimi").
        from son_of_anton_cli.model_search import model_search_text

        model_search_labels = []
        for mid in ordered:
            label = _label(mid)
            haystack = model_search_text(mid)
            # model_search_text always starts with the wire id; only append when
            # aliases add tokens beyond the bare id already in the label.
            model_search_labels.append(
                label if haystack == mid else f"{label} {haystack}"
            )
        model_search_labels.append("Enter custom model name")
        model_search_labels.append("Skip (keep current)")

        idx = curses_radiolist(
            "Select default model:",
            choices,
            selected=default_idx,
            cancel_returns=-1,
            description=description,
            searchable=True,
            search_labels=model_search_labels,
        )
        if idx < 0:
            return None
        print()
        if idx < len(ordered):
            return _confirmed_selection(ordered[idx])
        elif idx == len(ordered):
            try:
                custom = line_input("Enter model name: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            return _confirmed_selection(custom) if custom else None
        return None
    except (ImportError, NotImplementedError, OSError, subprocess.SubprocessError):
        pass

    # Fallback: numbered list (ANSI colors for sale chrome)
    from son_of_anton_cli.curses_ui import format_radio_item_ansi
    from son_of_anton_cli.colors import Colors, color

    for line in menu_title.splitlines():
        if "★" in line:
            print(line.replace("★", color("★", Colors.YELLOW), 1))
        else:
            print(line)
    num_width = len(str(len(ordered) + 2))
    for i, mid in enumerate(ordered, 1):
        print(f"  {i:>{num_width}}. {format_radio_item_ansi(_label_segments(mid))}")
    n = len(ordered)
    print(f"  {n + 1:>{num_width}}. Enter custom model name")
    print(f"  {n + 2:>{num_width}}. Skip (keep current)")

    if _unavailable:
        _upgrade_url = (portal_url or "").rstrip("/")
        unavailable_footer = unavailable_message.strip() or (
            f"Unavailable models (requires paid tier — upgrade at {_upgrade_url})"
        )
        print()
        print(f"  {_DIM}── {unavailable_footer} ──{_RESET}")
        for mid in _unavailable:
            print(f"  {'':>{num_width}}  {_DIM}{_label(mid)}{_RESET}")
    print()

    while True:
        try:
            choice = input(f"Choice [1-{n + 2}] (default: skip): ").strip()
            if not choice:
                return None
            idx = int(choice)
            if 1 <= idx <= n:
                return _confirmed_selection(ordered[idx - 1])
            elif idx == n + 1:
                custom = line_input("Enter model name: ").strip()
                return _confirmed_selection(custom) if custom else None
            elif idx == n + 2:
                return None
            print(f"Please enter 1-{n + 2}")
        except ValueError:
            print("Please enter a number")
        except (KeyboardInterrupt, EOFError):
            return None


def _save_model_choice(model_id: str) -> None:
    """Save the selected model to config.toml (single source of truth).

    The model is stored in config.toml only — NOT in .env.  This avoids
    conflicts in multi-agent setups where env vars would stomp each other.
    """
    from son_of_anton_cli.config import save_config, load_config

    config = load_config()
    # Always use dict format so provider/base_url can be stored alongside
    if isinstance(config.get("model"), dict):
        config["model"]["default"] = model_id
    else:
        config["model"] = {"default": model_id}
    save_config(config)

