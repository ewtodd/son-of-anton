"""Auxiliary LLM calls (titles, compaction, vision, approvals, ...).

Every call goes to the custom OpenAI-compatible endpoint. A task's route comes
from ``auxiliary.<task>`` in config.toml (``provider``/``model``/``base_url``/
``key_env``/``timeout``/``reasoning_effort``); ``provider = "auto"`` (the
default) runs the task on the main session's endpoint and model.

Failure handling: a transient transport error (connection drop, timeout, 5xx,
408) is retried on the same endpoint; a rejected ``temperature``,
``response_format`` or ``max_tokens`` is retried once without the field; and a
task pinned to its own model falls back to the main model when its model
cannot serve the request (rate limit, connection failure, incompatible model,
malformed response).
"""

import contextlib
import contextvars
import hashlib
import inspect
import logging
import os
import re
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse, urlunparse

from agent.reasoning_effort import custom_reasoning_extras
from utils import base_url_hostname, normalize_proxy_env_vars

logger = logging.getLogger(__name__)


# ── Availability probe mode ───────────────────────────────────────────────
# Tool gating (check_fns) only asks whether a client is resolvable; inside
# ``aux_probe_mode()`` client construction returns a stub instead of importing
# the SDK. Stubs are never cached.
_aux_probe_state = threading.local()


