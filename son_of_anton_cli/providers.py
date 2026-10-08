"""
Single source of truth for provider identity in Son of Anton Agent.

Two data sources, merged at runtime:

1. **Son of Anton overlays** — transport type, auth patterns, aggregator flags,
   base URLs, and env vars for the built-in providers.

2. **User config** (``providers:`` section in config.toml) — user-defined
   endpoints and overrides.  Merged on top of everything else.

Other modules import from this file.  No parallel registries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from utils import base_url_host_matches

logger = logging.getLogger(__name__)


# -- Son of Anton overlay ----------------------------------------------------------
# Additional metadata for the fork's built-in providers.

@dataclass(frozen=True)
class SonOfAntonOverlay:
    """Provider metadata for the fork's built-in providers."""

    transport: str = "openai_chat"        # wire transport key (see TRANSPORT_TO_API_MODE)
    is_aggregator: bool = False
    auth_type: str = "api_key"            # api_key | oauth_device_code | oauth_external | external_process
    extra_env_vars: Tuple[str, ...] = ()  # additional env vars for this provider
    base_url_override: str = ""           # built-in base URL
    base_url_env_var: str = ""            # env var for user-custom base URL
    keyless: bool = False                 # served anonymously — no credential exists to configure


# The fork's provider surface. Local / self-hosted endpoints (llama-swap,
# ollama, vllm, ...) are resolved through config.toml custom_providers — the
# ``custom`` plugin profile in plugins/model-providers/custom/ — not through
# overlays here.
SON_OF_ANTON_OVERLAYS: Dict[str, SonOfAntonOverlay] = {
    "openai-api": SonOfAntonOverlay(
        base_url_override="https://api.openai.com/v1",
        base_url_env_var="OPENAI_BASE_URL",
    ),
}


# -- Resolved provider -------------------------------------------------------
# The merged result of overlay + user config.

@dataclass
class ProviderDef:
    """Complete provider definition — merged from all sources."""

    id: str
    name: str
    transport: str                        # wire transport key (see TRANSPORT_TO_API_MODE)
    api_key_env_vars: Tuple[str, ...]     # all env vars to check for API key
    base_url: str = ""
    base_url_env_var: str = ""
    is_aggregator: bool = False
    auth_type: str = "api_key"
    doc: str = ""
    source: str = ""                      # "son-of-anton", "user-config", "plugin-profile"


# -- Aliases ------------------------------------------------------------------
# Maps human-friendly / legacy names to canonical provider IDs.

ALIASES: Dict[str, str] = {
    # openai
    "openai": "openai-api",     # bare "openai" → the direct OpenAI API

    # Local server aliases → virtual "local" concept (resolved via user config)
    "lmstudio": "custom",
    "lm-studio": "custom",
    "lm_studio": "custom",
    "vllm": "local",
    "llamacpp": "local",
    "llama.cpp": "local",
    "llama-cpp": "local",
}


# -- Display labels -----------------------------------------------------------
# Built dynamically from overlays + plugin profiles.  Fallback for providers
# not in the catalog.

_LABEL_OVERRIDES: Dict[str, str] = {
    "local": "Local endpoint",
}


# -- Transport → API mode mapping ---------------------------------------------

TRANSPORT_TO_API_MODE: Dict[str, str] = {
    "openai_chat": "chat_completions",
}


# -- Helper functions ---------------------------------------------------------

def normalize_provider(name: str) -> str:
    """Resolve aliases and normalise casing to a canonical provider id.

    Returns the canonical id string.  Does *not* validate that the id
    corresponds to a known provider.
    """
    key = name.strip().lower()
    return ALIASES.get(key, key)


