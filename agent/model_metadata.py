"""Model metadata, context lengths, and token estimation utilities.

Pure utility functions with no AIAgent dependency. Used by ContextCompactor
and run_agent.py for pre-flight context checks.

Context windows come from config (``model.context_length``, a
``custom_providers`` per-model ``context_length``), then the endpoint's own
OpenAI-compatible ``/models`` listing (maitred lists ``context_length`` per
model), then a local server's ``/v1/models/<id>``.
"""

import ipaddress
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:  # pragma: no cover — runtime import is lazy (see below)
    import requests

from utils import atomic_toml_write, fast_toml_load

from agent.message_metadata import PERSISTENCE_ONLY_MESSAGE_FIELDS

logger = logging.getLogger(__name__)

# ``requests`` (with urllib3) costs ~27 ms of the `import cli` waterfall and
# is only used inside the fetch functions below. It's resolved lazily:
# ``_ensure_requests()`` populates the module global on the runtime path, and
# the PEP 562 ``__getattr__`` covers external attribute access — notably
# ``patch("agent.model_metadata.requests.get")`` in tests, which resolves the
# attribute at patch time.


def _ensure_requests():
    if "requests" not in globals():
        import requests as _requests
        globals()["requests"] = _requests
    return globals()["requests"]


def __getattr__(name: str):
    if name == "requests":
        return _ensure_requests()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _resolve_requests_verify(base_url: str = "") -> bool | str:
    """Resolve SSL verify setting for `requests` calls.

    Priority (mirrors ``agent.ssl_verify.resolve_httpx_verify`` so the
    ``requests``-based ``/models`` probes agree with the httpx chat client):

    1. Per-provider ``ssl_verify: false`` for ``base_url`` — disable verification.
    2. Per-provider ``ssl_ca_cert`` for ``base_url`` — an explicit CA bundle.
       Without this, a custom endpoint whose chain only verifies against the
       provider's configured bundle (not the process ``SSL_CERT_FILE``) logs a
       spurious CERTIFICATE_VERIFY_FAILED on every probe even though the chat
       path succeeds (per-provider ``ssl_ca_cert`` was reaching only httpx).
    3. Env vars ``SON_OF_ANTON_CA_BUNDLE`` / ``REQUESTS_CA_BUNDLE`` / ``SSL_CERT_FILE``
       (a single var covers both ``requests`` and ``httpx`` in-process).
    4. ``True`` — defer to the requests default (certifi).

    ``base_url`` is optional so existing callers (OpenRouter, etc.) keep the
    env-only behavior unchanged; only probes that pass a base_url pick up the
    per-provider override.
    """
    if base_url:
        try:
            from son_of_anton_cli.config import get_custom_provider_tls_settings
            tls = get_custom_provider_tls_settings(base_url)
            if tls.get("ssl_verify") is False:
                return False
            ca = tls.get("ssl_ca_cert")
            if isinstance(ca, str) and ca and os.path.isfile(ca):
                return ca
        except Exception:
            pass  # fall through to env vars — never break a probe on config lookup
    for env_var in ("SON_OF_ANTON_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        val = os.getenv(env_var)
        if val and os.path.isfile(val):
            return val
    return True

_OLLAMA_TAG_PATTERN = re.compile(
    r"^(\d+\.?\d*b|latest|stable|q\d|fp?\d|instruct|chat|coder|vision|text)",
    re.IGNORECASE,
)


# Tailscale's CGNAT range (RFC 6598). `ipaddress.is_private` excludes this
# block, so without an explicit check Ollama reached over Tailscale (e.g.
# `http://100.77.243.5:11434`) wouldn't be treated as local and its stream
# read / stale timeouts wouldn't get auto-bumped. Built once at import time.
_TAILSCALE_CGNAT = ipaddress.IPv4Network("100.64.0.0/10")


def _strip_provider_prefix(model: str) -> str:
    """Strip a recognised provider prefix from a model string.

    Provider names and aliases come from the provider-profile registry, so
    bundled and user plugins are recognised without a core catalog update.

    ``"local:my-model"`` → ``"my-model"``
    ``"qwen3.5:27b"``   → ``"qwen3.5:27b"``  (unchanged — not a provider prefix)
    ``"qwen:0.5b"``     → ``"qwen:0.5b"``    (unchanged — Ollama model:tag)
    ``"deepseek:latest"``→ ``"deepseek:latest"``(unchanged — Ollama model:tag)
    """
    if ":" not in model or model.startswith("http"):
        return model
    prefix, suffix = model.split(":", 1)
    from son_of_anton_constants import _CUSTOM_FAMILY_PROVIDERS

    if prefix.strip().lower() in _CUSTOM_FAMILY_PROVIDERS:
        # Don't strip if suffix looks like an Ollama tag (e.g. "7b", "latest", "q4_0")
        if _OLLAMA_TAG_PATTERN.match(suffix.strip()):
            return model
        return suffix
    return model


_endpoint_model_metadata_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
_endpoint_model_metadata_cache_time: Dict[str, float] = {}
_ENDPOINT_MODEL_CACHE_TTL = 300

# A configured endpoint that is routable-but-dead — e.g. a corp LAN address
# while off-VPN — blackholes TCP: the SYN draws no SYN-ACK, no RST and no ICMP
# error, so a probe waits out its full timeout instead of failing fast. Startup
# runs several such probes (the /models listing, the local server query), and
# the stalls stack into a long hang before the banner renders.
#
# Once ANY probe has actually observed a connect timeout for an endpoint, the
# others have nothing to gain by repeating it. Recording that observation and
# short-circuiting on it performs no network I/O of its own — it adds no probe
# for callers or tests to mock, and it can only ever fire after a real timeout
# has already been paid, so it cannot suppress a probe that would have worked.
_ENDPOINT_BLACKHOLE_TTL_SECONDS = 30.0
# Values are monotonic timestamps of the last observed connect timeout.
_endpoint_blackhole_cache: Dict[str, float] = {}


def _endpoint_host_key(base_url: str) -> Optional[str]:
    """Return a ``host:port`` key for ``base_url``, or None if it has no host.

    Keyed on host:port rather than the full URL so every probe path for one
    server — ``/v1``-suffixed or not, LM Studio root or API root — shares a
    single entry.
    """
    normalized = _normalize_base_url(base_url)
    if not normalized:
        return None
    url = normalized if "://" in normalized else f"http://{normalized}"
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except Exception:
        return None
    return f"{host}:{port}" if host else None


def _note_endpoint_blackholed(base_url: str) -> None:
    """Record that a probe to ``base_url`` timed out during TCP connect."""
    key = _endpoint_host_key(base_url)
    if key is None:
        return
    _endpoint_blackhole_cache[key] = time.monotonic()
    logger.debug(
        "Endpoint %s timed out connecting — skipping further probes for %.0fs",
        key, _ENDPOINT_BLACKHOLE_TTL_SECONDS,
    )


def _endpoint_blackholed(base_url: str) -> bool:
    """True if a recent probe to ``base_url`` timed out during TCP connect.

    Pure cache lookup; never touches the network. The entry expires after
    _ENDPOINT_BLACKHOLE_TTL_SECONDS — long enough to collapse one startup's
    burst of probes, short enough that bringing the VPN up mid-session is
    picked up without a restart.
    """
    if _ENDPOINT_BLACKHOLE_TTL_SECONDS <= 0:
        return False
    key = _endpoint_host_key(base_url)
    if key is None:
        return False
    seen = _endpoint_blackhole_cache.get(key)
    if seen is None:
        return False
    if (time.monotonic() - seen) >= _ENDPOINT_BLACKHOLE_TTL_SECONDS:
        del _endpoint_blackhole_cache[key]
        return False
    return True


def _is_connect_timeout(exc: BaseException) -> bool:
    """True for connect-phase timeouts raised by httpx or requests.

    Read timeouts are deliberately excluded: those mean the server accepted
    the connection, which is the opposite of the blackhole this guards.
    """
    try:
        import httpx
        if isinstance(exc, httpx.ConnectTimeout):
            return True
    except Exception:
        pass
    try:
        from requests.exceptions import ConnectTimeout
        if isinstance(exc, ConnectTimeout):
            return True
    except Exception:
        pass
    return False


# Default context length when no detection method succeeds.
DEFAULT_FALLBACK_CONTEXT = 256_000

# (model, base_url) pairs that already emitted the fallback warning.
# The fallback result itself is deliberately never cached, so without this
# the warning would repeat on every resolution for the same unknown model.
_FALLBACK_WARNED: set = set()


def _warn_context_length_fallback(model: str, base_url: str) -> None:
    """Warn (once per model+endpoint) that context detection failed and the
    hard default is being used, so small-context models (8K, 32K) don't
    silently get 256K and cause hard-to-debug API failures."""
    key = (model, base_url or "")
    if key in _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED.add(key)
    logger.warning(
        "Could not determine context length for model %r (base_url=%s) "
        "— falling back to %s tokens. Set model.context_length in "
        "config.toml to override.",
        model, base_url or "default", f"{DEFAULT_FALLBACK_CONTEXT:,}",
    )

# Minimum context length required to run Son of Anton Agent.  Models with fewer
# tokens cannot maintain enough working memory for tool-calling workflows.
# Sessions, model switches, and cron jobs should reject models below this.
MINIMUM_CONTEXT_LENGTH = 64_000

# Short-lived in-process cache for local-server context probes. Bounds the
# probe rate when the local-endpoint live probes (reconcile-on-hit + the
# endpoint fallback) resolve the same model several times during one startup
# (banner, /model switch, compactor update_model). Keyed by (model, base_url);
# values are (result, monotonic_timestamp). Not persisted to disk — cross-
# restart freshness is handled by the reconcile logic re-probing after expiry.
_LOCAL_CTX_PROBE_TTL_SECONDS = 30.0
_LOCAL_CTX_PROBE_CACHE: Dict[tuple, tuple] = {}

_CONTEXT_LENGTH_KEYS = (
    "context_length",
    "context_window",
    "context_size",
    "max_context_length",
    "max_position_embeddings",
    "max_model_len",
    "max_input_tokens",
    "max_sequence_length",
    "max_seq_len",
    "n_ctx_train",
    "n_ctx",
    "ctx_size",
)

# Local server hostnames / address patterns
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
# LAN-only DNS suffixes: mDNS (``.local``), the conventional home-router
# zones, and RFC 8375's ``home.arpa``. None of these resolve publicly.
_LAN_HOST_SUFFIXES = (
    ".local",
    ".lan",
    ".home.arpa",
    ".localdomain",
)


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _auth_headers(api_key: str = "") -> Dict[str, str]:
    token = str(api_key or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}



def _maybe_cache_local_context_length(
    model: str,
    base_url: str,
    length: int,
) -> None:
    """Persist a locally probed context length only when it meets Son of Anton minimum.

    Sub-minimum live windows (e.g. vLLM ``--max-model-len 32768``) are still
    returned to callers so ``agent_init`` can fail with the existing
    minimum-context guidance — they must not be normalized into the on-disk cache
    as if they were valid operating limits.
    """
    if length >= MINIMUM_CONTEXT_LENGTH:
        save_context_length(model, base_url, length)


def _reconcile_local_cached_context_length(
    model: str,
    base_url: str,
    cached: int,
    api_key: str = "",
) -> int:
    """Return *cached* unless a live local probe reports a different limit.

    vLLM/Ollama operators can restart with a new ``--max-model-len`` / ``num_ctx``
    without changing the model id.  When the server is reachable, prefer its
    reported window over a stale disk entry; when the probe fails (offline tests,
    network blip), keep the cached value.

    Live probes below :data:`MINIMUM_CONTEXT_LENGTH` invalidate stale cache
    entries but are not persisted — startup should reject them, not bless a
    sub-64K window as config.
    """
    live_ctx = _query_local_context_length(model, base_url, api_key=api_key)
    if live_ctx and live_ctx > 0 and live_ctx != cached:
        if live_ctx < MINIMUM_CONTEXT_LENGTH:
            logger.info(
                "Live local probe for %s@%s reports %s (< minimum %s); "
                "invalidating stale cache — agent init should reject",
                model, base_url, f"{live_ctx:,}", f"{MINIMUM_CONTEXT_LENGTH:,}",
            )
            _invalidate_cached_context_length(model, base_url)
            return live_ctx
        logger.info(
            "Reconciling stale local cache entry %s@%s: %s -> %s (live probe)",
            model, base_url, f"{cached:,}", f"{live_ctx:,}",
        )
        _invalidate_cached_context_length(model, base_url)
        _maybe_cache_local_context_length(model, base_url, live_ctx)
        return live_ctx
    return cached


def is_local_endpoint(base_url: str) -> bool:
    """Return True if base_url points to a local machine.

    Recognises loopback (``localhost``, ``127.0.0.0/8``, ``::1``),
    LAN-only DNS names (``*.local``, ``*.lan``, ``*.home.arpa``), RFC-1918
    private ranges (``10/8``, ``172.16/12``, ``192.168/16``), link-local,
    and Tailscale CGNAT (``100.64.0.0/10``).
    """
    normalized = _normalize_base_url(base_url)
    if not normalized:
        return False
    url = normalized if "://" in normalized else f"http://{normalized}"
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except Exception:
        return False
    if host in _LOCAL_HOSTS:
        return True
    if any(host.endswith(suffix) for suffix in _LAN_HOST_SUFFIXES):
        return True
    # Unqualified hostnames (no dots) are local by definition — /etc/hosts
    # entries or mDNS names.
    if host and "." not in host:
        return True
    # RFC-1918 private ranges, link-local, and Tailscale CGNAT
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return True
        if isinstance(addr, ipaddress.IPv4Address) and addr in _TAILSCALE_CGNAT:
            return True
    except ValueError:
        pass
    return False


def _iter_nested_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_nested_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_nested_dicts(item)


def _coerce_reasonable_int(value: Any, minimum: int = 1024, maximum: int = 10_000_000) -> Optional[int]:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            value = value.strip().replace(",", "")
        result = int(value)
    except (TypeError, ValueError):
        return None
    if minimum <= result <= maximum:
        return result
    return None


def _extract_first_int(payload: Dict[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    keyset = {key.lower() for key in keys}
    for mapping in _iter_nested_dicts(payload):
        for key, value in mapping.items():
            if str(key).lower() not in keyset:
                continue
            coerced = _coerce_reasonable_int(value)
            if coerced is not None:
                return coerced
    return None


def _extract_context_length(payload: Dict[str, Any]) -> Optional[int]:
    return _extract_first_int(payload, _CONTEXT_LENGTH_KEYS)


def _extract_reasoning_decl(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Reasoning-effort vocabulary a ``/models`` entry advertises, if any.

    A front that knows which levels each model's server takes (maitred)
    lists them as ``reasoning_efforts`` (weakest first), the level the server
    applies when none is sent as ``default_reasoning_effort``, and its
    explicit rewrites (``medium`` → ``high`` on a none/low/high/max model) as
    ``reasoning_effort_aliases``. Read here so a custom route needs no
    hand-declared ``reasoning_efforts`` — the same way its context window
    comes from ``context_length``.
    """
    out: Dict[str, Any] = {}
    efforts = payload.get("reasoning_efforts")
    if isinstance(efforts, (list, tuple)):
        levels = tuple(
            level.strip().lower()
            for level in efforts
            if isinstance(level, str) and level.strip()
        )
        if levels:
            out["reasoning_efforts"] = levels
    default = payload.get("default_reasoning_effort")
    if isinstance(default, str) and default.strip():
        out["default_reasoning_effort"] = default.strip().lower()
    aliases = payload.get("reasoning_effort_aliases")
    if isinstance(aliases, dict):
        mapped = {
            k.strip().lower(): v.strip().lower()
            for k, v in aliases.items()
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
        }
        if mapped:
            out["reasoning_effort_aliases"] = mapped
    return out


def _extract_pricing(payload: Dict[str, Any]) -> Dict[str, Any]:
    alias_map = {
        "prompt": ("prompt", "input", "input_cost_per_token", "prompt_token_cost"),
        "completion": ("completion", "output", "output_cost_per_token", "completion_token_cost"),
        "request": ("request", "request_cost"),
        "cache_read": ("cache_read", "cached_prompt", "input_cache_read", "cache_read_cost_per_token"),
        "cache_write": ("cache_write", "cache_creation", "input_cache_write", "cache_write_cost_per_token"),
    }
    for mapping in _iter_nested_dicts(payload):
        normalized = {str(key).lower(): value for key, value in mapping.items()}
        if not any(any(alias in normalized for alias in aliases) for aliases in alias_map.values()):
            continue
        pricing: Dict[str, Any] = {}
        for target, aliases in alias_map.items():
            for alias in aliases:
                if alias in normalized and normalized[alias] not in {None, ""}:
                    pricing[target] = normalized[alias]
                    break
        if pricing:
            return pricing
    return {}


def _add_model_aliases(cache: Dict[str, Dict[str, Any]], model_id: str, entry: Dict[str, Any]) -> None:
    cache[model_id] = entry
    if "/" in model_id:
        bare_model = model_id.split("/", 1)[1]
        cache.setdefault(bare_model, entry)


def fetch_endpoint_model_metadata(
    base_url: str,
    api_key: str = "",
    force_refresh: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Fetch model metadata from an OpenAI-compatible ``/models`` endpoint.

    Results are cached in memory per base URL.
    """
    normalized = _normalize_base_url(base_url)
    if not normalized:
        return {}
    _ensure_requests()

    if not force_refresh:
        cached = _endpoint_model_metadata_cache.get(normalized)
        cached_at = _endpoint_model_metadata_cache_time.get(normalized, 0)
        if cached is not None and (time.time() - cached_at) < _ENDPOINT_MODEL_CACHE_TTL:
            return cached

    # Blackholed endpoint: every candidate below would spend its full 5s
    # connect budget. Returned empty rather than cached, so the endpoint is
    # retried as soon as the blackhole entry expires.
    if _endpoint_blackholed(normalized):
        return {}

    candidates = [normalized]
    if normalized.endswith("/v1"):
        alternate = normalized[:-3].rstrip("/")
    else:
        alternate = normalized + "/v1"
    if alternate and alternate not in candidates:
        candidates.append(alternate)

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    last_error: Optional[Exception] = None

    for candidate in candidates:
        # A connect timeout on one candidate condemns the host, not the path:
        # the remaining candidates differ only by URL suffix, so trying them
        # would repeat the same stall.
        if _endpoint_blackholed(normalized):
            break
        url = candidate.rstrip("/") + "/models"
        response = None
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=(5, 10),
                verify=_resolve_requests_verify(normalized),
                stream=True,
            )
            if response.status_code in (401, 403):
                logger.debug(
                    "Model metadata probe received HTTP %s from %s; stopping candidate probing",
                    response.status_code,
                    url,
                )
                break
            response.raise_for_status()
            payload = response.json()
            cache: Dict[str, Dict[str, Any]] = {}
            for model in payload.get("data", []):
                if not isinstance(model, dict):
                    continue
                model_id = model.get("id")
                if not model_id:
                    continue
                entry: Dict[str, Any] = {"name": model.get("name", model_id)}
                context_length = _extract_context_length(model)
                if context_length is not None:
                    entry["context_length"] = context_length
                pricing = _extract_pricing(model)
                if pricing:
                    entry["pricing"] = pricing
                entry.update(_extract_reasoning_decl(model))
                _add_model_aliases(cache, model_id, entry)

            _endpoint_model_metadata_cache[normalized] = cache
            _endpoint_model_metadata_cache_time[normalized] = time.time()
            return cache
        except Exception as exc:
            last_error = exc
            if _is_connect_timeout(exc):
                _note_endpoint_blackholed(normalized)
        finally:
            if response is not None:
                response.close()

    if last_error:
        logger.debug("Failed to fetch model metadata from %s/models: %s", normalized, last_error)
    _endpoint_model_metadata_cache[normalized] = {}
    _endpoint_model_metadata_cache_time[normalized] = time.time()
    return {}


def get_endpoint_reasoning_decl(
    model: str,
    base_url: str,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the reasoning vocabulary *base_url*'s ``/models`` lists for *model*.

    Same shape as ``get_custom_provider_reasoning_decl`` plus the aliases:
    ``{"efforts": tuple, "effort": str, "aliases": dict}``, each field absent
    when the endpoint does not list it. The model id is matched exactly (or
    by its bare name), never fuzzily: a neighbour's vocabulary is worse than
    none.

    With ``api_key=None`` only what an earlier fetch cached is read — no
    network, whatever its age — for per-request paths that hold no
    credential. With a key (``""`` for an open endpoint) the endpoint's
    ``/models`` is fetched through the usual per-URL cache. Keyless fetches
    are never made implicitly: a 401 is cached for the endpoint and would
    hide its context window from the keyed probe for the cache TTL.
    """
    if not model or not base_url:
        return {}
    if api_key is None:
        cache = _endpoint_model_metadata_cache.get(_normalize_base_url(base_url)) or {}
    else:
        try:
            cache = fetch_endpoint_model_metadata(base_url, api_key=api_key)
        except Exception:
            return {}
    entry = cache.get(model) or cache.get(_strip_provider_prefix(model))
    if not isinstance(entry, dict):
        return {}
    out: Dict[str, Any] = {}
    if entry.get("reasoning_efforts"):
        out["efforts"] = tuple(entry["reasoning_efforts"])
    if entry.get("default_reasoning_effort"):
        out["effort"] = entry["default_reasoning_effort"]
    if entry.get("reasoning_effort_aliases"):
        out["aliases"] = dict(entry["reasoning_effort_aliases"])
    return out


def _resolve_endpoint_context_length(
    model: str,
    base_url: str,
    api_key: str = "",
) -> Optional[int]:
    """Resolve context length from an endpoint's live ``/models`` metadata."""
    endpoint_metadata = fetch_endpoint_model_metadata(base_url, api_key=api_key)
    matched = endpoint_metadata.get(model)
    if not matched:
        if len(endpoint_metadata) == 1:
            matched = next(iter(endpoint_metadata.values()))
        elif model:
            # Substring fuzzy match — only meaningful with a non-empty model
            # name.  An empty string is a substring of EVERY key, which would
            # "match" whatever model the endpoint happens to list first (e.g.
            # a 32K embedding model) and poison the resolved context length
            # for the whole agent.
            for key, entry in endpoint_metadata.items():
                if model in key or key in model:
                    matched = entry
                    break
    if matched:
        context_length = matched.get("context_length")
        if isinstance(context_length, int):
            return context_length
    return None


def _get_context_cache_path() -> Path:
    """Return path to the persistent context length cache file."""
    from son_of_anton_constants import get_son_of_anton_home
    return get_son_of_anton_home() / "context_length_cache.toml"


def _load_context_cache() -> Dict[str, int]:
    """Load the model+provider -> context_length cache from disk."""
    path = _get_context_cache_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = fast_toml_load(f) or {}
        return data.get("context_lengths") or {}
    except Exception as e:
        logger.debug("Failed to load context length cache: %s", e)
        return {}


def _context_cache_key(model: str, base_url: str) -> str:
    """Canonical ``model@base_url`` key for the persistent context cache.

    Trailing slashes are stripped so ``http://host/v1`` and
    ``http://host/v1/`` share one entry instead of creating duplicates
    that can go stale independently.
    """
    return f"{model}@{(base_url or '').rstrip('/')}"


def save_context_length(model: str, base_url: str, length: int) -> None:
    """Persist a discovered context length for a model+provider combo.

    Cache key is ``model@base_url`` so the same model name served from
    different providers can have different limits.
    """
    # Never persist non-positive values — a 0 or negative context length
    # is always a bug and would poison the cache, causing downstream
    # `get_model_context_length()` to return 0 (since `0 is not None`).
    if length <= 0:
        logger.warning(
            "Refusing to cache non-positive context length %s -> %s tokens",
            f"{model}@{base_url}", length,
        )
        return
    key = _context_cache_key(model, base_url)
    cache = _load_context_cache()
    if cache.get(key) == length:
        return  # already stored
    cache[key] = length
    path = _get_context_cache_path()
    try:
        # Atomic write (temp file + fsync + os.replace): a plain truncating
        # ``open(path, "w")`` leaves the file empty/partial if the process is
        # killed mid-dump, and the next _load_context_cache() swallows the
        # resulting TOML error and returns {} — silently wiping EVERY cached
        # context length. It also exposes torn reads to a concurrent process
        # reading between truncate and dump-complete.
        atomic_toml_write(path, {"context_lengths": cache})
        logger.info("Cached context length %s -> %s tokens", key, f"{length:,}")
    except Exception as e:
        logger.debug("Failed to save context length cache: %s", e)


def get_cached_context_length(model: str, base_url: str) -> Optional[int]:
    """Look up a previously discovered context length for model+provider."""
    return _load_context_cache().get(_context_cache_key(model, base_url))


def _invalidate_cached_context_length(model: str, base_url: str) -> None:
    """Drop a stale cache entry so it gets re-resolved on the next lookup."""
    key = _context_cache_key(model, base_url)
    cache = _load_context_cache()
    # Invalidation must also drop the in-memory TTL probe entry for this
    # pair — otherwise the next resolution inside the TTL window reuses the
    # very value we just declared stale and re-persists it.
    _LOCAL_CTX_PROBE_CACHE.pop((_strip_provider_prefix(model), (base_url or "").rstrip("/")), None)
    if key not in cache:
        return
    cache.pop(key, None)
    path = _get_context_cache_path()
    try:
        # Atomic write — see save_context_length() for why a plain truncating
        # open() here risks wiping the entire cache on an interrupted dump.
        atomic_toml_write(path, {"context_lengths": cache})
    except Exception as e:
        logger.debug("Failed to invalidate context length cache entry %s: %s", key, e)


def parse_context_limit_from_error(error_msg: str) -> Optional[int]:
    """Try to extract the actual context limit from an API error message.

    Many providers include the limit in their error text, e.g.:
      - "maximum context length is 32768 tokens"
      - "context_length_exceeded: 131072"
      - "Maximum context size 32768 exceeded"
      - "model's max context length is 65536"
    """
    error_lower = error_msg.lower()
    # Pattern: look for numbers near context-related keywords
    patterns = [
        r'max_model_len\s*(?:is\s*)?[:=(]?\s*(\d{4,})',  # vLLM: "max_model_len 32768", "=32768", ": 32768", "(32768)", "is 32768"
        r'maximum model length\s*(?:is\s*)?[:=(]?\s*(\d{4,})',  # vLLM alt: "maximum model length 131072", "... is 131072"
        r'(?:max(?:imum)?|limit)\s*(?:context\s*)?(?:length|size|window)?\s*(?:is|of|:)?\s*(\d{4,})',
        r'context\s*(?:length|size|window)\s*(?:is|of|:)?\s*(\d{4,})',
        r'(\d{4,})\s*(?:token)?\s*(?:context|limit)',
        r'>\s*(\d{4,})\s*(?:max|limit|token)',  # "250000 tokens > 200000 maximum"
        r'(\d{4,})\s*(?:max(?:imum)?)\b',  # "200000 maximum"
    ]
    for pattern in patterns:
        match = re.search(pattern, error_lower)
        if match:
            limit = int(match.group(1))
            # Sanity check: must be a reasonable context length
            if 1024 <= limit <= 10_000_000:
                return limit
    return None


def get_context_length_from_provider_error(
    error_msg: str,
    current_context_length: int,
) -> Optional[int]:
    """Return a provider-reported lower context limit, if one is present.

    Context-overflow recovery must not invent a new model window size.  Some
    providers only say that the input exceeds the context window without
    reporting the actual maximum.  In that case callers should keep the
    configured context length and try compaction only, rather than stepping
    down through guessed probe tiers (1M → 256K → 128K → ...).
    """
    parsed_limit = parse_context_limit_from_error(error_msg)
    if parsed_limit is None:
        return None
    if parsed_limit < current_context_length:
        return parsed_limit
    return None


def parse_available_output_tokens_from_error(error_msg: str) -> Optional[int]:
    """Detect an "output cap too large" error and return how many output tokens are available.

    Background — two distinct context errors exist:
      1. "Prompt too long"  — the INPUT itself exceeds the context window.
           Fix: compact history, and only reduce context_length if the
           provider explicitly reports the actual lower limit.
      2. "max_tokens too large" — input is fine, but input + requested_output > window.
           Fix: reduce max_tokens (the output cap) for this call.
           Do NOT touch context_length — the window hasn't shrunk.

    Anthropic's API returns errors like:
      "max_tokens: 32768 > context_window: 200000 - input_tokens: 190000 = available_tokens: 10000"

    Returns the number of output tokens that would fit (e.g. 10000 above), or None if
    the error does not look like a max_tokens-too-large error.
    """
    error_lower = error_msg.lower()

    # Must look like an output-cap error, not a prompt-length error.
    is_output_cap_error = (
        "max_tokens" in error_lower
        and ("available_tokens" in error_lower or "available tokens" in error_lower)
    ) or (
        # OpenRouter/Nous phrasing of the same condition.
        "in the output" in error_lower
        and "maximum context length" in error_lower
    ) or (
        # LM Studio / llama.cpp / some OpenAI-compatible servers:
        #   "This model's maximum context length is 65536 tokens. However, you
        #    requested 65536 output tokens and your prompt contains 77409
        #    characters ..."
        # The "requested N output tokens" phrasing means the OUTPUT cap is the
        # problem (the input itself fits) — reduce max_tokens, don't compact.
        "maximum context length" in error_lower
        and "requested" in error_lower
        and "output tokens" in error_lower
    ) or (
        # DashScope / Alibaba Cloud (Qwen) phrasing.  The provider rejects an
        # over-cap output request with a bounded range whose upper bound IS the
        # real max-output cap, e.g.
        #   "Range of max_tokens should be [1, 65536]"
        # The input itself fits — this is purely an output-cap error, so reduce
        # max_tokens and retry; do NOT compact.
        "range of max_tokens should be" in error_lower
    ) or (
        # OpenAI-compatible relays may reject a request whose output cap exceeds
        # the model's separate completion-token limit, e.g.
        #   "max_tokens (98304) exceeds model's maximum output tokens (65536)"
        # This is independent of the input context window.
        "exceeds model" in error_lower
        and "maximum output tokens" in error_lower
    )
    if not is_output_cap_error:
        return None

    # Generic model-output-cap form:
    #   "max_tokens (98304) exceeds model's maximum output tokens (65536)"
    _m_max_output = re.search(
        r'exceeds model(?:\'s)? maximum output tokens\s*\(?\s*(\d+)\s*\)?',
        error_lower,
    )
    if _m_max_output:
        _cap = int(_m_max_output.group(1))
        if _cap >= 1:
            return _cap

    # DashScope / Alibaba range form: "Range of max_tokens should be [1, 65536]".
    # The upper bound is the available output cap.
    _m_range = re.search(
        r'range of max_tokens should be\s*\[\s*\d+\s*,\s*(\d+)\s*\]',
        error_lower,
    )
    if _m_range:
        _cap = int(_m_range.group(1))
        if _cap >= 1:
            return _cap

    # Extract the available_tokens figure.
    # Anthropic format: "… = available_tokens: 10000"
    patterns = [
        r'available_tokens[:\s]+(\d+)',
        r'available\s+tokens[:\s]+(\d+)',
        # fallback: last number after "=" in expressions like "200000 - 190000 = 10000"
        r'=\s*(\d+)\s*$',
    ]
    for pattern in patterns:
        match = re.search(pattern, error_lower)
        if match:
            tokens = int(match.group(1))
            if tokens >= 1:
                return tokens

    # OpenRouter/Nous format: "maximum context length is N … (A of text input,
    # B of tool input, C in the output)". Available output = ctx - text - tool.
    _m_ctx = re.search(r'maximum context length is (\d+)', error_lower)
    _m_parts = re.search(
        r'\((\d+)\s+of text input,\s*(\d+)\s+of tool input,\s*(\d+)\s+in the output\)',
        error_lower,
    )
    if _m_ctx and _m_parts:
        _available = int(_m_ctx.group(1)) - int(_m_parts.group(1)) - int(_m_parts.group(2))
        if _available >= 1:
            return _available

    # LM Studio / llama.cpp style: context window is reported in tokens but the
    # prompt size is reported in CHARACTERS, e.g.
    #   "maximum context length is 65536 tokens ... your prompt contains 77409
    #    characters ...".
    # Estimate the input tokens conservatively (~3 chars/token, which
    # over-reserves the input so the retried output cap stays safely inside the
    # window) and leave the remainder of the window for output.
    _m_ctx_tok = re.search(r'maximum context length is (\d+)\s*token', error_lower)
    _m_chars = re.search(r'prompt contains (\d+)\s*character', error_lower)
    if _m_ctx_tok and _m_chars:
        _ctx = int(_m_ctx_tok.group(1))
        _est_input = (int(_m_chars.group(1)) + 2) // 3
        _available = _ctx - _est_input
        if _available >= 1:
            return _available

    # vLLM style: both the window and the prompt are reported in TOKENS, e.g.
    #   "This model's maximum context length is 131072 tokens. However, you
    #    requested 65536 output tokens and your prompt contains at least 65537
    #    input tokens, for a total of at least 131073 tokens. Please reduce
    #    the length of the input prompt or the number of requested output
    #    tokens."
    # Available output = window - input. When the input alone is at or over
    # the window this stays None, so the caller correctly falls through to
    # compaction instead of futilely shrinking the output cap.
    #
    # Caveat: when max_tokens is the BINDING constraint, vLLM does not report
    # the real prompt size at all.  It back-computes a lower bound from the
    # constraint itself -- "at least N input tokens" where
    # N == window + 1 - requested_output -- so window - N is always exactly
    # requested_output - 1.  Subtracting the caller's safety margin then walks
    # the cap down ~65 tokens per retry while the reported input walks up by
    # the same amount, burning every compaction attempt without ever fitting.
    # Detect that degenerate case and halve the requested cap instead: it
    # carries the same guarantee (strictly below what was rejected) and
    # converges in one or two retries.
    _m_vllm_input = re.search(
        r'prompt contains (?:at least )?(\d+)\s*input tokens', error_lower
    )
    if _m_ctx_tok and _m_vllm_input:
        _available = int(_m_ctx_tok.group(1)) - int(_m_vllm_input.group(1))
        _m_requested_out = re.search(r'requested (\d+)\s*output tokens', error_lower)
        if 'at least' in error_lower and _m_requested_out:
            _requested_out = int(_m_requested_out.group(1))
            if _available >= _requested_out - 1:
                # The budget is derived from the constraint, not measured.
                return max(1, _requested_out // 2)
        if _available >= 1:
            return _available

    return None


def is_output_cap_error(error_msg: str) -> bool:
    """Return True if a 400 is about the OUTPUT cap (max_tokens) being too large.

    This is the broader sibling of :func:`parse_available_output_tokens_from_error`:
    that function only returns a number when it can extract the available output
    budget from a *known* provider phrasing.  This one answers the cheaper
    yes/no question — "is this an output-cap error at all?" — across providers
    whose exact wording we may not yet parse a number from.

    Why this matters: an output-cap 400 is deterministic (every retry with the
    same ``max_tokens`` gets the identical rejection).  If such an error is
    misclassified as a context-overflow it gets routed into the compaction
    loop, the compactor re-issues the call with the same oversized
    ``max_tokens``, the provider rejects it identically, and the session
    death-loops until "cannot compact further" (issue #55546, DashScope/Qwen:
    "Range of max_tokens should be [1, 65536]").  Compaction cannot help an
    output-cap error — the input already fits.

    The signal: the error talks about ``max_tokens`` (or its aliases) as a
    cap/range/limit, and does NOT talk about the INPUT/prompt/context window
    being too long.  When both are present we defer to the context-overflow
    path (a real input overflow can also mention max_tokens).
    """
    error_lower = error_msg.lower()

    mentions_output_param = (
        "max_tokens" in error_lower
        or "max_output_tokens" in error_lower
        or "max_completion_tokens" in error_lower
    )
    if not mentions_output_param:
        return False

    # Phrasing that signals the OUTPUT cap specifically is the problem.
    output_cap_signal = (
        "range of max_tokens should be" in error_lower      # DashScope / Alibaba
        or "available_tokens" in error_lower                # Anthropic
        or "available tokens" in error_lower
        or ("in the output" in error_lower                  # OpenRouter / Nous
            and "maximum context length" in error_lower)
        or ("requested" in error_lower                      # LM Studio / llama.cpp
            and "output tokens" in error_lower)
        or "should be" in error_lower                       # generic "max_tokens should be <= N"
        or "less than or equal" in error_lower
        or "must be" in error_lower
        or ("exceeds model" in error_lower
            and "maximum output tokens" in error_lower)
    )
    if not output_cap_signal:
        return False

    # If the error ALSO clearly describes an oversized INPUT, it is a genuine
    # context overflow that happens to mention max_tokens — let the
    # context-overflow path handle it (it can compact the input).
    input_overflow_signal = (
        "prompt is too long" in error_lower
        or "prompt too long" in error_lower
        or "input is too long" in error_lower
        or "input token" in error_lower
        or "prompt length" in error_lower
        or "prompt contains" in error_lower
        or "reduce the length" in error_lower
    )
    return not input_overflow_signal


def _model_id_matches(candidate_id: str, lookup_model: str) -> bool:
    """Return True if *candidate_id* (from server) matches *lookup_model* (configured).

    Supports two forms:
    - Exact match:  "nvidia-nemotron-super-49b-v1" == "nvidia-nemotron-super-49b-v1"
    - Slug match:   "nvidia/nvidia-nemotron-super-49b-v1" matches "nvidia-nemotron-super-49b-v1"
                    (the part after the last "/" equals lookup_model)

    This covers LM Studio's native API which stores models as "publisher/slug"
    while users typically configure only the slug after the "local:" prefix.
    """
    if candidate_id == lookup_model:
        return True
    # Slug match: basename of candidate equals the lookup name
    if "/" in candidate_id and candidate_id.rsplit("/", 1)[1] == lookup_model:
        return True
    return False


def _model_name_suggests_minimax_m3(model: str) -> bool:
    """Return True if the model name looks like MiniMax M3.

    Catches ``MiniMax-M3``, ``minimax/minimax-m3``, and similar variants
    across surfaces. Used by the models.dev underreport guard below and the
    cache-control gating in agent_runtime_helpers (stale persisted cache
    entries are handled generically by _stale_pre_catalog_cache_entry).
    """
    return "minimax-m3" in model.lower()


def _query_local_context_length(model: str, base_url: str, api_key: str = "") -> Optional[int]:
    """Query a local server for the model's context length (short-TTL cached).

    Reconcile-on-hit and the endpoint fallback can resolve the same model
    several times in quick succession during one startup — banner display,
    ``/model`` switch, compactor ``update_model``. A tiny in-process TTL
    cache collapses back-to-back probes for the same (model, base_url) into
    one network round-trip without persisting anything to disk.
    """
    import time as _time

    cache_key = (_strip_provider_prefix(model), base_url.rstrip("/"))
    now = _time.monotonic()
    cached = _LOCAL_CTX_PROBE_CACHE.get(cache_key)
    if cached is not None and (now - cached[1]) < _LOCAL_CTX_PROBE_TTL_SECONDS:
        return cached[0]

    result = _query_local_context_length_uncached(model, base_url, api_key=api_key)
    # Cache only positive results. A None/failure (server not up yet,
    # connection refused, timeout) must NOT be memoized — otherwise a probe
    # that fails during a startup race would suppress a legit retry seconds
    # later once the server is reachable.
    if result:
        _LOCAL_CTX_PROBE_CACHE[cache_key] = (result, now)
    return result


def _query_local_context_length_uncached(model: str, base_url: str, api_key: str = "") -> Optional[int]:
    """Query a local server for the model's context length."""
    import httpx

    model = _strip_provider_prefix(model)

    # Strip /v1 suffix to get the server root
    server_url = base_url.rstrip("/")
    if server_url.endswith("/v1"):
        server_url = server_url[:-3]

    if _endpoint_blackholed(server_url):
        return None

    headers = _auth_headers(api_key)

    try:
        with httpx.Client(timeout=3.0, headers=headers) as client:
            # /v1/models/{model} (maitred, vLLM)
            resp = client.get(f"{server_url}/v1/models/{model}")
            if resp.status_code == 200:
                data = resp.json()
                # vLLM returns max_model_len
                ctx = data.get("max_model_len") or data.get("context_length") or data.get("max_tokens")
                if ctx and isinstance(ctx, (int, float)):
                    return int(ctx)

            # Try /v1/models and find the model in the list.
            # Use _model_id_matches to handle "publisher/slug" vs bare "slug".
            resp = client.get(f"{server_url}/v1/models")
            if resp.status_code == 200:
                data = resp.json()
                models_list = data.get("data", [])
                # Match by id; on single-model servers (e.g. llama.cpp) the
                # configured name rarely equals the reported id (a GGUF path),
                # so fall back to the sole model when nothing matches.
                matched = None
                for m in models_list:
                    if _model_id_matches(m.get("id", ""), model):
                        matched = m
                        break
                if matched is None and len(models_list) == 1:
                    matched = models_list[0]
                if matched is not None:
                    # llama.cpp nests the runtime context under meta.n_ctx; the
                    # vLLM/OpenAI keys are also checked. Runtime n_ctx is
                    # preferred over n_ctx_train (the training maximum, which
                    # can be larger than what the server actually allocates).
                    for source in (matched, matched.get("meta") or {}):
                        if not isinstance(source, dict):
                            continue
                        for key in (
                            "n_ctx",
                            "context_length",
                            "context_window",
                            "max_model_len",
                            "max_context_length",
                            "max_tokens",
                            "n_ctx_train",
                        ):
                            val = source.get(key)
                            if isinstance(val, (int, float)) and val:
                                return int(val)
    except Exception as exc:
        if _is_connect_timeout(exc):
            _note_endpoint_blackholed(server_url)

    return None


def get_model_context_length(
    model: str,
    base_url: str = "",
    api_key: str = "",
    config_context_length: int | None = None,
    provider: str = "",
    custom_providers: list | None = None,
    *,
    unknown: int | None = None,
) -> int:
    """Get the context length for a model.

    ``unknown`` replaces the 256K default fallback when no source knows the
    window — pass ``0`` to tell "detection failed" apart from a model whose
    real window is 256K. The fallback warning is left to such a caller.

    Resolution order:
    0. Explicit config override (model.context_length or custom_providers per-model)
    1. Persistent cache (written when an overflow error names a lower limit);
       a local endpoint's live probe reconciles it
    2. The endpoint's ``/models`` metadata, then a local server's own query
    3. Default fallback (256K)"""
    # 0. Explicit config override — user knows best
    if config_context_length is not None and isinstance(config_context_length, int) and config_context_length > 0:
        return config_context_length

    # 0c. custom_providers per-model override — check before any probe.
    # ``custom_providers`` is loaded here when the caller did not pass it, so
    # every caller (the CLI's @-reference sizing, the context compactor, ...)
    # honors a context_length sitting in config.toml.
    if base_url and model:
        effective_custom_providers = custom_providers
        if effective_custom_providers is None:
            try:
                from son_of_anton_cli.config import (
                    get_compatible_custom_providers,
                    load_config,
                )
                effective_custom_providers = get_compatible_custom_providers(
                    load_config()
                )
            except Exception:
                effective_custom_providers = None
        if effective_custom_providers:
            try:
                from son_of_anton_cli.config import (
                    get_custom_provider_context_length,
                )
                cp_ctx = get_custom_provider_context_length(
                    model=model,
                    base_url=base_url,
                    custom_providers=effective_custom_providers,
                )
                if cp_ctx:
                    return cp_ctx
            except Exception:
                pass  # fall through to probing

    # Malformed user-provided URLs (for example an unmatched IPv6 bracket)
    # make urllib.parse raise. Context resolution should treat those as an
    # unknown endpoint rather than crashing before the inference layer can
    # report the configuration error itself.
    if base_url:
        try:
            parsed_base_url = urlparse(_normalize_base_url(base_url))
            _ = parsed_base_url.port
        except ValueError:
            base_url = ""

    # An empty/blank model id can't be meaningfully resolved: the endpoint
    # matcher's `model in key` check is vacuously true for "", returning
    # whatever context length a random entry has. Fall back to the default
    # immediately instead.
    if not str(model or "").strip():
        if unknown is not None:
            return unknown
        logger.info(
            "No model id provided for context length resolution — defaulting to %s tokens.",
            f"{DEFAULT_FALLBACK_CONTEXT:,}",
        )
        return DEFAULT_FALLBACK_CONTEXT

    # Normalise provider-prefixed model names (e.g. "local:model-name" →
    # "model-name") so cache lookups and server queries use the bare ID that
    # local servers actually know about.  Ollama "model:tag" colons are preserved.
    model = _strip_provider_prefix(model)

    # 1. Check persistent cache (model+provider)
    if base_url:
        cached = get_cached_context_length(model, base_url)
        if cached is not None:
            # Reject non-positive cached values — a 0 or negative value
            # is always a bug (corrupted cache, probe failure, or manual
            # edit).  Without this guard, `0 is not None` short-circuits
            # the resolution chain and the compactor gets context_length=0,
            # breaking every status-bar and /usage display downstream.
            if cached <= 0:
                logger.warning(
                    "Dropping non-positive cache entry %s@%s -> %s; re-resolving",
                    model, base_url, cached,
                )
                _invalidate_cached_context_length(model, base_url)
            else:
                if is_local_endpoint(base_url):
                    return _reconcile_local_cached_context_length(
                        model, base_url, cached, api_key=api_key,
                    )
                return cached

    # 2. The endpoint's own metadata.
    if base_url:
        context_length = _resolve_endpoint_context_length(model, base_url, api_key=api_key)
        if context_length is not None:
            return context_length
        if is_local_endpoint(base_url):
            local_ctx = _query_local_context_length(model, base_url, api_key=api_key)
            if local_ctx and local_ctx > 0:
                _maybe_cache_local_context_length(model, base_url, local_ctx)
                return local_ctx
        logger.info(
            "Could not detect context length for model %r at %s — "
            "defaulting to %s tokens (probe-down). Set model.context_length "
            "in config.toml to override.",
            model, base_url, f"{DEFAULT_FALLBACK_CONTEXT:,}",
        )

    # 3. Default fallback — warn (deduped per model+endpoint) so
    #    small-context models don't silently get 256K. See
    #    _warn_context_length_fallback for rationale.
    if unknown is not None:
        return unknown
    _warn_context_length_fallback(model, base_url)
    return DEFAULT_FALLBACK_CONTEXT


async def get_model_context_length_async(
    model: str,
    base_url: str = "",
    api_key: str = "",
    config_context_length: int | None = None,
    provider: str = "",
    custom_providers: list | None = None,
) -> int:
    """Async variant of get_model_context_length.

    Offloads the entire synchronous resolution chain (which contains
    blocking HTTP calls via ``requests``) to a background thread so it
    does not freeze the asyncio event loop and cause Discord heartbeat
    timeouts.

    Shares all logic with the sync version — no code duplication.
    """
    import asyncio
    return await asyncio.to_thread(
        get_model_context_length,
        model,
        base_url=base_url,
        api_key=api_key,
        config_context_length=config_context_length,
        provider=provider,
        custom_providers=custom_providers,
    )


# Same codepoint ranges as _is_cjk_token_dense_char, as a compiled character
# class so dense-char counting runs in C (``len(text) - len(re.sub(...))``)
# instead of a per-char Python loop.  MUST stay in sync with
# _is_cjk_token_dense_char.
_CJK_DENSE_RE = re.compile(
    "[\u1100-\u11ff"  # Hangul Jamo
    "\u2e80-\u9fff"  # CJK radicals/ideographs
    "\ua960-\ua97f"  # Hangul Jamo Extended-A
    "\uac00-\ud7af"  # Hangul Syllables
    "\uf900-\ufaff"  # CJK compatibility ideographs
    "\uff00-\uffef]"  # Fullwidth forms / halfwidth kana
)


def estimate_tokens_rough(text: str) -> int:
    """Rough token estimate for pre-flight checks.

    Uses ceiling division so short texts (1-3 chars) never estimate as
    0 tokens, which would cause the compactor and pre-flight checks to
    systematically undercount when many short tool results are present.
    CJK/Hangul/Kana text is much denser than English under common LLM
    tokenizers, so count those codepoints as roughly one token each instead
    of applying the English-centric ~4 chars/token rule.

    Perf: this runs on every message in every estimate/compaction walk,
    including MB-scale tool outputs, so the common all-ASCII case must stay
    O(1).  ``str.isascii()`` is a flag check on CPython's compact unicode
    representation (no scan), and the CJK counting itself is a single
    C-level ``re.findall`` rather than a per-character Python loop.
    """
    if not text:
        return 0
    text = str(text)
    if text.isascii():
        # O(1) fast path — ASCII text cannot contain token-dense CJK chars.
        return (len(text) + 3) // 4
    dense = len(text) - len(_CJK_DENSE_RE.sub("", text))
    if not dense:
        # Non-ASCII but no CJK (accents, Cyrillic, emoji, ...): keep the
        # classic ~4 chars/token rule.
        return (len(text) + 3) // 4
    sparse = len(text) - dense
    return dense + ((sparse + 3) // 4)


def estimate_messages_tokens_rough(messages: List[Dict[str, Any]]) -> int:
    """Rough token estimate for a message list (pre-flight only).

    Image parts (base64 PNG/JPEG) are counted as a flat ~1500 tokens per
    image — the Anthropic pricing model — instead of counting raw base64
    character length. Without this, a single ~1MB screenshot would be
    estimated at ~250K tokens and trigger premature context compaction.

    Per-message results are memoized (see ``_estimate_message_tokens_cached``)
    keyed on a deep *identity fingerprint* of the message, so re-walking a
    long history every iteration only pays for messages whose object graph
    actually changed. The memo is exact: equal fingerprints imply identical
    leaf objects and structure, hence an identical estimate.
    """
    _IMAGE_TOKEN_COST = 1500
    total = 0
    for msg in messages:
        total += _estimate_message_tokens_cached(msg, _IMAGE_TOKEN_COST)
    return total


# --- Per-message token-estimate memo -------------------------------------
#
# ``estimate_messages_tokens_rough`` is called on the full history every
# loop iteration (conversation_loop request sizing) and repeatedly during
# compaction telemetry. The per-message
# helpers are pure functions of the message's value, so a memo keyed on a
# fingerprint that uniquely determines the value is exactly equivalent.
#
# Fingerprint design (soundness argument):
#   * strings are fingerprinted by ``id()`` AND pinned (a strong reference is
#     stored in the cache entry). While the entry lives, that id cannot be
#     reused by another object, so id-equality implies object-equality —
#     strings are immutable, so value-equality too (no #50372-style aliasing).
#   * ints/floats/bools/None are fingerprinted by value.
#   * dicts/lists recurse structurally, preserving key order — ``str(shadow)``
#     depends on insertion order, so order is part of the key.
#   * any other type aborts the memo and falls through to a direct compute.
# Equal fingerprints therefore imply deep-equal messages built from identical
# immutable leaves ⇒ identical ``str(shadow)`` bytes ⇒ identical estimate.
#
# Because the api_messages build shallow-copies history dicts each iteration,
# the copies share the same content strings — so unchanged history messages
# hit the memo even though the outer dicts are fresh objects every turn.
_MSG_TOKENS_CACHE: Dict[Any, Tuple[list, int]] = {}
_MSG_TOKENS_CACHE_MAX = 4096


def _msg_fingerprint(value: Any, pins: list) -> Any:
    if value is None or value is True or value is False:
        return value
    t = type(value)
    if t is str:
        pins.append(value)
        return ("s", id(value))
    if t is int or t is float:
        return ("n", t.__name__, value)
    if t is dict:
        return ("d", tuple(
            (_msg_fingerprint(k, pins), _msg_fingerprint(v, pins))
            for k, v in value.items()
        ))
    if t is list:
        return ("l", tuple(_msg_fingerprint(v, pins) for v in value))
    if t is tuple:
        return ("t", tuple(_msg_fingerprint(v, pins) for v in value))
    raise ValueError("unfingerprintable message value")


def _estimate_message_tokens_cached(msg: Any, image_cost: int) -> int:
    try:
        pins: list = []
        key = _msg_fingerprint(msg, pins)
        hash(key)
    except Exception:
        return (
            _estimate_message_tokens_without_images(msg)
            + _count_image_tokens(msg, image_cost)
        )
    cached = _MSG_TOKENS_CACHE.get(key)
    if cached is not None:
        return cached[1]
    tokens = (
        _estimate_message_tokens_without_images(msg)
        + _count_image_tokens(msg, image_cost)
    )
    _MSG_TOKENS_CACHE[key] = (pins, tokens)
    while len(_MSG_TOKENS_CACHE) > _MSG_TOKENS_CACHE_MAX:
        try:
            _MSG_TOKENS_CACHE.pop(next(iter(_MSG_TOKENS_CACHE)))
        except (StopIteration, KeyError, RuntimeError):
            break
    return tokens


def _count_image_tokens(msg: Dict[str, Any], cost_per_image: int) -> int:
    """Count image-like content parts in a message; return their token cost."""
    count = 0
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in {"image", "image_url", "input_image"}:
                count += 1
    stashed = msg.get("_anthropic_content_blocks") if isinstance(msg, dict) else None
    if isinstance(stashed, list):
        for part in stashed:
            if isinstance(part, dict) and part.get("type") == "image":
                count += 1
    # Multimodal tool results that haven't been converted yet.
    if isinstance(content, dict) and content.get("_multimodal"):
        inner = content.get("content")
        if isinstance(inner, list):
            for part in inner:
                if isinstance(part, dict) and part.get("type") in {"image", "image_url"}:
                    count += 1
    return count * cost_per_image


def _wire_message_shadow(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Shadow of a message holding only what the provider actually receives.

    Two adjustments to the raw persisted dict:

    * ``api_content`` is a SUBSTITUTE for ``content``, not an addition to it.
      ``turn_context.substitute_api_content()`` pops the sidecar and overwrites
      ``content`` at every API-bound build site, so exactly one of the two is
      ever sent. Counting both double-counts any message whose sidecar differs
      from its clean stored content (2.00x on a 40KB sidecar).

      The substitution mirrors that helper's guard exactly: only a non-empty
      STRING sidecar on a ``user``/``assistant`` row displaces ``content``.
      Any other sidecar shape is popped and discarded on the wire without
      touching ``content``, so a shadow that substituted unconditionally
      would UNDERcount those rows — the dangerous direction, since it makes
      compaction fire too late and the turn dies on a hard context error.
    * Base64 image payloads are replaced with a placeholder; they are charged
      separately at a flat rate by ``_count_image_tokens``, and counting their
      raw chars here would massively overestimate usage.
    """
    sidecar = msg.get("api_content")
    sidecar_wins = (
        isinstance(sidecar, str)
        and bool(sidecar)
        and msg.get("role") in ("user", "assistant")
    )
    shadow: Dict[str, Any] = {}
    for k, v in msg.items():
        if k in ("_anthropic_content_blocks", "reasoning_details") or k in PERSISTENCE_ONLY_MESSAGE_FIELDS:
            continue
        if k == "api_content":
            # Always popped before the request is built; only counted when it
            # actually replaces ``content``.
            if sidecar_wins:
                shadow["content"] = v
            continue
        if k == "content":
            if sidecar_wins:
                # The sidecar wins on the wire; skip the clean copy so the
                # same logical content is not counted twice.
                continue
            if isinstance(v, list):
                cleaned = []
                for part in v:
                    if isinstance(part, dict):
                        if part.get("type") in {"image", "image_url", "input_image"}:
                            cleaned.append({"type": part.get("type"), "image": "[stripped]"})
                        else:
                            cleaned.append(part)
                    else:
                        cleaned.append(part)
                shadow[k] = cleaned
            elif isinstance(v, dict) and v.get("_multimodal"):
                shadow[k] = v.get("text_summary", "")
            else:
                shadow[k] = v
        else:
            shadow[k] = v
    return shadow


def _estimate_message_tokens_without_images(msg: Dict[str, Any]) -> int:
    """Token estimate for a message shadow with image payloads stripped."""
    if not isinstance(msg, dict):
        return estimate_tokens_rough(str(msg))
    return estimate_tokens_rough(str(_wire_message_shadow(msg)))


def estimate_request_tokens_rough(
    messages: List[Dict[str, Any]],
    *,
    system_prompt: str = "",
    tools: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Rough token estimate for a full chat-completions request.

    Includes the major payload buckets Son of Anton sends to providers:
    system prompt, conversation messages, and tool schemas.  With 50+
    tools enabled, schemas alone can add 20-30K tokens — a significant
    blind spot when only counting messages. Image content is counted
    at a flat per-image cost (see estimate_messages_tokens_rough).
    """
    total = 0
    if system_prompt:
        total += estimate_tokens_rough(system_prompt)
    if messages:
        total += estimate_messages_tokens_rough(messages)
    if tools:
        total += _estimate_tools_tokens_rough(tools)
    return total


# NOTE: tool schemas can be large. Avoid repeated `str(tools)` conversions,
# which are CPU-heavy and can stall GUI event loops under GIL pressure.
#
# Keyed by ``id(tools)``. A long-lived gateway/desktop backend builds many
# transient tool lists over its lifetime, so the cache is bounded and evicts
# oldest-first (insertion-ordered dict) once it exceeds the cap. The cap is
# generous relative to how rarely toolsets are rebuilt within a process.
_TOOLS_TOKENS_CACHE: dict[int, Tuple[int, str, str, int]] = {}
_TOOLS_TOKENS_CACHE_MAX = 256


def _tool_name_for_cache(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        if isinstance(name, str):
            return name
    name = tool.get("name")
    return name if isinstance(name, str) else ""


def _estimate_tools_tokens_rough(tools: List[Dict[str, Any]]) -> int:
    if not tools:
        return 0

    # Cache by list identity. Tools are rebuilt rarely (toolset changes),
    # but token estimates are requested frequently (request sizing, compaction).
    key = id(tools)
    n = len(tools)
    first = _tool_name_for_cache(tools[0]) if n else ""
    last = _tool_name_for_cache(tools[-1]) if n else ""

    cached = _TOOLS_TOKENS_CACHE.get(key)
    if cached is not None:
        cached_n, cached_first, cached_last, cached_tokens = cached
        if cached_n == n and cached_first == first and cached_last == last:
            return cached_tokens

    # Fast, stable rough estimate: sum lengths of the major schema fields.
    # This avoids the pathological `str(tools)` path while still scaling with
    # schema size (descriptions + parameters dominate).
    total_chars = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict):
            name = fn.get("name") or ""
            desc = fn.get("description") or ""
            params = fn.get("parameters") or {}
        else:
            name = tool.get("name") or ""
            desc = tool.get("description") or ""
            params = tool.get("parameters") or {}

        if isinstance(name, str):
            total_chars += len(name)
        if isinstance(desc, str):
            total_chars += len(desc)
        # Parameters can be nested; JSON is closer to over-the-wire size than repr().
        try:
            total_chars += len(json.dumps(params, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            total_chars += len(str(params))

    tokens = (total_chars + 3) // 4
    # Bound the cache: drop the oldest entry when the cap is exceeded so a
    # long-running process can't accumulate an unbounded number of stale
    # ``id(tools)`` entries (id values are recycled after GC anyway).
    if len(_TOOLS_TOKENS_CACHE) >= _TOOLS_TOKENS_CACHE_MAX:
        _TOOLS_TOKENS_CACHE.pop(next(iter(_TOOLS_TOKENS_CACHE)), None)
    _TOOLS_TOKENS_CACHE[key] = (n, first, last, tokens)
    return tokens


_KIMI_FAMILY_MODEL_PREFIXES = (
    "kimi-", "kimi_",
    "moonshot-", "moonshot_",
    "k1.", "k1-",
    "k2.", "k2-",
    "k25", "k2.5",
    "k3.", "k3-",
)

# Bare release slugs with no separator suffix (Kimi Coding Plan serves K3 as
# the exact slug ``k3``). Exact-match so unrelated names that merely start with
# the same characters aren't misclassified.
_KIMI_FAMILY_EXACT_SLUGS = frozenset({"k3"})


def _model_name_is_kimi_family(model: "str | None") -> bool:
    if not isinstance(model, str):
        return False
    m = model.strip().lower()
    if not m:
        return False
    # Strip vendor prefix (e.g. ``moonshotai/kimi-k2.5`` → ``kimi-k2.5``)
    if "/" in m:
        m = m.rsplit("/", 1)[-1]
    if m in _KIMI_FAMILY_EXACT_SLUGS:
        return True
    return m.startswith(_KIMI_FAMILY_MODEL_PREFIXES)