class _AuxProbeClientStub:
    """Non-functional placeholder returned while `aux_probe_mode` is active."""

    __slots__ = ("api_key", "base_url")

    def __init__(self, api_key: str = "", base_url: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(
            f"_AuxProbeClientStub used as a real client (attribute {name!r}); "
            "aux_probe_mode is for availability checks only"
        )


def _aux_probe_active() -> bool:
    return bool(getattr(_aux_probe_state, "active", False))


@contextlib.contextmanager
def aux_probe_mode():
    """Resolve provider availability without constructing real SDK clients."""
    prev = getattr(_aux_probe_state, "active", False)
    _aux_probe_state.active = True
    try:
        yield
    finally:
        _aux_probe_state.active = prev


def _create_openai_client(*, api_key: str, base_url: str, async_mode: bool = False) -> Any:
    """OpenAI/AsyncOpenAI client for the endpoint, with the agent's User-Agent.

    SDK-internal retries are off: this module owns the retry budget, and the
    SDK's own 3 attempts would triple the wall time of a hung call.
    """
    if _aux_probe_active():
        return _AuxProbeClientStub(api_key=api_key, base_url=base_url)
    from agent.transports.chat_completions import user_agent

    kwargs: Dict[str, Any] = {"default_headers": {"User-Agent": user_agent()}, "max_retries": 0}
    parsed = urlparse(base_url)
    if parsed.query:
        base_url = urlunparse(parsed._replace(query=""))
        kwargs["default_query"] = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    try:
        from agent.process_bootstrap import build_keepalive_http_client
        from agent.ssl_verify import resolve_httpx_verify

        http_client = build_keepalive_http_client(
            base_url, async_mode=async_mode, verify=resolve_httpx_verify(base_url=base_url)
        )
        if http_client is not None:
            kwargs["http_client"] = http_client
    except Exception:
        pass
    if async_mode:
        from openai import AsyncOpenAI as _cls
    else:
        from openai import OpenAI as _cls
    return _cls(api_key=api_key, base_url=base_url, **kwargs)


# ── Interrupt protection for atomic auxiliary tasks ──────────────────────
# Compaction must not be aborted mid-flight by an incoming message (the summary
# would be lost). A thread-local flag marks the call protected; an explicit
# host cancel (Ctrl+C, /stop) may still install a cancel check that wins.
_aux_interrupt_protection = threading.local()


class AuxiliaryExplicitCancellation(BaseException):
    """An auxiliary attempt was explicitly hard-cancelled by the host.

    A ``BaseException`` so retry code catching ``Exception`` never mistakes a
    host stop for a transport failure.
    """

    cause = "explicit_host_cancel"

    def __init__(self) -> None:
        super().__init__("auxiliary request explicitly cancelled by host")


def _aux_interrupt_protected() -> bool:
    return bool(getattr(_aux_interrupt_protection, "active", False))


@contextlib.contextmanager
def aux_interrupt_protection(active: bool = True, cancel_check=None, cancel_event=None):
    """Mark the current thread's auxiliary LLM call as interrupt-protected."""
    prev = getattr(_aux_interrupt_protection, "active", False)
    prev_cancel_check = getattr(_aux_interrupt_protection, "cancel_check", None)
    prev_cancel_event = getattr(_aux_interrupt_protection, "cancel_event", None)
    _aux_interrupt_protection.active = active
    if callable(cancel_check):
        _aux_interrupt_protection.cancel_check = cancel_check
    if cancel_event is not None and callable(getattr(cancel_event, "is_set", None)):
        _aux_interrupt_protection.cancel_event = cancel_event
    try:
        yield
    finally:
        _aux_interrupt_protection.active = prev
        _aux_interrupt_protection.cancel_check = prev_cancel_check
        _aux_interrupt_protection.cancel_event = prev_cancel_event


def _capture_aux_cancel_check() -> Optional[Callable[[], Any]]:
    event = getattr(_aux_interrupt_protection, "cancel_event", None)
    is_set = getattr(event, "is_set", None)
    if callable(is_set):
        return is_set
    check = getattr(_aux_interrupt_protection, "cancel_check", None)
    return check if callable(check) else None


def _captured_aux_cancel_requested(cancel_check: Callable[[], Any]) -> bool:
    try:
        return bool(cancel_check())
    except Exception:
        logger.debug("captured aux cancel check failed", exc_info=True)
        return False


class _AuxiliaryCancellationDecision:
    """Latch the first explicit cancellation for one attempt."""

    def __init__(self, source_cancel_check: Callable[[], Any]) -> None:
        self._source_cancel_check = source_cancel_check
        self._lock = threading.Lock()
        self._cancelled = False

    def __call__(self) -> bool:
        with self._lock:
            if not self._cancelled and _captured_aux_cancel_requested(self._source_cancel_check):
                self._cancelled = True
            return self._cancelled


# ── Forward-progress hook for streamed auxiliary calls ───────────────────
# Hosts watching a long compaction extend their deadline while tokens move;
# the stream consumers below tick the thread-local hook per chunk.
_aux_progress = threading.local()


def _notify_aux_progress() -> None:
    hook = getattr(_aux_progress, "hook", None)
    if hook is None:
        return
    try:
        hook()
    except Exception:
        logger.debug("aux progress hook failed", exc_info=True)


def _aux_progress_active() -> bool:
    return getattr(_aux_progress, "hook", None) is not None


@contextlib.contextmanager
def aux_progress_hook(hook):
    """Install *hook* as the current thread's aux forward-progress callback."""
    prev = getattr(_aux_progress, "hook", None)
    _aux_progress.hook = hook if callable(hook) else prev
    try:
        yield
    finally:
        _aux_progress.hook = prev


def _run_protected_sync_provider_call(callback: Callable[[dict], Any], kwargs: dict) -> Any:
    """Run one protected provider callback so a hard cancel can release its owner.

    Only protected calls with a cancel source take this path: the provider
    callback runs in a daemon worker while the owner polls cancellation, and
    on cancel the owner unwinds at once (the worker finishes under the
    request's own timeout). Every other call runs directly.
    """
    source_cancel_check = _capture_aux_cancel_check()
    if not _aux_interrupt_protected() or not callable(source_cancel_check):
        return callback(kwargs)
    cancel_check = _AuxiliaryCancellationDecision(source_cancel_check)
    if cancel_check():
        raise AuxiliaryExplicitCancellation()
    progress_hook = getattr(_aux_progress, "hook", None)
    provider_context = contextvars.copy_context()
    done = threading.Event()
    outcome: dict[str, Any] = {}

    def _provider_worker() -> None:
        try:
            with aux_progress_hook(progress_hook), aux_interrupt_protection(cancel_check=cancel_check):
                outcome["result"] = callback(kwargs)
        except BaseException as exc:
            outcome["exception"] = exc
        finally:
            done.set()

    threading.Thread(
        target=provider_context.run,
        args=(_provider_worker,),
        name="son-of-anton-protected-aux-provider",
        daemon=True,
    ).start()
    while True:
        if _captured_aux_cancel_requested(cancel_check):
            raise AuxiliaryExplicitCancellation()
        if not done.wait(0.02):
            continue
        if _captured_aux_cancel_requested(cancel_check):
            raise AuxiliaryExplicitCancellation()
        if outcome.get("exception") is not None:
            raise outcome["exception"]
        return outcome.get("result")


def _scoped_key_env(name: str) -> str:
    """Read an API key env var through the secret scope."""
    if not name:
        return ""
    try:
        from agent.secret_scope import get_secret

        return (get_secret(name) or "").strip()
    except Exception:
        return (os.getenv(name) or "").strip()


# ── The main session's runtime ────────────────────────────────────────────
# Context-local so concurrent gateway sessions never see each other's route.
_MAIN_RUNTIME_FIELDS = ("provider", "requested_provider", "model", "base_url", "api_key", "api_mode")
_RUNTIME_MAIN_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = (
    contextvars.ContextVar("auxiliary_runtime_main", default=None)
)


def set_runtime_main(
    provider: str,
    model: str,
    *,
    requested_provider: str = "",
    base_url: str = "",
    api_key: Any = "",
    api_mode: str = "",
    **_unused: Any,
) -> contextvars.Token:
    """Record the current context's live main runtime for auxiliary routing."""
    return _RUNTIME_MAIN_CONTEXT.set(_normalize_main_runtime({
        "provider": provider,
        "requested_provider": requested_provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "api_mode": api_mode,
    }))


@contextlib.contextmanager
def scoped_runtime_main(main_runtime: Optional[Dict[str, Any]]):
    """Temporarily bind an explicit runtime."""
    runtime = _normalize_main_runtime(main_runtime)
    token = _RUNTIME_MAIN_CONTEXT.set(runtime or None)
    try:
        yield runtime
    finally:
        _RUNTIME_MAIN_CONTEXT.reset(token)


def _normalize_main_runtime(main_runtime: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Sanitized copy of a main-runtime dict (the ambient one when None)."""
    if main_runtime is None:
        main_runtime = _RUNTIME_MAIN_CONTEXT.get()
    if not isinstance(main_runtime, dict):
        return {}
    normalized: Dict[str, Any] = {}
    for key in _MAIN_RUNTIME_FIELDS:
        value = main_runtime.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip().lower() if key in ("provider", "requested_provider") else value.strip()
    return normalized


def _runtime_main_value(field: str) -> Any:
    runtime = _RUNTIME_MAIN_CONTEXT.get()
    return (runtime or {}).get(field) or ""


def _read_model_section(key: str) -> str:
    try:
        from son_of_anton_cli.config import load_config_readonly

        model_cfg = load_config_readonly().get("model", {})
        if key == "default" and isinstance(model_cfg, str):
            return model_cfg.strip()
        if isinstance(model_cfg, dict):
            value = model_cfg.get(key, "")
            if isinstance(value, str):
                return value.strip()
    except Exception:
        pass
    return ""


def _read_main_model() -> str:
    """The live main model (runtime first, then ``model.default``)."""
    return str(_runtime_main_value("model") or _read_model_section("default"))


def _read_main_provider() -> str:
    """The live main provider (runtime first, then ``model.provider``)."""
    return str(_runtime_main_value("provider") or _read_model_section("provider")).lower()


def _resolve_custom_runtime() -> Tuple[Optional[str], Optional[str]]:
    """The configured custom endpoint's (base_url, api_key), as the main CLI resolves it."""
    try:
        from son_of_anton_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="custom")
    except Exception as exc:
        logger.debug("Auxiliary client: custom runtime resolution failed: %s", exc)
        return None, None
    base = str(runtime.get("base_url") or "").strip().rstrip("/")
    if not base:
        return None, None
    key = runtime.get("api_key")
    return base, (key.strip() if isinstance(key, str) and key.strip() else "no-key-required")


def _validate_proxy_env_urls() -> None:
    """Fail fast with a clear error when a proxy env var holds a malformed URL."""
    normalize_proxy_env_vars()
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        value = str(os.environ.get(key) or "").strip()
        if not value:
            continue
        try:
            parsed = urlparse(value)
            if parsed.scheme:
                _ = parsed.port
        except ValueError as exc:
            raise RuntimeError(
                f"Malformed proxy environment variable {key}={value!r}. "
                "Fix or unset your proxy settings and try again."
            ) from exc


def _validate_base_url(base_url: str) -> None:
    """Reject obviously broken endpoint URLs before they reach httpx."""
    candidate = str(base_url or "").strip()
    if not candidate:
        return
    try:
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"}:
            _ = parsed.port
    except ValueError as exc:
        raise RuntimeError(
            f"Malformed custom endpoint URL: {candidate!r}. Set a valid http(s) base URL in config.toml."
        ) from exc


# ── Error classification ─────────────────────────────────────────────────

def _is_timeout_error(exc: Exception) -> bool:
    try:
        from openai import APITimeoutError

        if isinstance(exc, APITimeoutError):
            return True
    except ImportError:
        pass
    return "Timeout" in type(exc).__name__ or "timed out" in str(exc).lower()


def _is_connection_error(exc: Exception) -> bool:
    """The endpoint is unreachable or dropped the connection (not an API error)."""
    try:
        from openai import APIConnectionError, APITimeoutError

        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return True
    except ImportError:
        pass
    if any(kw in type(exc).__name__ for kw in ("Connection", "Timeout", "DNS", "SSL")):
        return True
    err_lower = str(exc).lower()
    return any(kw in err_lower for kw in (
        "connection refused", "name or service not known", "no route to host",
        "network is unreachable", "timed out", "connection reset",
        "incomplete chunked read", "peer closed connection",
        "response ended prematurely", "unexpected eof",
        "remoteprotocolerror", "localprotocolerror",
    ))


def _is_transient_transport_error(exc: Exception) -> bool:
    """A blip worth retrying on the same endpoint: connection drop, 5xx, 408."""
    if _is_connection_error(exc):
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and (status == 408 or 500 <= status < 600)


_TRANSIENT_RETRIES = 2
# Base for exponential backoff between transient retries (seconds).
_TRANSIENT_RETRY_BACKOFF_BASE = 1.0


def _is_auth_error(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 401:
        return True
    return "error code: 401" in str(exc).lower() or "authenticationerror" in type(exc).__name__.lower()


_BILLING_KEYWORDS = (
    "credits", "insufficient funds", "billing", "payment required", "can only afford",
    "out of funds", "run out of funds", "balance_depleted", "no usable credits",
    "model_not_supported_on_free_tier", "not available on the free tier",
)


def _is_rate_limit_error(exc: Exception) -> bool:
    if type(exc).__name__ == "RateLimitError":
        return True
    if getattr(exc, "status_code", None) != 429:
        return False
    return not any(kw in str(exc).lower() for kw in _BILLING_KEYWORDS)


def _is_unsupported_parameter_error(exc: Exception, param: str) -> bool:
    """A 400 for an unsupported request parameter named *param*."""
    err_lower = str(exc).lower()
    if not param or param.lower() not in err_lower:
        return False
    return any(marker in err_lower for marker in (
        "unsupported parameter", "unsupported_parameter", "not supported",
        "does not support", "unknown parameter", "unrecognized request argument",
        "unrecognized parameter", "invalid parameter",
    ))


def _is_structured_output_rejection(exc: Exception) -> bool:
    """The endpoint rejected ``response_format`` (e.g. vLLM without a grammar backend)."""
    status = getattr(exc, "status_code", None)
    if status is not None and status not in {400, 422}:
        return False
    err_lower = str(exc).lower()
    if "guided_grammar" in err_lower or "xgrammar" in err_lower or "compile_grammar_error" in err_lower:
        return True
    if "extra inputs are not permitted" in err_lower and "response_format" in err_lower:
        return True
    if "response_format" in err_lower and "unavailable" in err_lower:
        return True
    return _is_unsupported_parameter_error(exc, "response_format")


def _is_model_incompatible_error(exc: Exception) -> bool:
    """The route cannot run this model (a valid name it does not serve here)."""
    if getattr(exc, "status_code", None) not in {400, None}:
        return False
    err_lower = str(exc).lower()
    if any(kw in err_lower for kw in (
        "model does not exist", "is not a valid model", "no such model", "model not found",
        "the model `", "model_not_found", "unknown model",
    )):
        return False
    if any(kw in err_lower for kw in _BILLING_KEYWORDS + ("free tier", "free-tier", "quota")):
        return False
    return any(kw in err_lower for kw in (
        "is not supported when using", "model is not supported", "not supported with this",
        "not supported for this account", "model_not_supported",
        "does not support this model", "unsupported model",
    ))


def _is_invalid_aux_response_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        isinstance(exc, RuntimeError)
        and "auxiliary " in msg
        and "llm returned invalid response" in msg
        and "choices[0].message" in msg
    )


# ── Task configuration ───────────────────────────────────────────────────

_DEFAULT_AUX_TIMEOUT = 30.0
# Compaction summarises large contexts; a config-derived timeout is floored
# here so a slow summary model is not cut off. Explicit per-call timeouts win.
_COMPACTION_TIMEOUT_FLOOR_SECONDS = 300.0
# Tasks whose output is a handful of tokens by definition: their explicit
# max_tokens is sent (a looping small model otherwise emits thousands).
_HARD_CAPPED_TASKS: frozenset = frozenset({"title_generation"})


def _get_auxiliary_task_config(task: str) -> Dict[str, Any]:
    """``auxiliary.<task>`` from config.toml, over any plugin-declared defaults."""
    if not task:
        return {}
    try:
        from son_of_anton_cli.config import load_config_readonly

        config = load_config_readonly()
    except ImportError:
        return {}
    aux = config.get("auxiliary", {}) if isinstance(config, dict) else {}
    task_config = aux.get(task, {}) if isinstance(aux, dict) else {}
    if not isinstance(task_config, dict):
        task_config = {}
    try:
        from son_of_anton_cli.plugins import get_plugin_auxiliary_tasks

        for entry in get_plugin_auxiliary_tasks():
            if entry.get("key") == task:
                defaults = entry.get("defaults") or {}
                if isinstance(defaults, dict):
                    return {**defaults, **task_config}
                break
    except Exception:
        pass
    return task_config


def _effective_aux_timeout(task: str, timeout: Optional[float]) -> float:
    if timeout is not None:
        return timeout
    effective = _DEFAULT_AUX_TIMEOUT
    raw = _get_auxiliary_task_config(task).get("timeout")
    if raw is not None:
        try:
            effective = float(raw)
        except (ValueError, TypeError):
            pass
    if task == "compaction":
        effective = max(effective, _COMPACTION_TIMEOUT_FLOOR_SECONDS)
    return effective


def _get_task_extra_body(task: str) -> Dict[str, Any]:
    """``auxiliary.<task>.extra_body``, plus ``reasoning_effort`` as ``extra_body.reasoning``."""
    task_config = _get_auxiliary_task_config(task)
    raw = task_config.get("extra_body")
    result = dict(raw) if isinstance(raw, dict) else {}
    effort = task_config.get("reasoning_effort")
    if "reasoning" not in result and effort is not None and effort != "":
        from son_of_anton_constants import parse_reasoning_effort

        parsed = parse_reasoning_effort(effort)
        if parsed is not None:
            result["reasoning"] = parsed
        else:
            logger.warning(
                "auxiliary.%s.reasoning_effort %r is not a valid level "
                "(none, minimal, low, medium, high, xhigh, max, ultra) — ignoring",
                task, effort,
            )
    return result


def _resolve_task_provider_model(
    task: str = None,
    provider: str = None,
    model: str = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Return ``(provider, model, base_url, api_key, api_mode)`` for a call.

    Explicit arguments win over ``auxiliary.<task>``; a base_url makes the
    route ``custom``; otherwise ``auto`` (the main session's route). A model
    of ``auto`` means "inherit", never a literal id.
    """
    cfg: Dict[str, Any] = _get_auxiliary_task_config(task) if task else {}
    cfg_provider = str(cfg.get("provider", "")).strip() or None
    cfg_model = str(cfg.get("model", "")).strip() or None
    cfg_base_url = str(cfg.get("base_url", "")).strip() or None
    cfg_api_key = str(cfg.get("api_key", "")).strip() or None
    if not cfg_api_key:
        key_env = str(cfg.get("key_env") or cfg.get("api_key_env") or "").strip()
        cfg_api_key = _scoped_key_env(key_env) or None if key_env else None
    if model and model.lower() == "auto":
        model = None
    if cfg_model and cfg_model.lower() == "auto":
        cfg_model = None
    resolved_model = model or cfg_model
    if provider and provider != "auto" and not base_url and cfg_base_url and cfg_provider in (None, provider):
        base_url = cfg_base_url
        api_key = api_key or cfg_api_key
    if base_url:
        return "custom", resolved_model, base_url, api_key, None
    if provider:
        return provider, resolved_model, None, api_key, None
    if cfg_base_url and cfg_api_key:
        return "custom", resolved_model, cfg_base_url, cfg_api_key, None
    if cfg_base_url and cfg_provider and cfg_provider != "auto":
        return cfg_provider, resolved_model, cfg_base_url, None, None
    if cfg_provider and cfg_provider != "auto":
        return cfg_provider, resolved_model, None, cfg_api_key, None
    return "auto", resolved_model, None, None, None


# ── Client resolution ────────────────────────────────────────────────────

def _route(
    provider: str,
    model: Optional[str],
    explicit_base_url: Optional[str],
    explicit_api_key: Optional[str],
    main_runtime: Optional[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """``(base_url, api_key, model)`` for an auxiliary call, or Nones."""
    runtime = _normalize_main_runtime(main_runtime)
    if not model:
        # "auto" follows the session's runtime; a pinned route the configured default.
        if (provider or "auto").strip().lower() == "auto":
            model = runtime.get("model") or _read_main_model() or None
        else:
            model = _read_main_model() or runtime.get("model") or None
    if explicit_base_url:
        base = explicit_base_url.strip().rstrip("/")
        key = (explicit_api_key or "").strip() or _scoped_key_env("OPENAI_API_KEY")
        if not key:
            # The main key only goes to the main endpoint's host.
            main_base = _runtime_main_value("base_url") or _read_model_section("base_url")
            if base_url_hostname(base) and base_url_hostname(base) == base_url_hostname(main_base):
                key = _runtime_main_value("api_key") or _read_model_section("api_key")
        return base, key or "no-key-required", model or "gpt-4o-mini"
    if runtime.get("base_url"):
        key = runtime.get("api_key")
        if not key:
            cfg_base, cfg_key = _resolve_custom_runtime()
            if cfg_base and base_url_hostname(cfg_base) == base_url_hostname(runtime["base_url"]):
                key = cfg_key
        return runtime["base_url"], key or "no-key-required", model or "gpt-4o-mini"
    base, key = _resolve_custom_runtime()
    if not base:
        return None, None, None
    return base, key, model or "gpt-4o-mini"


# Client cache: (async_mode, base_url, api_key digest) -> (client, loop).
_client_cache: Dict[tuple, tuple] = {}
_client_cache_lock = threading.Lock()


def _get_client(base_url: str, api_key: str, async_mode: bool) -> Any:
    """A cached client per endpoint+key; async clients per open event loop."""
    if _aux_probe_active():
        return _AuxProbeClientStub(api_key=api_key, base_url=base_url)
    loop = None
    if async_mode:
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            pass
    key = (async_mode, base_url, hashlib.blake2b(api_key.encode("utf-8"), digest_size=16).digest())
    with _client_cache_lock:
        cached = _client_cache.get(key)
        if cached is not None:
            client, cached_loop = cached
            if not async_mode or (cached_loop is loop and loop is not None and not loop.is_closed()):
                return client
            _close_cached_client(client, close_async=cached_loop is not None and cached_loop.is_closed())
            del _client_cache[key]
    client = _create_openai_client(api_key=api_key, base_url=base_url, async_mode=async_mode)
    with _client_cache_lock:
        if key in _client_cache:
            _close_cached_client(client, close_async=async_mode)
            return _client_cache[key][0]
        _client_cache[key] = (client, loop)
    return client


def resolve_provider_client(
    provider: str,
    model: str = None,
    async_mode: bool = False,
    explicit_base_url: str = None,
    explicit_api_key: str = None,
    api_mode: str = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    is_vision: bool = False,
    task: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Return ``(client, model)`` for the route, or ``(None, None)``."""
    _validate_proxy_env_urls()
    base, key, final_model = _route(provider, model, explicit_base_url, explicit_api_key, main_runtime)
    if not base:
        logger.warning("Auxiliary client: no custom endpoint configured")
        return None, None
    return _get_client(base, key, async_mode), final_model


def get_text_auxiliary_client(
    task: str = "",
    *,
    main_runtime: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Return ``(client, model)`` for a text-only auxiliary task."""
    provider, model, base_url, api_key, _mode = _resolve_task_provider_model(task or None)
    return resolve_provider_client(
        provider, model=model, explicit_base_url=base_url, explicit_api_key=api_key,
        main_runtime=main_runtime, task=task or None,
    )


def resolve_vision_provider_client(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    async_mode: bool = False,
    main_runtime: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[Any], Optional[str]]:
    """Return ``(provider, client, model)`` for vision tasks.

    An ``auxiliary.vision`` endpoint wins; otherwise the main model is used
    unless it is known to be text-only.
    """
    requested, resolved_model, resolved_base_url, resolved_api_key, _mode = _resolve_task_provider_model(
        "vision", provider, model, base_url, api_key
    )
    if not resolved_base_url:
        runtime = _normalize_main_runtime(main_runtime)
        main_provider = str(runtime.get("provider") or _read_main_provider())
        vision_model = resolved_model or str(runtime.get("model") or _read_main_model())
        try:
            from agent.image_routing import _lookup_supports_vision
            from son_of_anton_cli.config import load_config_readonly

            if _lookup_supports_vision(main_provider, vision_model, load_config_readonly()) is False:
                logger.debug("Vision: the main model reports no vision capability")
                return None, None, None
        except Exception:
            pass
    client, final_model = resolve_provider_client(
        requested, resolved_model, async_mode,
        explicit_base_url=resolved_base_url, explicit_api_key=resolved_api_key,
        main_runtime=main_runtime, is_vision=True,
    )
    return ("custom" if resolved_base_url else requested), client, final_model


# ── Client cache teardown ────────────────────────────────────────────────

def _force_close_async_httpx(client: Any) -> None:
    """Mark the httpx AsyncClient inside an AsyncOpenAI client closed so its
    ``__del__`` never schedules ``aclose()`` on a dead event loop."""
    try:
        from httpx._client import ClientState

        inner = getattr(client, "_client", None)
        if inner is not None and not getattr(inner, "is_closed", True):
            inner._state = ClientState.CLOSED
    except Exception:
        pass


def _close_cached_client(client: Any, *, close_async: bool = False) -> None:
    """Close one cached client, awaiting async transports only when safe."""
    close_fn = getattr(client, "close", None)
    if not callable(close_fn):
        _force_close_async_httpx(client)
        return
    try:
        close_result = close_fn()
    except Exception:
        _force_close_async_httpx(client)
        return
    if inspect.isawaitable(close_result):
        if close_async:
            import asyncio

            async def _await_close() -> None:
                try:
                    await close_result
                except Exception:
                    pass
                finally:
                    _force_close_async_httpx(client)

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    asyncio.run(_await_close())
                except Exception:
                    _force_close_async_httpx(client)
            else:
                loop.create_task(_await_close()).add_done_callback(
                    lambda t: t.cancelled() or t.exception()
                )
            return
        try:
            close_result.close()
        except Exception:
            pass
    _force_close_async_httpx(client)


def shutdown_cached_clients() -> None:
    """Close all cached clients; call before the event loop is closed."""
    with _client_cache_lock:
        clients = list(_client_cache.values())
        _client_cache.clear()
    try:
        import asyncio

        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    for client, owner_loop in clients:
        _close_cached_client(
            client,
            close_async=owner_loop is not None and (owner_loop.is_closed() or owner_loop is running_loop),
        )


def cleanup_stale_async_clients() -> None:
    """Close cached async clients whose event loop is closed."""
    with _client_cache_lock:
        stale = [k for k, (_c, loop) in _client_cache.items() if loop is not None and loop.is_closed()]
        clients = [_client_cache.pop(k)[0] for k in stale]
    for client in clients:
        _close_cached_client(client, close_async=True)


def _evict_cached_client_instance(target: Any) -> None:
    """Drop one client (e.g. after a connection error poisoned its transport)."""
    with _client_cache_lock:
        for key, (client, _loop) in list(_client_cache.items()):
            if client is target:
                del _client_cache[key]
    _close_cached_client(target)


# ── Request building ─────────────────────────────────────────────────────

def _build_call_kwargs(
    model: str,
    messages: list,
    *,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    tools: Optional[list] = None,
    timeout: float = 30.0,
    extra_body: Optional[dict] = None,
    reasoning_config: Optional[dict] = None,
    base_url: Optional[str] = None,
    task: Optional[str] = None,
) -> dict:
    """Build kwargs for ``chat.completions.create()``.

    ``max_tokens`` is sent only for hard-capped tasks: an omitted cap means
    the model's own maximum, which is right for open-ended output. A task's
    ``extra_body.reasoning`` (its ``reasoning_effort``) becomes the
    endpoint's top-level ``reasoning_effort`` unless an explicit
    ``reasoning_config`` was passed.
    """
    kwargs: Dict[str, Any] = {"model": model, "messages": messages, "timeout": timeout}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None and str(task or "") in _HARD_CAPPED_TASKS:
        kwargs["max_tokens"] = max_tokens
    if tools:
        seen: set = set()
        deduped: list = []
        for tool in tools:
            name = (tool.get("function") or {}).get("name", "")
            if name and name in seen:
                logger.warning("_build_call_kwargs: duplicate tool name '%s' removed", name)
                continue
            if name:
                seen.add(name)
            deduped.append(tool)
        kwargs["tools"] = deduped
    merged_extra = dict(extra_body or {})
    task_reasoning = merged_extra.pop("reasoning", None)
    if reasoning_config is None and isinstance(task_reasoning, dict):
        reasoning_config = task_reasoning
    reasoning_extra, top_level = custom_reasoning_extras(reasoning_config, model=model, base_url=base_url)
    kwargs.update(top_level)
    merged_extra.update(reasoning_extra)
    if merged_extra:
        kwargs["extra_body"] = merged_extra
    return kwargs


def _validate_llm_response(response: Any, task: Optional[str] = None, base_url: Optional[str] = None) -> Any:
    """Fail fast unless the response has ``.choices[0].message``; record usage."""
    if response is None:
        raise RuntimeError(f"Auxiliary {task or 'call'}: LLM returned None response")
    from agent.aux_accounting import record_aux_usage

    record_aux_usage(response, task, provider="custom", base_url=base_url)
    try:
        choices = response.choices
        if not choices or not hasattr(choices[0], "message"):
            raise AttributeError("missing choices[0].message")
    except (AttributeError, TypeError, IndexError) as exc:
        raise RuntimeError(
            f"Auxiliary {task or 'call'}: LLM returned invalid response "
            f"(type={type(response).__name__}): {str(response)[:120]!r}. "
            f"Expected object with .choices[0].message — check the endpoint."
        ) from exc
    return response


# ── Streamed aggregation for progress-hooked calls ───────────────────────
# With a progress hook installed (compaction), the request is streamed and
# aggregated: the timeout then acts per stream read (a slow but generating
# model is never cut off) and every chunk ticks the hook. A total ceiling
# still bounds a stream that trickles one token per idle window.

_AUX_STREAM_CEILING_FLOOR_SECONDS = 600.0
_AUX_STREAM_CEILING_MULTIPLIER = 4.0


def _aux_stream_total_ceiling(effective_timeout: Optional[float]) -> float:
    try:
        timeout = float(effective_timeout) if effective_timeout is not None else 0.0
    except (TypeError, ValueError):
        timeout = 0.0
    return max(_AUX_STREAM_CEILING_FLOOR_SECONDS, _AUX_STREAM_CEILING_MULTIPLIER * timeout)


class _ChatStreamAccumulator:
    """Reassemble chat.completions chunks into a complete response."""

    def __init__(self, model: str = "", total_ceiling: Optional[float] = None):
        self._started = time.monotonic()
        self._total_ceiling = total_ceiling
        self.content_parts: List[str] = []
        self.reasoning_parts: List[str] = []
        self.tool_calls_acc: Dict[int, Dict[str, Any]] = {}
        self.finish_reason = None
        self.usage = None
        self.resp_id = ""
        self.resp_model = model or ""

    def feed(self, chunk: Any) -> None:
        _notify_aux_progress()
        if self._total_ceiling is not None and (time.monotonic() - self._started) >= self._total_ceiling:
            raise TimeoutError(
                f"Auxiliary streamed call timed out after {self._total_ceiling:.0f}s "
                "total ceiling (stream still open but over budget)"
            )
        self.resp_id = getattr(chunk, "id", None) or self.resp_id
        self.resp_model = getattr(chunk, "model", None) or self.resp_model
        if getattr(chunk, "usage", None):
            self.usage = chunk.usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        choice = choices[0]
        self.finish_reason = getattr(choice, "finish_reason", None) or self.finish_reason
        delta = getattr(choice, "delta", None)
        if delta is None:
            return
        if getattr(delta, "content", None):
            self.content_parts.append(delta.content)
        reasoning_piece = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
        if reasoning_piece and isinstance(reasoning_piece, str):
            self.reasoning_parts.append(reasoning_piece)
        for tc in getattr(delta, "tool_calls", None) or []:
            acc = self.tool_calls_acc.setdefault(
                getattr(tc, "index", 0) or 0, {"id": "", "name": "", "arguments": []}
            )
            if getattr(tc, "id", None):
                acc["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    acc["name"] = fn.name
                if getattr(fn, "arguments", None):
                    acc["arguments"].append(fn.arguments)

    def finish(self) -> Any:
        tool_calls = [
            SimpleNamespace(
                id=acc["id"], type="function",
                function=SimpleNamespace(name=acc["name"], arguments="".join(acc["arguments"])),
            )
            for _idx, acc in sorted(self.tool_calls_acc.items())
        ] or None
        message = SimpleNamespace(
            role="assistant",
            content="".join(self.content_parts),
            tool_calls=tool_calls,
            reasoning="".join(self.reasoning_parts) or None,
        )
        return SimpleNamespace(
            id=self.resp_id, model=self.resp_model, object="chat.completion",
            choices=[SimpleNamespace(index=0, message=message, finish_reason=self.finish_reason or "stop")],
            usage=self.usage,
        )


def _create_with_progress(client: Any, kwargs: Dict[str, Any], task: Optional[str] = None) -> Any:
    """``chat.completions.create()`` that streams and aggregates when a progress hook is active."""
    _notify_aux_progress()
    if not _aux_progress_active():
        return client.chat.completions.create(**kwargs)
    stream_kwargs = {**kwargs, "stream": True, "stream_options": {"include_usage": True}}
    try:
        chunks = client.chat.completions.create(**stream_kwargs)
    except Exception as exc:
        if (
            _is_transient_transport_error(exc)
            or _is_auth_error(exc)
            or _is_rate_limit_error(exc)
            or getattr(exc, "status_code", None) == 402
        ):
            raise
        # Possibly a streaming-specific rejection: retry once non-streaming.
        logger.debug("Auxiliary %s: streamed request failed (%s); retrying non-streaming", task or "call", exc)
        return client.chat.completions.create(**kwargs)
    if hasattr(chunks, "choices"):
        _notify_aux_progress()
        return chunks
    acc = _ChatStreamAccumulator(
        model=str(kwargs.get("model") or ""), total_ceiling=_aux_stream_total_ceiling(kwargs.get("timeout"))
    )
    try:
        for chunk in chunks:
            acc.feed(chunk)
    finally:
        close_fn = getattr(chunks, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
    return acc.finish()


# ── Calls ────────────────────────────────────────────────────────────────

def _without_field(kwargs: dict, field: str) -> Optional[dict]:
    """*kwargs* without *field* (top-level or in ``extra_body``), or None if absent."""
    out = dict(kwargs)
    changed = out.pop(field, None) is not None
    extra = out.get("extra_body")
    if isinstance(extra, dict) and field in extra:
        rest = {k: v for k, v in extra.items() if k != field}
        if rest:
            out["extra_body"] = rest
        else:
            out.pop("extra_body")
        changed = True
    return out if changed else None


def _main_model_fallback(failed_model: Optional[str], task: Optional[str], reason: str):
    """The main session's client+model, unless it is the model that just failed."""
    main_model = _read_main_model()
    if not main_model or (failed_model or "").strip().lower() == main_model.strip().lower():
        return None, None
    try:
        client, model = resolve_provider_client("auto", main_model)
    except Exception:
        return None, None
    if client is not None:
        logger.info(
            "Auxiliary %s: %s — falling back to the main model %s", task or "call", reason, model or main_model
        )
    return client, model


def _fallback_reason(exc: Exception) -> Optional[str]:
    """Why the task's own model cannot serve the request, when that warrants the main model."""
    if _is_connection_error(exc):
        return "connection error"
    if _is_rate_limit_error(exc):
        return "rate limit"
    if _is_model_incompatible_error(exc):
        return "model incompatible with route"
    if _is_invalid_aux_response_error(exc):
        return "invalid provider response"
    return None


def call_llm(
    task: str = None,
    *,
    provider: str = None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    messages: list,
    temperature: Optional[float] = None,
    max_tokens: int = None,
    tools: list = None,
    timeout: float = None,
    extra_body: dict = None,
    reasoning_config: Optional[dict] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    stream: bool = False,
    stream_options: dict = None,
    route_info: Optional[Dict[str, str]] = None,
) -> Any:
    """Run an auxiliary LLM request synchronously.

    With ``stream=True`` the raw SDK stream is returned for the caller to
    consume (no retries or fallback). ``route_info`` (a dict) receives the
    provider/model used.
    """
    main_runtime = _normalize_main_runtime(main_runtime)
    resolved_provider, resolved_model, resolved_base_url, resolved_api_key, _mode = _resolve_task_provider_model(
        task, provider, model, base_url, api_key
    )
    effective_extra_body = _get_task_extra_body(task)
    effective_extra_body.update(extra_body or {})
    if task == "vision":
        _p, client, final_model = resolve_vision_provider_client(
            provider=resolved_provider, model=resolved_model, base_url=resolved_base_url,
            api_key=resolved_api_key, main_runtime=main_runtime,
        )
    else:
        client, final_model = resolve_provider_client(
            resolved_provider, resolved_model, explicit_base_url=resolved_base_url,
            explicit_api_key=resolved_api_key, main_runtime=main_runtime, task=task,
        )
    if client is None:
        raise RuntimeError(
            f"No LLM provider configured for task={task} provider={resolved_provider}. "
            "Declare one in config.toml (the Nix module's settings)."
        )
    if route_info is not None:
        route_info["provider"] = resolved_provider or "auto"
        route_info["model"] = final_model or "default"
    effective_timeout = _effective_aux_timeout(task, timeout)
    client_base = str(getattr(client, "base_url", "") or resolved_base_url or "")
    if task:
        logger.info("Auxiliary %s: using %s (%s) at %s", task, resolved_provider, final_model, client_base)

    def _kwargs(model_name: str) -> dict:
        built = _build_call_kwargs(
            model_name, messages, temperature=temperature, max_tokens=max_tokens, tools=tools,
            timeout=effective_timeout, extra_body=effective_extra_body,
            reasoning_config=reasoning_config, base_url=client_base, task=task,
        )
        if extra_headers:
            built["extra_headers"] = dict(extra_headers)
        return built

    kwargs = _kwargs(final_model)
    if stream:
        kwargs["stream"] = True
        if stream_options:
            kwargs["stream_options"] = stream_options
        return client.chat.completions.create(**kwargs)

    def _send(request_client: Any, request: dict) -> Any:
        return _validate_llm_response(
            _run_protected_sync_provider_call(
                lambda req: _create_with_progress(request_client, req, task), request
            ),
            task, base_url=client_base,
        )

    try:
        try:
            return _send(client, kwargs)
        except Exception as transient_err:
            if not _is_transient_transport_error(transient_err):
                raise
            # A full-budget timeout on compaction falls straight through:
            # another timeout-long retry would double the user-visible stall.
            if task == "compaction" and _is_timeout_error(transient_err):
                raise
            last = transient_err
            for attempt in range(1, _TRANSIENT_RETRIES + 1):
                backoff = min(_TRANSIENT_RETRY_BACKOFF_BASE * (2.0 ** (attempt - 1)), 8.0)
                logger.info(
                    "Auxiliary %s: transient transport error (attempt %d/%d); retrying after %.1fs: %s",
                    task or "call", attempt, _TRANSIENT_RETRIES, backoff, last,
                )
                time.sleep(backoff)
                try:
                    return _send(client, kwargs)
                except Exception as retry_err:
                    if not _is_transient_transport_error(retry_err):
                        raise
                    last = retry_err
            raise last
    except Exception as first_err:
        err = first_err
        for field, rejected in (
            ("temperature", lambda e: _is_unsupported_parameter_error(e, "temperature")),
            ("response_format", _is_structured_output_rejection),
            ("max_tokens", lambda e: "max_tokens" in str(e) or "unsupported_parameter" in str(e)),
        ):
            stripped = _without_field(kwargs, field)
            if stripped is not None and rejected(err):
                logger.info("Auxiliary %s: endpoint rejected %s; retrying once without it", task or "call", field)
                kwargs = stripped
                try:
                    return _send(client, kwargs)
                except Exception as retry_err:
                    err = retry_err
        reason = _fallback_reason(err)
        if reason and resolved_provider != "auto":
            fb_client, fb_model = _main_model_fallback(final_model, task, reason)
            if fb_client is not None:
                if route_info is not None:
                    route_info["model"] = fb_model or "default"
                return _send(fb_client, _kwargs(fb_model))
        if _is_connection_error(err):
            _evict_cached_client_instance(client)
        if err is first_err:
            raise
        raise err


def extract_content_or_reasoning(response) -> str:
    """Content from an LLM response, falling back to its reasoning fields.

    Inline think blocks are stripped from content; when nothing remains,
    ``reasoning`` / ``reasoning_content`` / ``reasoning_details`` are used.
    """
    msg = response.choices[0].message
    content = (msg.content or "").strip()
    if content:
        cleaned = re.sub(
            r"<(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>.*?"
            r"</(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>",
            "", content, flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        if cleaned:
            return cleaned
    parts: list[str] = []
    for field in ("reasoning", "reasoning_content"):
        val = getattr(msg, field, None)
        if val and isinstance(val, str) and val.strip() and val not in parts:
            parts.append(val.strip())
    details = getattr(msg, "reasoning_details", None)
    if details and isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict):
                summary = detail.get("summary") or detail.get("content") or detail.get("text")
                if summary and summary not in parts:
                    parts.append(summary.strip() if isinstance(summary, str) else str(summary))
    return "\n\n".join(parts)


async def async_call_llm(
    task: str = None,
    *,
    provider: str = None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    messages: list,
    temperature: Optional[float] = None,
    max_tokens: int = None,
    tools: list = None,
    timeout: float = None,
    extra_body: dict = None,
    reasoning_config: Optional[dict] = None,
    route_info: Optional[Dict[str, str]] = None,
) -> Any:
    """Asynchronous :func:`call_llm` (transient retries; no field/model fallback)."""
    import asyncio

    main_runtime = _normalize_main_runtime(main_runtime)
    resolved_provider, resolved_model, resolved_base_url, resolved_api_key, _mode = _resolve_task_provider_model(
        task, provider, model, base_url, api_key
    )
    effective_extra_body = _get_task_extra_body(task)
    effective_extra_body.update(extra_body or {})
    if task == "vision":
        _p, client, final_model = resolve_vision_provider_client(
            provider=resolved_provider, model=resolved_model, base_url=resolved_base_url,
            api_key=resolved_api_key, async_mode=True, main_runtime=main_runtime,
        )
    else:
        client, final_model = resolve_provider_client(
            resolved_provider, resolved_model, True, explicit_base_url=resolved_base_url,
            explicit_api_key=resolved_api_key, main_runtime=main_runtime, task=task,
        )
    if client is None:
        raise RuntimeError(
            f"No LLM provider configured for task={task} provider={resolved_provider}. "
            "Declare one in config.toml (the Nix module's settings)."
        )
    if route_info is not None:
        route_info["provider"] = resolved_provider or "auto"
        route_info["model"] = final_model or "default"
    client_base = str(getattr(client, "base_url", "") or resolved_base_url or "")
    kwargs = _build_call_kwargs(
        final_model, messages, temperature=temperature, max_tokens=max_tokens, tools=tools,
        timeout=_effective_aux_timeout(task, timeout), extra_body=effective_extra_body,
        reasoning_config=reasoning_config, base_url=client_base, task=task,
    )
    last: Optional[Exception] = None
    for attempt in range(_TRANSIENT_RETRIES + 1):
        if attempt:
            await asyncio.sleep(min(_TRANSIENT_RETRY_BACKOFF_BASE * (2.0 ** (attempt - 1)), 8.0))
        try:
            return _validate_llm_response(
                await client.chat.completions.create(**kwargs), task, base_url=client_base
            )
        except Exception as exc:
            if not _is_transient_transport_error(exc):
                raise
            last = exc
    raise last