def get_provider(name: str, *, allow_network: bool = True) -> Optional[ProviderDef]:
    """Look up a built-in provider by id or alias.

    Resolution order:
      1. Son of Anton overlays
      2. Plugin-registered provider profiles (``plugins/model-providers/<name>/``)

    User-defined providers from config.toml (``providers:`` / ``custom_providers:``)
    are resolved by :func:`resolve_provider_full`, which layers ``resolve_user_provider``
    and ``resolve_custom_provider`` on top of this function. Callers that need
    user-config support should use ``resolve_provider_full`` instead.

    Returns a fully-resolved ProviderDef or None.
    """
    canonical = normalize_provider(name)

    overlay = SON_OF_ANTON_OVERLAYS.get(canonical)

    if overlay is not None:
        # Overlay-only provider
        return ProviderDef(
            id=canonical,
            name=_LABEL_OVERRIDES.get(canonical, canonical),
            transport=overlay.transport,
            api_key_env_vars=overlay.extra_env_vars,
            base_url=overlay.base_url_override,
            base_url_env_var=overlay.base_url_env_var,
            is_aggregator=overlay.is_aggregator,
            auth_type=overlay.auth_type,
            source="son-of-anton",
        )

    # Plugin-registered provider profiles (plugins/model-providers/<name>/).
    # Providers that ship only as plugin profiles (e.g. commandcode,
    # tencent-tokenhub) are absent from SON_OF_ANTON_OVERLAYS, so
    # without this fallback they resolve as "Unknown provider" in /model,
    # --provider, and the model-switch path even though the picker lists them
    # (CANONICAL_PROVIDERS auto-extends from the same plugin registry).
    try:
        from providers import get_provider_profile as _profile

        _prof = _profile(canonical)
        # Only profiles with a concrete endpoint resolve here. Placeholder
        # profiles like ``custom`` (aliases: ollama/local/vllm) ship with an
        # empty base_url and are completed by config.toml custom_providers —
        # resolving them here would preempt resolve_provider_full's
        # custom-provider step and collapse keyed IDs
        # (``custom:local-...``) back to a bare, endpoint-less ``custom``.
        if _prof is not None and (_prof.base_url or "").strip():
            _api_mode_to_transport = {v: k for k, v in TRANSPORT_TO_API_MODE.items()}
            _transport = _api_mode_to_transport.get(_prof.api_mode, "openai_chat")
            return ProviderDef(
                id=canonical,
                name=_prof.display_name or _prof.name or canonical,
                transport=_transport,
                api_key_env_vars=tuple(_prof.env_vars or ()),
                base_url=_prof.base_url or "",
                auth_type=_prof.auth_type or "api_key",
                source="plugin-profile",
            )
    except Exception:
        pass

    return None


def get_label(provider_id: str) -> str:
    """Get a human-readable display name for a provider."""
    canonical = normalize_provider(provider_id)

    # Check label overrides first
    if canonical in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[canonical]

    # Resolve the provider name
    pdef = get_provider(canonical)
    if pdef:
        return pdef.name

    return canonical


def is_aggregator(provider: str) -> bool:
    """Return True when the provider is a multi-model aggregator."""
    provider_norm = normalize_provider(provider or "")
    if provider_norm.startswith("custom:"):
        return True
    pdef = get_provider(provider_norm)
    return pdef.is_aggregator if pdef else False


def is_routing_aggregator(provider: str) -> bool:
    """Return True only for TRUE routing aggregators (named ``custom:*``
    proxies) — those that route bare/vendor-slugged model names to *other*
    providers' endpoints.
    """
    return is_aggregator(normalize_provider(provider or ""))


def is_official_openai_host(base_url: str) -> bool:
    """True when *base_url* points at OpenAI's official API host family.

    Matches the canonical host (``api.openai.com``) and OpenAI's documented
    data-residency / regional hosts (``us.api.openai.com``,
    ``eu.api.openai.com``, and any future ``<region>.api.openai.com``) —
    those serve the same API surface with the same transport requirements
    and the same access-scoped ``/v1/models`` listing.

    Hostname-parsed matching only — never substring — so lookalike hosts
    (``api.openai.com.attacker.test``) and path-segment spoofs
    (``proxy.test/api.openai.com/v1``) are rejected. A genuine
    ``*.api.openai.com`` subdomain requires control of openai.com DNS, so
    the dot-suffix match does not reopen the #32243 spoofing hole.
    Delegates to ``utils.base_url_host_matches``, which owns the
    exact-or-dot-suffix hostname contract (userinfo/port stripped,
    lowercased, trailing dot removed) — one implementation, not two.
    """
    return base_url_host_matches(base_url, "api.openai.com")


