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
import shutil
import shlex
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
from urllib.parse import urlparse

from son_of_anton_cli.config import (
    get_son_of_anton_home,
)
from son_of_anton_constants import secure_parent_dir
from agent.credential_persistence import sanitize_borrowed_credential_payload
from utils import atomic_replace, env_float

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

# Nous Portal defaults
DEFAULT_NOUS_PORTAL_URL = "https://portal.nousresearch.com"
DEFAULT_NOUS_INFERENCE_URL = "https://inference-api.nousresearch.com/v1"
DEFAULT_NOUS_CLIENT_ID = "son-of-anton-cli"
NOUS_INFERENCE_INVOKE_SCOPE = "inference:invoke"
NOUS_BILLING_MANAGE_SCOPE = "billing:manage"
DEFAULT_NOUS_SCOPE = NOUS_INFERENCE_INVOKE_SCOPE
NOUS_DEVICE_CODE_SOURCE = "device_code"
NOUS_AUTH_PATH_INVOKE_JWT = "invoke_jwt"
ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120       # refresh 2 min before expiry
NOUS_INVOKE_JWT_MIN_TTL_SECONDS = ACCESS_TOKEN_REFRESH_SKEW_SECONDS
DEFAULT_XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"
MINIMAX_OAUTH_CLIENT_ID = "78257093-7e40-4613-99e0-527b14b39113"
MINIMAX_OAUTH_SCOPE = "group_id profile model.completion"
MINIMAX_OAUTH_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:user_code"
MINIMAX_OAUTH_GLOBAL_BASE = "https://api.minimax.io"
MINIMAX_OAUTH_CN_BASE = "https://api.minimaxi.com"
MINIMAX_OAUTH_GLOBAL_INFERENCE = "https://api.minimax.io/anthropic"
MINIMAX_OAUTH_CN_INFERENCE = "https://api.minimaxi.com/anthropic"
MINIMAX_OAUTH_REFRESH_SKEW_SECONDS = 60
DEFAULT_GITHUB_MODELS_BASE_URL = "https://api.githubcopilot.com"
DEFAULT_COPILOT_ACP_BASE_URL = "acp://copilot"
DEFAULT_OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
DEFAULT_ACTUAL_BASE_URL = "https://api.actual.inc/v1"
DEFAULT_ACTUAL_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
STEPFUN_STEP_PLAN_INTL_BASE_URL = "https://api.stepfun.ai/step_plan/v1"
STEPFUN_STEP_PLAN_CN_BASE_URL = "https://api.stepfun.com/step_plan/v1"
try:  # Version tag for OAuth token-endpoint User-Agents; fall back if unavailable.
    from son_of_anton_cli import __version__ as _SON_OF_ANTON_CLI_VERSION
except Exception:  # pragma: no cover - version import should always succeed
    _SON_OF_ANTON_CLI_VERSION = "unknown"
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
# xAI/Grok OAuth access tokens are intentionally short-lived (about 6h in
# current SuperGrok flows). A two-minute refresh window is too narrow for
# gateway/cron workloads that may only touch the provider every 30 minutes,
# leaving brief but noisy credential-expiry gaps. Refresh up to one hour
# early so ordinary runtime calls keep the token warm without user reauth.
XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 3600
QWEN_OAUTH_CLIENT_ID = "f0304373b74a44d2b584a3fb70ca9e56"
QWEN_OAUTH_TOKEN_URL = "https://chat.qwen.ai/api/v1/oauth2/token"
QWEN_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
DEFAULT_SPOTIFY_ACCOUNTS_BASE_URL = "https://accounts.spotify.com"
DEFAULT_SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1"
DEFAULT_SPOTIFY_REDIRECT_URI = "http://127.0.0.1:43827/spotify/callback"
SPOTIFY_DOCS_URL = "https://son-of-anton.nousresearch.com/docs/user-guide/features/spotify"
SPOTIFY_DASHBOARD_URL = "https://developer.spotify.com/dashboard"
SPOTIFY_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120

OAUTH_OVER_SSH_DOCS_URL = "https://son-of-anton.nousresearch.com/docs/guides/oauth-over-ssh"
DEFAULT_SPOTIFY_SCOPE = " ".join((
    "user-modify-playback-state",
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-library-read",
    "user-library-modify",
))
SERVICE_PROVIDER_NAMES: Dict[str, str] = {
    "spotify": "Spotify",
}

# LM Studio's default no-auth mode still requires *some* non-empty bearer for
# the API-key code paths (auxiliary_client, runtime resolver) to treat the
# provider as configured. This sentinel is sent only to LM Studio, never to
# any remote service.
LMSTUDIO_NOAUTH_PLACEHOLDER = "dummy-lm-api-key"
ACTUAL_LOCAL_NOAUTH_PLACEHOLDER = "dummy-actual-local-api-key"


