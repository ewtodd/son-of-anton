"""Runtime resolution of the custom OpenAI-compatible endpoint.

``custom`` is the only provider: an endpoint declared under
``custom_providers.<name>`` (``base_url``, ``key_env`` or ``api_key``) in
config.toml. CLI, gateway, cron, delegation and the auxiliary client all
resolve it here.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agent.secret_scope import get_secret as _get_secret
from son_of_anton_cli.config import get_compatible_custom_providers, load_config

logger = logging.getLogger(__name__)

_PLACEHOLDER_SECRET_VALUES = {
    "*", "**", "***", "changeme", "your_api_key", "your_api_key_here",
    "your-api-key", "placeholder", "example", "dummy", "null", "none",
}


class AuthError(RuntimeError):
    """The endpoint's credentials or configuration are missing."""

    def __init__(self, message: str, *, provider: str = "", code: Optional[str] = None):
        super().__init__(message)
        self.provider = provider
        self.code = code


def has_usable_secret(value: Any, *, min_length: int = 4) -> bool:
    """True when a configured secret looks usable, not empty/placeholder."""
    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    return len(cleaned) >= min_length and cleaned.lower() not in _PLACEHOLDER_SECRET_VALUES


def format_runtime_provider_error(error: Exception) -> str:
    return str(error)


def _getenv(name: str) -> str:
    return (_get_secret(name, "") or "").strip() if name else ""


def _get_model_config() -> Dict[str, Any]:
    """The ``model`` config section, with ``model.model`` accepted for ``default``."""
    model_cfg = load_config().get("model")
    if isinstance(model_cfg, dict):
        cfg = dict(model_cfg)
        if not cfg.get("default") and cfg.get("model"):
            cfg["default"] = cfg["model"]
        return cfg
    if isinstance(model_cfg, str) and model_cfg.strip():
        return {"default": model_cfg.strip()}
    return {}


def resolve_requested_provider(requested: Optional[str] = None) -> str:
    """The provider asked for: explicit argument, then ``model.provider``, else ``auto``."""
    if requested and requested.strip():
        return requested.strip().lower()
    cfg_provider = _get_model_config().get("provider")
    if isinstance(cfg_provider, str) and cfg_provider.strip():
        return cfg_provider.strip().lower()
    return "auto"


def _entry_slug(entry: Dict[str, Any]) -> str:
    identity = str(entry.get("provider_key") or entry.get("name") or "").strip().lower().replace(" ", "-")
    return identity if identity.startswith("custom:") else f"custom:{identity}"


def _entry_aliases(entry: Dict[str, Any]) -> set:
    aliases: set = set()
    for value in (entry.get("name"), entry.get("provider_key")):
        raw = str(value or "").strip().lower()
        if raw:
            normalized = raw.replace(" ", "-")
            aliases.update({raw, normalized, f"custom:{normalized}"})
    return aliases


def _custom_entries() -> list:
    try:
        return [e for e in get_compatible_custom_providers(load_config()) if isinstance(e, dict)]
    except Exception:
        return []


def _get_named_custom_provider(requested_provider: str) -> Optional[Dict[str, Any]]:
    """The ``custom_providers`` entry *requested_provider* names (``custom``,
    ``custom:<name>`` or ``<name>``), with its key resolved; None when none."""
    requested = (requested_provider or "").strip().lower().replace(" ", "-")
    if not requested or requested == "auto":
        return None
    for entry in _custom_entries():
        base_url = entry.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip() or requested not in _entry_aliases(entry):
            continue
        result = {"name": str(entry.get("name") or "").strip(), "base_url": base_url.strip()}
        api_key = str(entry.get("api_key", "") or "").strip()
        key_env = str(entry.get("key_env", "") or entry.get("api_key_env", "") or "").strip()
        if key_env:
            result["key_env"] = key_env
            api_key = api_key or _getenv(key_env)
        result["api_key"] = api_key
        if entry.get("provider_key"):
            result["provider_key"] = entry["provider_key"]
        if entry.get("model"):
            result["model"] = str(entry["model"]).strip()
        return result
    return None


def has_named_custom_provider(requested_provider: str) -> bool:
    try:
        return _get_named_custom_provider(requested_provider) is not None
    except Exception:
        return False


def canonical_custom_identity(
    *,
    base_url: Optional[str] = None,
    config_provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[str]:
    """The durable ``custom:<name>`` identity for a bare ``custom`` session
    provider: the entry owning *base_url*, else the one serving *model*."""
    entries = _custom_entries()
    target = str(base_url or "").strip().rstrip("/").lower()
    if target:
        for entry in entries:
            if str(entry.get("base_url") or "").strip().rstrip("/").lower() == target:
                return _entry_slug(entry)
    wanted = str(model or "").strip().lower()
    if wanted:
        for entry in entries:
            models = entry.get("models")
            served = {str(m).strip().lower() for m in models} if isinstance(models, (dict, list)) else set()
            served.update(str(entry.get(k) or "").strip().lower() for k in ("model", "default_model"))
            if wanted in served:
                return _entry_slug(entry)
    return None


def resolve_runtime_provider(
    *,
    requested: Optional[str] = None,
    explicit_api_key: Optional[str] = None,
    explicit_base_url: Optional[str] = None,
    target_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve the endpoint and credential for agent execution.

    An explicit base_url wins; otherwise the named ``custom_providers`` entry
    (for ``auto``, the first entry), else ``model.base_url``.
    """
    requested_provider = resolve_requested_provider(requested)
    entry = _get_named_custom_provider(requested_provider)
    if entry is None and requested_provider in ("auto", "custom"):
        entries = _custom_entries()
        if entries:
            entry = _get_named_custom_provider(_entry_slug(entries[0]))
    model_cfg = _get_model_config()
    explicit_base_url = str(explicit_base_url or "").strip().rstrip("/")
    explicit_api_key = str(explicit_api_key or "").strip()
    if explicit_base_url:
        base_url, api_key, source = explicit_base_url, explicit_api_key, "explicit"
    elif entry is not None:
        base_url = entry["base_url"].rstrip("/")
        api_key = explicit_api_key or entry.get("api_key", "")
        source = f"custom_provider:{entry.get('name') or requested_provider}"
    else:
        base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
        api_key = explicit_api_key or str(model_cfg.get("api_key") or "").strip()
        source = "explicit" if explicit_api_key else "env/config"
    if not base_url:
        raise AuthError(
            "No LLM endpoint configured. Declare it under custom_providers in "
            "config.toml (the Nix module's settings).",
            provider=requested_provider,
            code="missing_endpoint",
        )
    return {
        "provider": "custom",
        "api_mode": "chat_completions",
        "base_url": base_url,
        "api_key": api_key if has_usable_secret(api_key) else "no-key-required",
        "source": source,
        "requested_provider": requested_provider,
    }