def determine_api_mode(provider: str, base_url: str = "", model: str = "") -> str:
    """Determine the API mode (wire protocol) for a provider/endpoint.

    Resolution: known provider → transport → TRANSPORT_TO_API_MODE, else
    the 'chat_completions' default. The only wire the fork speaks today.

    *model* and *base_url* are accepted for call-site compatibility.
    """
    pdef = get_provider(provider)
    if pdef is not None:
        return TRANSPORT_TO_API_MODE.get(pdef.transport, "chat_completions")

    return "chat_completions"


# -- Provider from user config ------------------------------------------------

def resolve_user_provider(name: str, user_config: Dict[str, Any]) -> Optional[ProviderDef]:
    """Resolve a provider from the user's config.toml ``providers:`` section.

    Args:
        name: Provider name as given by the user.
        user_config: The ``providers:`` dict from config.toml.

    Returns:
        ProviderDef if found, else None.
    """
    if not user_config or not isinstance(user_config, dict):
        return None

    entry = user_config.get(name)
    if not isinstance(entry, dict):
        return None

    # Extract fields
    display_name = entry.get("name", "") or name
    api_url = entry.get("api", "") or entry.get("url", "") or entry.get("base_url", "") or ""
    key_env = entry.get("key_env") or entry.get("api_key_env") or ""
    transport = entry.get("transport", "openai_chat") or "openai_chat"

    env_vars: List[str] = []
    if key_env:
        env_vars.append(key_env)

    return ProviderDef(
        id=name,
        name=display_name,
        transport=transport,
        api_key_env_vars=tuple(env_vars),
        base_url=api_url,
        is_aggregator=False,
        auth_type="api_key",
        source="user-config",
    )


def custom_provider_slug(display_name: str, provider_key: str = "") -> str:
    """Build the stable ``custom:`` identity for a configured provider.

    Keyed ``providers:`` entries keep their config key as the durable
    identity even when their display name changes. Legacy
    ``custom_providers:`` entries have no key, so their normalized display
    name remains the identity.
    """
    identity = str(provider_key or "").strip() or str(display_name or "").strip()
    normalized = identity.lower().replace(" ", "-")
    return normalized if normalized.startswith("custom:") else f"custom:{normalized}"


def custom_provider_aliases(
    display_name: str,
    provider_key: str = "",
) -> frozenset[str]:
    """Return every current and legacy identity accepted for one endpoint."""
    aliases: set[str] = set()
    for value in (display_name, provider_key):
        raw = str(value or "").strip().lower()
        if not raw:
            continue
        normalized = raw.replace(" ", "-")
        aliases.update({raw, normalized, custom_provider_slug(normalized)})
        if normalized.startswith("custom:"):
            suffix = normalized.split(":", 1)[1]
            if suffix:
                aliases.update({suffix, f"custom:{normalized}"})
    return frozenset(aliases)