def is_actual_local_base_url(base_url: str) -> bool:
    """Return True for Actual's loopback local API endpoint."""
    try:
        host = (urlparse(base_url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def normalize_actual_base_url(base_url: str) -> str:
    """Return Actual's OpenAI-compatible base URL.

    Actual hosted inference is exposed at api.actual.inc, while the Actual
    client's offline local server binds a loopback host. Both use a /v1 API
    surface for Son of Anton' Responses transport.
    """
    url = str(base_url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_ACTUAL_BASE_URL
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path.rstrip("/")
    except Exception:
        return url
    if host == "api.actual.inc" and path in {"", "/"}:
        return url + "/v1"
    if is_actual_local_base_url(url) and path in {"", "/"}:
        return url + "/v1"
    return url


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
KIMI_CODE_BASE_URL = "https://api.kimi.com/coding"


def _resolve_kimi_base_url(api_key: str, default_url: str, env_override: str) -> str:
    """Return the correct Kimi base URL based on the API key prefix.

    If the user has explicitly set KIMI_BASE_URL, that always wins.
    Otherwise, sk-kimi- prefixed keys route to api.kimi.com/coding/v1.
    """
    if env_override:
        return env_override
    # No key → nothing to infer from.  Return default without inspecting.
    if not api_key:
        return default_url
    if api_key.startswith("sk-kimi-"):
        return KIMI_CODE_BASE_URL
    return default_url


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
# Z.AI Endpoint Detection
# =============================================================================

# Z.AI has separate billing for general vs coding plans, and global vs China
# endpoints.  A key that works on one may return "Insufficient balance" on
# another.  We probe at setup time and store the working endpoint.
# Each entry lists candidate models to try in order — newer coding plan accounts
# may only have access to recent models (glm-5.1, glm-5v-turbo) while older
# ones still use glm-4.7.

ZAI_ENDPOINTS = [
    # (id, base_url, probe_models, label)
    ("global",        "https://api.z.ai/api/paas/v4",        ["glm-5"],   "Global"),
    ("cn",            "https://open.bigmodel.cn/api/paas/v4", ["glm-5"],   "China"),
    ("coding-global", "https://api.z.ai/api/coding/paas/v4",  ["glm-5.2", "glm-5.1", "glm-5v-turbo", "glm-4.7"], "Global (Coding Plan)"),
    ("coding-cn",     "https://open.bigmodel.cn/api/coding/paas/v4", ["glm-5.2", "glm-5.1", "glm-5v-turbo", "glm-4.7"], "China (Coding Plan)"),
]


def _probe_single_zai_endpoint(
    api_key: str, endpoint: tuple, timeout: float,
) -> Optional[Dict[str, str]]:
    """Probe a single Z.AI endpoint. Returns endpoint info dict or None.

    Preserves the per-endpoint candidate-model loop: endpoints carry a
    ``probe_models`` LIST and each model is tried in order until one
    succeeds (some plans only accept newer/older GLM slugs).
    """
    ep_id, base_url, probe_models, label = endpoint
    for model in probe_models:
        try:
            resp = httpx.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "stream": False,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=timeout,
            )
            if resp.status_code == 200:
                logger.debug("Z.AI endpoint probe: %s (%s) model=%s OK", ep_id, base_url, model)
                return {
                    "id": ep_id,
                    "base_url": base_url,
                    "model": model,
                    "label": label,
                }
            logger.debug("Z.AI endpoint probe: %s model=%s returned %s", ep_id, model, resp.status_code)
        except Exception as exc:
            logger.debug("Z.AI endpoint probe: %s model=%s failed: %s", ep_id, model, exc)
    return None


def detect_zai_endpoint(api_key: str, timeout: float = 8.0) -> Optional[Dict[str, str]]:
    """Probe z.ai endpoints in parallel to find one that accepts this API key.

    Returns {"id": ..., "base_url": ..., "model": ..., "label": ...} for the
    first working endpoint (in ZAI_ENDPOINTS priority order), or None if all
    fail.  For endpoints with multiple candidate models, each worker tries
    its endpoint's models in order and returns the first that succeeds.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # No `with` block: a context manager would join ALL probe threads on
    # exit, defeating the early return below. shutdown(wait=False) lets the
    # surviving daemon-style probes drain in the background instead of
    # blocking the caller on slow/unreachable endpoints.
    pool = ThreadPoolExecutor(max_workers=len(ZAI_ENDPOINTS))
    try:
        futures = {
            pool.submit(_probe_single_zai_endpoint, api_key, ep, timeout): ep[0]
            for ep in ZAI_ENDPOINTS
        }
        by_id = {ep_id: f for f, ep_id in futures.items()}
        results: Dict[str, Dict[str, str]] = {}
        for future in as_completed(futures):
            ep_id = futures[future]
            try:
                result = future.result()
                if result is not None:
                    results[ep_id] = result
            except Exception:
                pass
            # Early exit in PRIORITY order: walk endpoints highest-priority
            # first; if one has succeeded and every higher-priority probe
            # has already finished (without success), no later completion
            # can win — return now instead of waiting out slow endpoints
            # (main's sequential loop also stopped at first success).
            for ep in ZAI_ENDPOINTS:
                if not by_id[ep[0]].done():
                    break  # a higher-priority probe is still in flight
                if ep[0] in results:
                    return results[ep[0]]

        # All probes finished: first match in priority order, if any.
        for ep in ZAI_ENDPOINTS:
            if ep[0] in results:
                return results[ep[0]]
        return None
    finally:
        pool.shutdown(wait=False)


def _resolve_zai_base_url(api_key: str, default_url: str, env_override: str) -> str:
    """Return the correct Z.AI base URL by probing endpoints.

    If the user has explicitly set GLM_BASE_URL, that always wins.
    Otherwise, probe the candidate endpoints to find one that accepts the
    key.  The detected endpoint is cached in provider state (auth.json) keyed
    on a hash of the API key so subsequent starts skip the probe.
    """
    if env_override:
        return env_override

    # No API key set → don't probe (would fire N×M HTTPS requests with an
    # empty Bearer token, all returning 401).  This path is hit during
    # auxiliary-client auto-detection when the user has no Z.AI credentials
    # at all — the caller discards the result immediately, so the probe is
    # pure latency for every AIAgent construction.
    if not api_key:
        return default_url

    # Check provider-state cache for a previously-detected endpoint.
    auth_store = _load_auth_store()
    state = _load_provider_state(auth_store, "zai") or {}
    cached = state.get("detected_endpoint")
    if isinstance(cached, dict) and cached.get("base_url"):
        key_hash = cached.get("key_hash", "")
        if key_hash == hashlib.sha256(api_key.encode()).hexdigest()[:16]:
            logger.debug("Z.AI: using cached endpoint %s", cached["base_url"])
            return cached["base_url"]

    # Probe — may take up to ~8s per endpoint.
    detected = detect_zai_endpoint(api_key)
    if detected and detected.get("base_url"):
        # Persist the detection result keyed on the API key hash.
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        detected_endpoint = {
            "base_url": detected["base_url"],
            "endpoint_id": detected.get("id", ""),
            "model": detected.get("model", ""),
            "label": detected.get("label", ""),
            "key_hash": key_hash,
        }
        # Persist failure (disk full, permissions, lock timeout) must not
        # break resolution — detection already succeeded; worst case the
        # next start re-probes.
        try:
            with _auth_store_lock():
                # Reload auth_store under lock to avoid overwriting concurrent changes
                auth_store = _load_auth_store()
                state_under_lock = _load_provider_state(auth_store, "zai") or {}
                state_under_lock["detected_endpoint"] = detected_endpoint
                # set_active=False: this runs from credential-pool env seeding
                # (agent/credential_pool.py) for ANY user with a Z.AI key in env,
                # and caching a probe result must not flip their active provider.
                _store_provider_state(auth_store, "zai", state_under_lock, set_active=False)
                _save_auth_store(auth_store)
        except Exception as exc:
            logger.warning("Z.AI: could not persist detected endpoint (%s); will re-probe next start", exc)
        logger.info("Z.AI: auto-detected endpoint %s (%s)", detected["label"], detected["base_url"])
        return detected["base_url"]

    logger.debug("Z.AI: probe failed, falling back to default %s", default_url)
    return default_url


def _normalize_lmstudio_runtime_base_url(base_url: str) -> str:
    """Return the OpenAI-compatible LM Studio runtime base URL.

    LM Studio's native management API lives under ``/api/v1`` while its
    OpenAI-compatible chat endpoint lives under ``/v1``. Users often paste
    either form into ``LM_BASE_URL`` or ``model.base_url``; normalize before
    the OpenAI SDK appends ``/chat/completions``.
    """
    root = str(base_url or "").strip().rstrip("/")
    for suffix in ("/api/v1", "/api", "/v1"):
        if root.endswith(suffix):
            root = root[: -len(suffix)].rstrip("/")
            break
    return (root or "http://127.0.0.1:1234") + "/v1"


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


def _load_provider_state_with_source(
    auth_store: Dict[str, Any],
    provider_id: str,
) -> tuple[Optional[Dict[str, Any]], Optional[Path]]:
    """Return a provider state plus the auth.json path it came from.

    Most callers only need the state, but refresh paths that rotate single-use
    OAuth refresh tokens must write the updated token chain back to the same
    store they read. In profile mode ``_load_provider_state`` can read a
    global-root fallback state; persisting a rotated Nous refresh token only to
    the profile would leave the global/root store stale and cause the next
    process to replay an already-consumed refresh token.
    """
    providers = auth_store.get("providers")
    if isinstance(providers, dict):
        state = providers.get(provider_id)
        if isinstance(state, dict):
            return dict(state), _auth_file_path()

    global_path = _global_auth_file_path()
    global_store = _load_global_auth_store()
    if global_store:
        global_providers = global_store.get("providers")
        if isinstance(global_providers, dict):
            global_state = global_providers.get(provider_id)
            if isinstance(global_state, dict):
                return dict(global_state), global_path
    return None, None


def _load_provider_state(auth_store: Dict[str, Any], provider_id: str) -> Optional[Dict[str, Any]]:
    """Return a provider's persisted state.

    In profile mode, falls back to the global-root ``auth.json`` when the
    profile has no entry for ``provider_id``. This mirrors the per-provider
    shadowing already used by ``read_credential_pool``: workers spawned in a
    profile can see providers (e.g. ``nous``) that were only authenticated at
    global scope. Once the user runs ``son-of-anton auth login <provider>`` inside
    the profile, the profile state fully shadows the global state on the next
    read. See issue #18594 follow-up.
    """
    state, _source_path = _load_provider_state_with_source(auth_store, provider_id)
    return state


def _save_provider_state(auth_store: Dict[str, Any], provider_id: str, state: Dict[str, Any]) -> None:
    providers = auth_store.setdefault("providers", {})
    if not isinstance(providers, dict):
        auth_store["providers"] = {}
        providers = auth_store["providers"]
    providers[provider_id] = state
    auth_store["active_provider"] = provider_id


def _store_provider_state(
    auth_store: Dict[str, Any],
    provider_id: str,
    state: Dict[str, Any],
    *,
    set_active: bool = True,
) -> None:
    providers = auth_store.setdefault("providers", {})
    if not isinstance(providers, dict):
        auth_store["providers"] = {}
        providers = auth_store["providers"]
    providers[provider_id] = state
    if set_active:
        auth_store["active_provider"] = provider_id


def _persist_provider_state_to_store(
    provider_id: str,
    state: Dict[str, Any],
    target_path: Path,
    *,
    set_active: bool = False,
) -> Path:
    """Merge one provider into a specific auth store under that store's lock."""
    with _auth_store_lock(target_path=target_path):
        auth_store = _load_auth_store(target_path)
        _store_provider_state(
            auth_store,
            provider_id,
            dict(state),
            set_active=set_active,
        )
        return _save_auth_store(auth_store, target_path=target_path)


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


def get_provider_auth_state(provider_id: str) -> Optional[Dict[str, Any]]:
    """Return persisted auth state for a provider, or None.

    In profile mode, ``_load_provider_state`` already falls back to the
    global-root ``auth.json`` per-provider when the profile has no entry —
    so this is now a thin convenience wrapper. Profile state always wins
    when present. Writes (``_save_auth_store`` / ``persist_*_credentials``)
    are unchanged — they still target the profile only. This mirrors
    ``read_credential_pool``'s per-provider shadowing semantics so that
    ``_seed_from_singletons`` can reseed a profile's credential pool from
    global-scope provider state (e.g. a globally-authenticated Anthropic
    OAuth or Nous device-code session). See issue #18594 follow-up.
    """
    auth_store = _load_auth_store()
    return _load_provider_state(auth_store, provider_id)


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
        "ollama": "custom", "ollama_cloud": "ollama-cloud",
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


def _is_expiring(expires_at_iso: Any, skew_seconds: int) -> bool:
    expires_epoch = _parse_iso_timestamp(expires_at_iso)
    if expires_epoch is None:
        return True
    return expires_epoch <= (time.time() + skew_seconds)


_NOUS_STALE_PORTAL_HOSTS: FrozenSet[str] = frozenset({
    "api.nousresearch.com",
})

# Allowlist of valid Nous Portal hosts. A portal_base_url outside this
# set is treated as a misconfiguration and falls back to the default.
# "localhost" / "127.0.0.1" are valid for local development and testing.
_NOUS_PORTAL_ALLOWED_HOSTS: FrozenSet[str] = frozenset({
    "portal.nousresearch.com",
    "localhost",
    "127.0.0.1",
})


# Allowlist of hosts the Nous Portal proxy is willing to forward inference
# JWTs to. Sending a bearer anywhere else would leak it.
#
# This is consulted only for URLs coming from the NETWORK side (Portal
# refresh responses). User-controlled env-var overrides
# (NOUS_INFERENCE_BASE_URL) bypass validation — that's the documented
# dev/staging escape hatch and the env source is already trusted (the
# user set it themselves).
_ALLOWED_NOUS_INFERENCE_HOSTS: FrozenSet[str] = frozenset({
    "inference-api.nousresearch.com",
})


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


_NOUS_EFFECTIVE_STATE_IGNORED_KEYS = frozenset({
    # These are derived from expires_at/JWT exp and naturally tick down between
    # reads. Persisting only these changes makes auth.json noisy and defeats
    # the mtime-keyed auth-status cache.
    "expires_in",
    "agent_key_expires_in",
})


# =============================================================================
# Spotify auth — PKCE tokens stored in ~/.son-of-anton/auth.json
# =============================================================================


def get_spotify_auth_status() -> Dict[str, Any]:
    state = get_provider_auth_state("spotify")
    if not state:
        return {"logged_in": False}

    expires_at = state.get("expires_at")
    refresh_token = str(state.get("refresh_token", "") or "").strip()
    return {
        "logged_in": bool(refresh_token or not _is_expiring(expires_at, 0)),
        "auth_type": state.get("auth_type", "oauth_pkce"),
        "client_id": state.get("client_id"),
        "redirect_uri": state.get("redirect_uri"),
        "scope": state.get("granted_scope") or state.get("scope"),
        "expires_at": expires_at,
        "api_base_url": state.get("api_base_url"),
        "has_refresh_token": bool(refresh_token),
    }


# =============================================================================
# SSH / remote session detection
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

def _xai_oauth_state_from_store(auth_store: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return usable xAI OAuth state from provider state or credential pool."""
    state = _load_provider_state(auth_store, "xai-oauth")
    tokens = state.get("tokens") if isinstance(state, dict) else None
    if isinstance(tokens, dict):
        access_token = str(tokens.get("access_token", "") or "").strip()
        refresh_token = str(tokens.get("refresh_token", "") or "").strip()
        if access_token and refresh_token:
            return state

    credential_pool = auth_store.get("credential_pool")
    entries = (
        credential_pool.get("xai-oauth")
        if isinstance(credential_pool, dict)
        else None
    )
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            access_token = str(entry.get("access_token", "") or "").strip()
            refresh_token = str(entry.get("refresh_token", "") or "").strip()
            if not access_token or not refresh_token:
                continue
            merged = dict(state or {})
            merged["tokens"] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": str(entry.get("token_type") or "Bearer"),
            }
            if entry.get("last_refresh"):
                merged["last_refresh"] = entry.get("last_refresh")
            merged.setdefault("auth_mode", "oauth_pkce")
            return merged

    return state if isinstance(state, dict) else None


def _xai_oauth_state_has_usable_tokens(state: Optional[Dict[str, Any]]) -> bool:
    tokens = state.get("tokens") if isinstance(state, dict) else None
    return (
        isinstance(tokens, dict)
        and bool(str(tokens.get("access_token", "") or "").strip())
        and bool(str(tokens.get("refresh_token", "") or "").strip())
    )


def _read_xai_oauth_tokens(*, _lock: bool = True) -> Dict[str, Any]:
    if _lock:
        with _auth_store_lock():
            auth_store = _load_auth_store()
    else:
        auth_store = _load_auth_store()
    state = _xai_oauth_state_from_store(auth_store)
    if not _xai_oauth_state_has_usable_tokens(state):
        global_state = _xai_oauth_state_from_store(_load_global_auth_store())
        if _xai_oauth_state_has_usable_tokens(global_state):
            state = global_state
    if not state:
        raise AuthError(
            "No xAI OAuth credentials stored. Select xAI Grok OAuth (SuperGrok / Premium+) in `son-of-anton model`.",
            provider="xai-oauth",
            code="xai_auth_missing",
            relogin_required=True,
        )
    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        raise AuthError(
            "xAI OAuth state is missing tokens. Re-authenticate with `son-of-anton model`.",
            provider="xai-oauth",
            code="xai_auth_invalid_shape",
            relogin_required=True,
        )
    access_token = str(tokens.get("access_token", "") or "").strip()
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    if not access_token:
        raise AuthError(
            "xAI OAuth state is missing access_token. Re-authenticate with `son-of-anton model`.",
            provider="xai-oauth",
            code="xai_auth_missing_access_token",
            relogin_required=True,
        )
    if not refresh_token:
        raise AuthError(
            "xAI OAuth state is missing refresh_token. Re-authenticate with `son-of-anton model`.",
            provider="xai-oauth",
            code="xai_auth_missing_refresh_token",
            relogin_required=True,
        )
    return {
        "tokens": tokens,
        "last_refresh": state.get("last_refresh"),
        "discovery": state.get("discovery") or {},
        "redirect_uri": state.get("redirect_uri"),
    }


def _write_through_xai_oauth_to_global_root(state: Dict[str, Any]) -> None:
    """Persist a rotated xAI OAuth ``state`` into the global-root auth.json.

    Best-effort write-through for the multi-profile rotation hazard (#43589):
    xAI rotates the refresh_token on every refresh, so when a profile session
    refreshes a grant it resolved from the root fallback, the rotated chain
    must land back in root. Otherwise root keeps a now-revoked refresh token
    and every other profile reading the stale root grant dies with
    ``invalid_grant`` once its access token expires.

    Only updates ``providers.xai-oauth`` in the root store; never touches the
    profile store (the caller already saved that). Swallows all errors — a
    failed write-through degrades to the pre-existing behavior (root stale),
    it must never break the profile's own successful save.
    """
    global_path = _global_auth_file_path()
    if global_path is None:
        # Classic mode (profile == root); the profile save already hit root.
        return
    # Seat belt: under pytest, refuse to write the real user's
    # ~/.son-of-anton/auth.json even when SON_OF_ANTON_HOME points at a profile path
    # (mirrors the read-side guard in _load_global_auth_store). Uses the
    # unmodified HOME env, not Path.home() which fixtures may monkeypatch.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        real_home_env = os.environ.get("HOME", "")
        if real_home_env:
            real_root = Path(real_home_env) / ".son-of-anton" / "auth.json"
            try:
                if global_path.resolve(strict=False) == real_root.resolve(strict=False):
                    return
            except Exception:
                return
    try:
        _persist_provider_state_to_store(
            "xai-oauth",
            state,
            global_path,
            set_active=False,
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("xAI OAuth: write-through to global root failed: %s", exc)


def _save_xai_oauth_tokens(
    tokens: Dict[str, Any],
    *,
    discovery: Optional[Dict[str, Any]] = None,
    redirect_uri: str = "",
    last_refresh: Optional[str] = None,
    auth_mode: str = "oauth_device_code",
    set_active: bool = True,
) -> None:
    """Persist xAI OAuth tokens into the auth store.

    When *set_active* is True (default), also promote ``xai-oauth`` to
    ``active_provider`` — appropriate for intentional model/auth login.
    Pass ``set_active=False`` for side-tool credential bootstrap (TTS/setup,
    tools config, dashboard token save, token refresh) so inference routing
    is unchanged.
    """
    if last_refresh is None:
        last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _auth_store_lock():
        auth_store = _load_auth_store()
        # A profile that lacks its own xai-oauth block is reading the root
        # grant through _load_provider_state's fallback. When such a profile
        # refreshes the (rotating) grant, we must write the rotated chain back
        # to root too, or root is left holding a revoked refresh token (#43589).
        # #74339: the old key-presence check (_profile_has_own_xai_oauth_state)
        # decided write-through based on whether the profile had a
        # providers.xai-oauth key BEFORE the save — but _store_provider_state
        # unconditionally creates that key below. Use
        # _load_provider_state_with_source to learn where the grant was
        # resolved from and write back only to that source.
        state, source_path = _load_provider_state_with_source(
            auth_store, "xai-oauth"
        )
        if state is None:
            state = {}
        state["tokens"] = tokens
        state["last_refresh"] = last_refresh
        state["auth_mode"] = auth_mode
        if discovery:
            state["discovery"] = discovery
        if redirect_uri:
            state["redirect_uri"] = redirect_uri
        global_root = _global_auth_file_path()
        is_from_root = bool(
            source_path is not None
            and global_root is not None
            and _same_path(source_path, global_root)
        )
        if is_from_root:
            # Grant was resolved from root — write back to root only.
            # Do NOT call _store_provider_state on the profile auth_store
            # (it would create a shadowing providers.xai-oauth key that
            # disables write-through on the next refresh — #74339).
            _write_through_xai_oauth_to_global_root(state)
        else:
            # Profile genuinely owns this — write to profile store.
            _store_provider_state(
                auth_store, "xai-oauth", state, set_active=set_active
            )
            _save_auth_store(auth_store)


def _xai_access_token_is_expiring(access_token: str, skew_seconds: int = 0) -> bool:
    if not isinstance(access_token, str) or "." not in access_token:
        return False
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return False
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8"))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return float(exp) <= (time.time() + max(0, int(skew_seconds)))
    except Exception:
        return False


def _xai_proactive_refresh_skew_seconds(access_token: str) -> int:
    """How far before JWT ``exp`` to proactively refresh xAI OAuth tokens.

    SuperGrok sessions can still ship multi-hour access tokens, where the
    gateway-oriented :data:`XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS` window
    makes sense. Device-code logins often return ~15-minute JWTs; applying
    the full hour-long skew to those forces a refresh on *every* credential
    resolution (chat turn, Imagine tool call, ``son-of-anton auth status``, …),
    which burns single-use refresh tokens and races concurrent callers into
    ``invalid_grant`` quarantine.
    """
    max_skew = XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS
    if not isinstance(access_token, str) or "." not in access_token:
        return max_skew
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return max_skew
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8"))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return max_skew
        remaining = float(exp) - time.time()
        if remaining <= 0:
            return max_skew
        if remaining <= 45 * 60:
            return min(120, max_skew)
        return max_skew
    except Exception:
        return max_skew


def _xai_validate_oauth_endpoint(url: str, *, field: str) -> str:
    """Refuse any OIDC discovery endpoint that isn't HTTPS on the xAI origin.

    The OIDC discovery response is a long-lived, low-frequency request whose
    output is cached in ``~/.son-of-anton/auth.json``. A single MITM during initial
    login could substitute a malicious ``token_endpoint``; that URL would
    then receive the refresh_token on every subsequent refresh — a permanent
    credential leak from a one-time MITM. Validating scheme + host pins the
    cached endpoint to the xAI auth origin (or a future ``*.x.ai`` subdomain
    if xAI migrates) so the cache poisoning loses its persistence guarantee.

    RFC 8414 §2 requires the issuer to be ``https://`` and SHOULD-keeps the
    token_endpoint on the same origin; we enforce both. ``x.ai`` is the
    bare apex, so we accept either exact host match or any ``.x.ai`` suffix.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise AuthError(
            f"xAI OIDC discovery returned a non-HTTPS {field}: {url!r}.",
            provider="xai-oauth",
            code="xai_discovery_invalid",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise AuthError(
            f"xAI OIDC discovery {field} is missing a hostname: {url!r}.",
            provider="xai-oauth",
            code="xai_discovery_invalid",
        )
    if host != "x.ai" and not host.endswith(".x.ai"):
        raise AuthError(
            f"xAI OIDC discovery {field} host {host!r} is not on the xAI origin "
            f"(expected x.ai or a *.x.ai subdomain). Refusing to use a cached "
            f"endpoint that may have been substituted by a MITM during initial "
            f"discovery; re-authenticate with `son-of-anton model` to re-fetch.",
            provider="xai-oauth",
            code="xai_discovery_invalid",
        )
    return url


def _xai_validate_inference_base_url(value: str, *, fallback: str) -> str:
    """Refuse a non-xAI base_url for the OAuth-authenticated inference path.

    The xAI Grok OAuth bearer is a high-value, long-lived credential tied to
    the user's SuperGrok subscription. ``XAI_BASE_URL`` / ``SON_OF_ANTON_XAI_BASE_URL``
    let users repoint the inference endpoint (handy for staging or a local
    proxy), but the env override is also a credential-leak vector: a tampered
    ``.env`` or hostile shell init that sets
    ``XAI_BASE_URL=https://attacker.example/v1`` would ship the OAuth access
    token to a third party on every request, silently.

    Pin the inference origin to ``api.x.ai`` (or any ``*.x.ai`` subdomain xAI
    may add). On rejection, fall back to the default and log a warning rather
    than raise — a bad env var should not deadlock authentication, but it
    should also never leak the bearer.

    ``value`` is the already-stripped, trailing-slash-trimmed candidate from
    env. Empty input returns ``fallback`` unchanged.
    """
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return fallback
    try:
        parsed = urlparse(candidate)
    except Exception:
        logger.warning(
            "Ignoring malformed xAI base_url override %r; using %s instead.",
            candidate, fallback,
        )
        return fallback
    if parsed.scheme != "https":
        logger.warning(
            "Refusing non-HTTPS xAI base_url override %r (xai-oauth bearer would "
            "be sent in cleartext); falling back to %s.",
            candidate, fallback,
        )
        return fallback
    host = (parsed.hostname or "").lower()
    if not host:
        logger.warning(
            "Ignoring xAI base_url override %r with no hostname; using %s instead.",
            candidate, fallback,
        )
        return fallback
    if host != "x.ai" and not host.endswith(".x.ai"):
        logger.warning(
            "Refusing xAI base_url override %r — host %r is not on the xAI origin "
            "(expected x.ai or a *.x.ai subdomain). The xai-oauth bearer is only "
            "valid against xAI's inference API; sending it elsewhere would leak "
            "the credential. Falling back to %s.",
            candidate, host, fallback,
        )
        return fallback
    return candidate


def _xai_oauth_discovery(timeout_seconds: float = 15.0) -> Dict[str, str]:
    try:
        response = httpx.get(
            XAI_OAUTH_DISCOVERY_URL,
            headers={"Accept": "application/json"},
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise AuthError(
            f"xAI OIDC discovery failed: {exc}",
            provider="xai-oauth",
            code="xai_discovery_failed",
        ) from exc
    if response.status_code != 200:
        raise AuthError(
            f"xAI OIDC discovery returned status {response.status_code}.",
            provider="xai-oauth",
            code="xai_discovery_failed",
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            f"xAI OIDC discovery returned invalid JSON: {exc}",
            provider="xai-oauth",
            code="xai_discovery_invalid_json",
        ) from exc
    if not isinstance(payload, dict):
        raise AuthError(
            "xAI OIDC discovery response was not a JSON object.",
            provider="xai-oauth",
            code="xai_discovery_incomplete",
        )
    authorization_endpoint = str(payload.get("authorization_endpoint", "") or "").strip()
    token_endpoint = str(payload.get("token_endpoint", "") or "").strip()
    if not authorization_endpoint or not token_endpoint:
        raise AuthError(
            "xAI OIDC discovery response was missing required endpoints.",
            provider="xai-oauth",
            code="xai_discovery_incomplete",
        )
    _xai_validate_oauth_endpoint(authorization_endpoint, field="authorization_endpoint")
    _xai_validate_oauth_endpoint(token_endpoint, field="token_endpoint")
    return {
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
    }


def refresh_xai_oauth_pure(
    access_token: str,
    refresh_token: str,
    *,
    token_endpoint: str = "",
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    del access_token
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise AuthError(
            "xAI OAuth is missing refresh_token. Re-authenticate with `son-of-anton model`.",
            provider="xai-oauth",
            code="xai_auth_missing_refresh_token",
            relogin_required=True,
        )
    endpoint = token_endpoint.strip() or _xai_oauth_discovery(timeout_seconds)["token_endpoint"]
    # Re-validate cached endpoints on the refresh hot path: an auth.json
    # written by an older Son of Anton (or hand-edited) may carry a non-xAI
    # token_endpoint that would receive every future refresh_token in
    # plaintext if we trusted it blindly. Cheap suffix check; fast-fail
    # with a clear error so the user can re-run `son-of-anton model` to refetch.
    _xai_validate_oauth_endpoint(endpoint, field="token_endpoint")
    timeout = httpx.Timeout(max(5.0, float(timeout_seconds)))
    with httpx.Client(timeout=timeout, headers={"Accept": "application/json"}) as client:
        response = client.post(
            endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": XAI_OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )
    if response.status_code != 200:
        detail = response.text.strip()
        # ``403`` from xAI's token endpoint is almost always a tier /
        # entitlement gate (the OAuth grant exists but the account isn't
        # on the allowlist for API access).  Re-running ``son-of-anton model``
        # won't fix that — surface a separate error code so
        # ``format_auth_error`` doesn't append a misleading
        # re-authenticate hint, and point users at the ``XAI_API_KEY``
        # fallback.  See #26847.
        if response.status_code == 403:
            raise AuthError(
                "xAI token refresh failed with HTTP 403."
                + (f" Response: {detail}" if detail else "")
                + " This OAuth account is not authorized for xAI API"
                  " access — xAI may be restricting API/OAuth use to"
                  " specific SuperGrok tiers despite the in-app"
                  " subscription being active. Re-logging in won't"
                  " change that; set ``XAI_API_KEY`` and switch to"
                  " ``provider: xai`` (API-key path) if available, or"
                  " upgrade your subscription at https://x.ai/grok.",
                provider="xai-oauth",
                code="xai_oauth_tier_denied",
                relogin_required=False,
            )
        raise AuthError(
            "xAI token refresh failed."
            + (f" Response: {detail}" if detail else ""),
            provider="xai-oauth",
            code="xai_refresh_failed",
            relogin_required=(response.status_code in {400, 401}),
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            f"xAI token refresh returned invalid JSON: {exc}",
            provider="xai-oauth",
            code="xai_refresh_invalid_json",
        ) from exc
    if not isinstance(payload, dict):
        raise AuthError(
            "xAI token refresh response was not a JSON object.",
            provider="xai-oauth",
            code="xai_refresh_invalid_response",
            relogin_required=True,
        )
    refreshed_access = str(payload.get("access_token", "") or "").strip()
    if not refreshed_access:
        raise AuthError(
            "xAI token refresh response was missing access_token.",
            provider="xai-oauth",
            code="xai_refresh_missing_access_token",
            relogin_required=True,
        )
    updated = {
        "access_token": refreshed_access,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "id_token": str(payload.get("id_token") or "").strip(),
        "expires_in": payload.get("expires_in"),
        "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return updated


def _refresh_xai_oauth_tokens(
    tokens: Dict[str, Any],
    *,
    token_endpoint: str,
    redirect_uri: str = "",
    timeout_seconds: float,
) -> Dict[str, Any]:
    # Re-persist whatever auth_mode is already stored (legacy pre-device-code
    # logins may still carry ``oauth_pkce``): the refresh hot path must not
    # relabel how the grant was originally obtained.
    try:
        state = _load_provider_state(_load_auth_store(), "xai-oauth") or {}
        auth_mode = str(state.get("auth_mode") or "oauth_device_code")
    except Exception:
        auth_mode = "oauth_device_code"
    refreshed = refresh_xai_oauth_pure(
        str(tokens.get("access_token", "") or ""),
        str(tokens.get("refresh_token", "") or ""),
        token_endpoint=token_endpoint,
        timeout_seconds=timeout_seconds,
    )
    updated_tokens = dict(tokens)
    updated_tokens["access_token"] = refreshed["access_token"]
    updated_tokens["refresh_token"] = refreshed["refresh_token"]
    if refreshed.get("id_token"):
        updated_tokens["id_token"] = refreshed["id_token"]
    if refreshed.get("expires_in") is not None:
        updated_tokens["expires_in"] = refreshed["expires_in"]
    if refreshed.get("token_type"):
        updated_tokens["token_type"] = refreshed["token_type"]
    _save_xai_oauth_tokens(
        updated_tokens,
        discovery={"token_endpoint": token_endpoint},
        redirect_uri=redirect_uri,
        last_refresh=refreshed["last_refresh"],
        auth_mode=auth_mode,
        # Refresh must not flip active_provider — TTS/side tools can refresh
        # xAI tokens while chat still routes through another provider.
        set_active=False,
    )
    return updated_tokens


def resolve_xai_oauth_runtime_credentials(
    *,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
    refresh_skew_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    data = _read_xai_oauth_tokens()
    tokens = dict(data["tokens"])
    access_token = str(tokens.get("access_token", "") or "").strip()
    refresh_timeout_seconds = env_float("SON_OF_ANTON_XAI_REFRESH_TIMEOUT_SECONDS", 20)
    discovery = dict(data.get("discovery") or {})
    token_endpoint = str(discovery.get("token_endpoint", "") or "").strip()
    redirect_uri = str(data.get("redirect_uri", "") or "").strip()

    effective_skew = (
        int(refresh_skew_seconds)
        if refresh_skew_seconds is not None
        else _xai_proactive_refresh_skew_seconds(access_token)
    )
    should_refresh = bool(force_refresh)
    if (not should_refresh) and refresh_if_expiring:
        should_refresh = _xai_access_token_is_expiring(access_token, effective_skew)
    if should_refresh:
        with _auth_store_lock(timeout_seconds=max(float(AUTH_LOCK_TIMEOUT_SECONDS), refresh_timeout_seconds + 5.0)):
            data = _read_xai_oauth_tokens(_lock=False)
            tokens = dict(data["tokens"])
            access_token = str(tokens.get("access_token", "") or "").strip()
            discovery = dict(data.get("discovery") or {})
            token_endpoint = str(discovery.get("token_endpoint", "") or "").strip()
            redirect_uri = str(data.get("redirect_uri", "") or "").strip()
            effective_skew = (
                int(refresh_skew_seconds)
                if refresh_skew_seconds is not None
                else _xai_proactive_refresh_skew_seconds(access_token)
            )
            should_refresh = bool(force_refresh)
            if (not should_refresh) and refresh_if_expiring:
                should_refresh = _xai_access_token_is_expiring(access_token, effective_skew)
            if should_refresh:
                if not token_endpoint:
                    token_endpoint = _xai_oauth_discovery(refresh_timeout_seconds)["token_endpoint"]
                try:
                    tokens = _refresh_xai_oauth_tokens(
                        tokens,
                        token_endpoint=token_endpoint,
                        redirect_uri=redirect_uri,
                        timeout_seconds=refresh_timeout_seconds,
                    )
                    access_token = str(tokens.get("access_token", "") or "").strip()
                except AuthError as exc:
                    if _is_terminal_xai_oauth_refresh_error(exc):
                        # Terminal failure (HTTP 400/401/403 — invalid_grant, token revoked).
                        # Clear dead tokens from auth.json so subsequent sessions fail fast
                        # without a network retry. Mirrors credential_pool.py quarantine.
                        try:
                            _q_store = _load_auth_store()
                            _q_state = _load_provider_state(_q_store, "xai-oauth") or {}
                            _q_tokens = dict(_q_state.get("tokens") or {})
                            _q_tokens.pop("access_token", None)
                            _q_tokens.pop("refresh_token", None)
                            _q_state["tokens"] = _q_tokens
                            _q_state["last_auth_error"] = {
                                "provider": "xai-oauth",
                                "code": exc.code or "xai_refresh_failed",
                                "message": str(exc),
                                "reason": "runtime_refresh_failure",
                                "relogin_required": True,
                                "at": datetime.now(timezone.utc).isoformat(),
                            }
                            _store_provider_state(_q_store, "xai-oauth", _q_state, set_active=False)
                            _save_auth_store(_q_store)
                        except Exception as _save_exc:
                            logger.debug(
                                "xAI OAuth: failed to persist quarantined state: %s", _save_exc,
                            )
                    raise

    base_url = _xai_validate_inference_base_url(
        os.getenv("SON_OF_ANTON_XAI_BASE_URL", "").strip().rstrip("/")
        or os.getenv("XAI_BASE_URL", "").strip().rstrip("/"),
        fallback=DEFAULT_XAI_OAUTH_BASE_URL,
    )
    return {
        "provider": "xai-oauth",
        "base_url": base_url,
        "api_key": access_token,
        "source": "son-of-anton-auth-store",
        "last_refresh": data.get("last_refresh"),
        # Display/telemetry only. Device-code is the only supported xAI OAuth
        # flow, so report it unconditionally — auth.json may still carry a
        # legacy ``oauth_pkce`` label, which the refresh path preserves as-is.
        "auth_mode": "oauth_device_code",
    }


# =============================================================================
# TLS verification helper
# =============================================================================


def _is_terminal_xai_oauth_refresh_error(exc: Exception) -> bool:
    """True when retrying the same xAI OAuth refresh token cannot succeed.

    ``xai_refresh_failed`` covers HTTP 400/401/403 from the token endpoint
    (invalid_grant, token revoked, refresh_token_reused).
    ``xai_auth_missing_refresh_token`` means the pool entry has no refresh
    token at all — retrying will never work.
    Both carry ``relogin_required=True``; transient failures (429, 5xx) do not.
    """
    return (
        isinstance(exc, AuthError)
        and exc.provider == "xai-oauth"
        and exc.code in {"xai_refresh_failed", "xai_auth_missing_refresh_token"}
        and bool(exc.relogin_required)
    )


# =============================================================================
# Status helpers
# =============================================================================

# mtime-keyed memo for _load_global_auth_store(): (path, mtime_ns, store).
# Same invalidation contract as _nous_auth_status_cache — the global auth
# file changes only when a global-scope auth write touches it.
_global_auth_store_cache: Optional[Tuple[str, int, Dict[str, Any]]] = None


def get_xai_oauth_auth_status() -> Dict[str, Any]:
    try:
        from agent.credential_pool import load_pool

        pool = load_pool("xai-oauth")
        if pool and pool.has_credentials():
            entry = pool.select()
            if entry is not None:
                api_key = (
                    getattr(entry, "runtime_api_key", None)
                    or getattr(entry, "access_token", "")
                )
                if api_key and not _xai_access_token_is_expiring(api_key, 0):
                    return {
                        "logged_in": True,
                        "auth_store": str(_auth_file_path()),
                        "last_refresh": getattr(entry, "last_refresh", None),
                        # Display/telemetry only. Device-code is the only xAI
                        # OAuth flow, so report it unconditionally (auth.json
                        # may still carry a legacy ``oauth_pkce`` label).
                        "auth_mode": "oauth_device_code",
                        "source": f"pool:{getattr(entry, 'label', 'unknown')}",
                        "api_key": api_key,
                    }
    except Exception:
        pass

    try:
        creds = resolve_xai_oauth_runtime_credentials()
        return {
            "logged_in": True,
            "auth_store": str(_auth_file_path()),
            "last_refresh": creds.get("last_refresh"),
            "auth_mode": creds.get("auth_mode"),
            "source": creds.get("source"),
            "api_key": creds.get("api_key"),
        }
    except AuthError as exc:
        return {
            "logged_in": False,
            "auth_store": str(_auth_file_path()),
            "error": str(exc),
        }


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

    api_key = ""
    key_source = ""
    api_key, key_source = _resolve_api_key_provider_secret(provider_id, pconfig)

    env_url = ""
    if pconfig.base_url_env_var:
        env_url = os.getenv(pconfig.base_url_env_var, "").strip()

    if provider_id in {"kimi-coding", "kimi-coding-cn"}:
        base_url = _resolve_kimi_base_url(api_key, pconfig.inference_base_url, env_url)
    elif env_url:
        base_url = env_url
    else:
        base_url = pconfig.inference_base_url

    if provider_id == "actual":
        base_url = normalize_actual_base_url(base_url)

    actual_local_noauth = (
        provider_id == "actual"
        and not api_key
        and is_actual_local_base_url(base_url)
    )

    return {
        "configured": bool(api_key) or actual_local_noauth,
        "provider": provider_id,
        "name": pconfig.name,
        "key_source": key_source or ("local-offline" if actual_local_noauth else ""),
        "base_url": base_url,
        "logged_in": bool(api_key) or actual_local_noauth,  # compat with OAuth status shape
    }


def get_external_process_provider_status(provider_id: str) -> Dict[str, Any]:
    """Status snapshot for providers that run a local subprocess."""
    pconfig = PROVIDER_REGISTRY.get(provider_id)
    if not pconfig or pconfig.auth_type != "external_process":
        return {"configured": False}

    command = (
        os.getenv("SON_OF_ANTON_COPILOT_ACP_COMMAND", "").strip()
        or os.getenv("COPILOT_CLI_PATH", "").strip()
        or "copilot"
    )
    raw_args = os.getenv("SON_OF_ANTON_COPILOT_ACP_ARGS", "").strip()
    args = shlex.split(raw_args) if raw_args else ["--acp", "--stdio"]
    base_url = os.getenv(pconfig.base_url_env_var, "").strip() if pconfig.base_url_env_var else ""
    if not base_url:
        base_url = pconfig.inference_base_url

    resolved_command = shutil.which(command) if command else None
    return {
        "configured": bool(resolved_command or base_url.startswith("acp+tcp://")),
        "provider": provider_id,
        "name": pconfig.name,
        "command": command,
        "args": args,
        "resolved_command": resolved_command,
        "base_url": base_url,
        "logged_in": bool(resolved_command or base_url.startswith("acp+tcp://")),
    }


def get_auth_status(provider_id: Optional[str] = None) -> Dict[str, Any]:
    """Generic auth status dispatcher."""
    target = (provider_id or get_active_provider() or "").strip().lower()
    if not target:
        return {"logged_in": False}
    if target == "spotify":
        return get_spotify_auth_status()
    if target == "xai-oauth":
        return get_xai_oauth_auth_status()
    if target == "minimax-oauth":
        return get_minimax_oauth_auth_status()
    if target == "copilot-acp":
        return get_external_process_provider_status(target)
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

    api_key = ""
    key_source = ""
    api_key, key_source = _resolve_api_key_provider_secret(provider_id, pconfig)

    # No-auth LM Studio: substitute a placeholder so runtime / auxiliary_client
    # see the local server as configured. doctor still reports unconfigured
    # because get_api_key_provider_status uses the raw secret resolver.
    if not api_key and provider_id == "lmstudio":
        api_key = LMSTUDIO_NOAUTH_PLACEHOLDER
        key_source = key_source or "default"

    env_url = ""
    if pconfig.base_url_env_var:
        env_url = os.getenv(pconfig.base_url_env_var, "").strip()

    if provider_id in {"kimi-coding", "kimi-coding-cn"}:
        base_url = _resolve_kimi_base_url(api_key, pconfig.inference_base_url, env_url)
    elif provider_id == "zai":
        base_url = _resolve_zai_base_url(api_key, pconfig.inference_base_url, env_url)
    elif env_url:
        base_url = env_url.rstrip("/")
    else:
        base_url = pconfig.inference_base_url

    if provider_id == "lmstudio":
        base_url = _normalize_lmstudio_runtime_base_url(base_url)

    if provider_id == "actual":
        base_url = normalize_actual_base_url(base_url)

    # Last-resort guard: an API-key provider must never hand back an empty
    # base URL (a set-but-empty COPILOT_API_BASE_URL or similar env override
    # otherwise wedges chat inference — #50252).
    if not (isinstance(base_url, str) and base_url.strip()):
        base_url = pconfig.inference_base_url

    if not api_key and provider_id == "actual" and is_actual_local_base_url(base_url):
        api_key = ACTUAL_LOCAL_NOAUTH_PLACEHOLDER
        key_source = key_source or "local-offline"

    return {
        "provider": provider_id,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "source": key_source or "default",
    }


def resolve_external_process_provider_credentials(provider_id: str) -> Dict[str, Any]:
    """Resolve runtime details for local subprocess-backed providers."""
    pconfig = PROVIDER_REGISTRY.get(provider_id)
    if not pconfig or pconfig.auth_type != "external_process":
        raise AuthError(
            f"Provider '{provider_id}' is not an external-process provider.",
            provider=provider_id,
            code="invalid_provider",
        )

    base_url = os.getenv(pconfig.base_url_env_var, "").strip() if pconfig.base_url_env_var else ""
    if not base_url:
        base_url = pconfig.inference_base_url

    command = (
        os.getenv("SON_OF_ANTON_COPILOT_ACP_COMMAND", "").strip()
        or os.getenv("COPILOT_CLI_PATH", "").strip()
        or "copilot"
    )
    raw_args = os.getenv("SON_OF_ANTON_COPILOT_ACP_ARGS", "").strip()
    args = shlex.split(raw_args) if raw_args else ["--acp", "--stdio"]
    resolved_command = shutil.which(command) if command else None
    if not resolved_command and not base_url.startswith("acp+tcp://"):
        raise AuthError(
            f"Could not find the Copilot CLI command '{command}'. "
            "Install GitHub Copilot CLI or set SON_OF_ANTON_COPILOT_ACP_COMMAND/COPILOT_CLI_PATH.",
            provider=provider_id,
            code="missing_copilot_cli",
        )

    return {
        "provider": provider_id,
        "api_key": "copilot-acp",
        "base_url": base_url.rstrip("/"),
        "command": resolved_command or command,
        "args": args,
        "source": "process",
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

        _upgrade_url = (portal_url or DEFAULT_NOUS_PORTAL_URL).rstrip("/")
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
        _upgrade_url = (portal_url or DEFAULT_NOUS_PORTAL_URL).rstrip("/")
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


# ==================== MiniMax Portal OAuth ====================

_MINIMAX_OAUTH_ERROR_BODY_LIMIT = 16 * 1024


def get_minimax_oauth_auth_status() -> Dict[str, Any]:
    """Return auth status dict for MiniMax OAuth provider."""
    state = get_provider_auth_state("minimax-oauth")
    if not state or not state.get("access_token"):
        return {"logged_in": False, "provider": "minimax-oauth"}
    try:
        expires_at = datetime.fromisoformat(state.get("expires_at", "")).timestamp()
        token_valid = (expires_at - time.time()) > 0
    except Exception:
        token_valid = bool(state.get("access_token"))
    return {
        "logged_in": token_valid,
        "provider": "minimax-oauth",
        "region": state.get("region", "global"),
        "expires_at": state.get("expires_at"),
    }