def resolve_custom_provider(
    name: str,
    custom_providers: Optional[List[Dict[str, Any]]],
) -> Optional[ProviderDef]:
    """Resolve a provider from the user's config.toml ``custom_providers`` list."""
    if not custom_providers or not isinstance(custom_providers, list):
        return None

    requested = (name or "").strip().lower()
    if not requested:
        return None

    # If the stored provider is the bare string "custom" (corrupt state
    # from a prior model-switch bug), fall back to the first custom
    # provider entry so existing configs self-heal.  (GH #17478)
    bare_custom_fallback = requested == "custom"
    first_valid: Optional[Tuple[str, str, Tuple[str, ...], str]] = None

    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue

        display_name = (entry.get("name") or "").strip()
        api_url = (
            entry.get("base_url", "")
            or entry.get("url", "")
            or entry.get("api", "")
            or ""
        ).strip()
        if not display_name or not api_url:
            continue

        key_env = (entry.get("key_env") or "").strip()
        provider_key = (entry.get("provider_key") or "").strip()
        env_vars: List[str] = []
        if key_env:
            env_vars.append(key_env)

        # Stash the first valid entry for bare-"custom" fallback
        if first_valid is None:
            first_valid = (
                display_name,
                api_url,
                tuple(env_vars),
                custom_provider_slug(display_name, provider_key),
            )

        slug = custom_provider_slug(display_name, provider_key)
        if requested not in custom_provider_aliases(display_name, provider_key):
            continue

        return ProviderDef(
            id=slug,
            name=display_name,
            transport="openai_chat",
            api_key_env_vars=tuple(env_vars),
            base_url=api_url,
            is_aggregator=False,
            auth_type="api_key",
            source="user-config",
        )

    # Self-heal: bare "custom" matched nothing — return first valid entry
    if bare_custom_fallback and first_valid:
        dname, aurl, denv, slug = first_valid
        return ProviderDef(
            id=slug,
            name=dname,
            transport="openai_chat",
            api_key_env_vars=denv,
            base_url=aurl,
            is_aggregator=False,
            auth_type="api_key",
            source="user-config",
        )

    return None


def resolve_provider_full(
    name: str,
    user_providers: Optional[Dict[str, Any]] = None,
    custom_providers: Optional[List[Dict[str, Any]]] = None,
) -> Optional[ProviderDef]:
    """Full resolution chain: built-in → user config.

    This is the main entry point for --provider flag resolution.

    Args:
        name: Provider name or alias.
        user_providers: The ``providers:`` dict from config.toml (optional).
        custom_providers: The ``custom_providers:`` list from config.toml (optional).

    Returns:
        ProviderDef if found, else None.
    """
    canonical = normalize_provider(name)
    raw = name.strip().lower()

    # 0. User-defined config providers win over the built-in alias table.
    #    A user who declares ``providers.<name>`` in config.toml has stated
    #    explicit intent for that name — it must not be hijacked by a legacy
    #    vendor alias. Resolve the raw name against user config FIRST; only
    #    the raw (pre-alias) name is tried here, canonical/alias resolution
    #    still happens below.
    if user_providers:
        user_pdef = resolve_user_provider(raw, user_providers)
        if user_pdef is not None:
            return user_pdef

    # 0.5 Exact Son of Anton provider IDs must win over LOSSY alias collapsing.
    # A collapse is lossy only when MULTIPLE distinct registry providers
    # normalize to the same canonical name — resolving through the alias
    # would then lose which one the caller meant. Single-entry rewrites
    # (e.g. "copilot" → "github-copilot") are correct routing and must keep
    # resolving through the built-in chain below so overlay transports apply.
    if canonical != raw:
        try:
            from son_of_anton_cli.auth import PROVIDER_REGISTRY as _AUTH_PROVIDER_REGISTRY
            _pcfg = _AUTH_PROVIDER_REGISTRY.get(raw)
            if _pcfg is not None:
                _collapsed_siblings = [
                    _rid
                    for _rid in _AUTH_PROVIDER_REGISTRY
                    if normalize_provider(_rid) == canonical
                ]
                if len(_collapsed_siblings) > 1:
                    return ProviderDef(
                        id=_pcfg.id,
                        name=_pcfg.name,
                        transport="openai_chat",
                        api_key_env_vars=tuple(_pcfg.api_key_env_vars or ()),
                        base_url=_pcfg.inference_base_url or "",
                        source="son-of-anton-auth-registry",
                    )
        except Exception:
            pass

    # 1. Built-in (overlays + plugin profiles)
    pdef = get_provider(canonical)
    if pdef is not None:
        return pdef

    # 2. User-defined providers from config
    if user_providers:
        # Try canonical name
        user_pdef = resolve_user_provider(canonical, user_providers)
        if user_pdef is not None:
            return user_pdef
        # Try original name (in case alias didn't match)
        user_pdef = resolve_user_provider(raw, user_providers)
        if user_pdef is not None:
            return user_pdef

    # 2b. Saved custom providers from config
    custom_pdef = resolve_custom_provider(name, custom_providers)
    if custom_pdef is not None:
        return custom_pdef

    return None
