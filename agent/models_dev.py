"""Model metadata for the agent — driven by ``config.toml``.

Son of Anton talks to a single OpenAI-compatible endpoint. Per-model
metadata (context window, max output tokens, capabilities) comes from the
``model_overrides`` config section, not from any vendor catalog.

Canonical override schema (the ONLY key space consumers accept)::

    model_overrides:
      <provider>:
        <model_id>:
          context_window: 128000
          max_output_tokens: 8192
          supports_tools: true
          supports_vision: false
          supports_reasoning: false
          model_family: "..."
      _default:            # applies to any provider/model not set above
        context_window: 128000

Semantics:
  * Explicit ``<provider>.<model_id>`` entries win for the fields they set.
  * ``_default`` entries (per-provider or global) fill gaps only.
  * A model with no override starts from safe defaults (200K context,
    tools on, vision/reasoning off) and the override patches its fields.

The historical models.dev vendor catalog (network fetch, ETag conditional
GET, disk cache, the 109-provider ``PROVIDER_TO_MODELS_DEV`` mapping,
pricing, and the model-listing queries) has been removed: it had no place
in a single-standard-endpoint deployment. The lookups below resolve
metadata from ``model_overrides`` and safe defaults only.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Metadata dataclasses                                                        #
# --------------------------------------------------------------------------- #

@dataclass
class ModelInfo:
    """Metadata for a single model (config-driven; no vendor catalog)."""

    id: str
    name: str = ""
    family: str = ""
    provider_id: str = ""

    reasoning: bool = False
    tool_call: bool = True
    attachment: bool = False
    temperature: bool = False
    structured_output: bool = False
    open_weights: bool = False

    input_modalities: tuple = ()
    output_modalities: tuple = ()

    context_window: int = 0
    max_output: int = 0
    max_input: Optional[int] = None

    cost_input: float = 0.0
    cost_output: float = 0.0
    cost_cache_read: Optional[float] = None
    cost_cache_write: Optional[float] = None

    knowledge_cutoff: str = ""
    release_date: str = ""
    status: str = ""
    interleaved: Any = False

    def has_cost_data(self) -> bool:
        return self.cost_input > 0 or self.cost_output > 0

    def supports_vision(self) -> bool:
        return self.attachment or "image" in self.input_modalities

    def supports_pdf(self) -> bool:
        return "pdf" in self.input_modalities

    def supports_audio_input(self) -> bool:
        return "audio" in self.input_modalities

    def format_cost(self) -> str:
        """Human-readable cost string, e.g. '$3.00/M in, $15.00/M out'."""
        if not self.has_cost_data():
            return "unknown"
        parts = [f"${self.cost_input:.2f}/M in", f"${self.cost_output:.2f}/M out"]
        if self.cost_cache_read is not None:
            parts.append(f"cache read ${self.cost_cache_read:.2f}/M")
        return ", ".join(parts)

    def format_capabilities(self) -> str:
        """Human-readable capabilities, e.g. 'reasoning, tools, vision'."""
        caps = []
        if self.reasoning:
            caps.append("reasoning")
        if self.tool_call:
            caps.append("tools")
        if self.supports_vision():
            caps.append("vision")
        if self.supports_pdf():
            caps.append("PDF")
        if self.supports_audio_input():
            caps.append("audio")
        if self.structured_output:
            caps.append("structured output")
        if self.open_weights:
            caps.append("open weights")
        return ", ".join(caps) if caps else "basic"


@dataclass
class ModelCapabilities:
    """Structured capability metadata for a model (config-driven)."""

    supports_tools: bool = True
    supports_vision: bool = False
    supports_reasoning: bool = False
    context_window: int = 200000
    max_output_tokens: int = 8192
    model_family: str = ""


# --------------------------------------------------------------------------- #
# Per-model metadata overrides (config.toml -> model_overrides)               #
# --------------------------------------------------------------------------- #
#
# Canonical override schema (the ONLY key space consumers accept):
#   context_window, max_output_tokens, supports_tools, supports_vision,
#   supports_reasoning, model_family
#
# Resolution semantics:
#   1. ``model_overrides.<provider>.<model_id>`` — explicit override.
#      Wins for the fields it sets (partial patch).
#   2. ``model_overrides.<provider>._default`` / ``model_overrides._default``
#      — FILL-GAP defaults. Apply only to models not explicitly overridden.
#
# Provider keys are the provider id as used in config.toml. Model ids match
# exactly, then case-insensitively.

_OVERRIDE_WARNED_KEYS: set = set()


def _load_model_overrides() -> Dict[str, Any]:
    """Load the ``model_overrides`` config section.

    No local memoization on purpose: ``load_config_readonly()`` is already
    (mtime, size)-cached upstream, and an ``id(cfg)``-keyed layer here can
    serve stale overrides after a config reload. Returns empty dict on any
    failure.
    """
    try:
        from son_of_anton_cli.config import cfg_get, load_config_readonly
        raw = cfg_get(load_config_readonly(), "model_overrides", default={})
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _provider_override_section(provider: str) -> Optional[Dict[str, Any]]:
    """Return the override section for *provider*, or None.

    Keyed directly by the provider id the caller passes. The vendor
    catalog's id-mapping table is gone, so there is a single id space —
    the provider key written in config.toml.
    """
    overrides = _load_model_overrides()
    if not overrides:
        return None
    provider_key = (provider or "").strip()
    if not provider_key:
        return None
    section = overrides.get(provider_key)
    if isinstance(section, dict):
        return section
    return None


def _explicit_model_override(provider: str, model: str) -> Optional[Dict[str, Any]]:
    """Return the explicit per-provider+model override dict, or None.

    Model ids match exactly first, then case-insensitively (skipping the
    ``_default`` sentinel).
    """
    model_key = (model or "").strip()
    if not model_key:
        return None
    section = _provider_override_section(provider)
    if section is None:
        return None

    entry = section.get(model_key)
    if isinstance(entry, dict):
        return entry

    model_lower = model_key.lower()
    for mid, mdata in section.items():
        if mid == "_default":
            continue
        if mid.lower() == model_lower and isinstance(mdata, dict):
            return mdata
    return None


def _default_model_override(provider: str) -> Optional[Dict[str, Any]]:
    """Return the fill-gap ``_default`` override for *provider*, or None.

    Checks the per-provider ``_default`` first, then the global one.
    """
    section = _provider_override_section(provider)
    if section is not None:
        default = section.get("_default")
        if isinstance(default, dict):
            return default
    overrides = _load_model_overrides()
    global_default = overrides.get("_default")
    if isinstance(global_default, dict):
        return global_default
    return None


def _override_for(
    provider: str, model: str, *, catalog_hit: bool
) -> Optional[Dict[str, Any]]:
    """Select the override dict for a lookup, honoring fill-gap semantics.

    Explicit per-provider+model overrides always apply. ``_default``
    entries apply only when there is no explicit entry for the model.
    (With the vendor catalog gone, *catalog_hit* is always False at the
    call sites below, so a ``_default`` fills any gap.)
    """
    explicit = _explicit_model_override(provider, model)
    if explicit is not None:
        return explicit
    if catalog_hit:
        return None
    return _default_model_override(provider)


def _override_int(override: Dict[str, Any], key: str) -> Optional[int]:
    """Coerce an override field to a positive int, warning once on garbage."""
    raw = override.get(key)
    if raw is None:
        return None
    try:
        value = int(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    warn_key = (key, repr(raw))
    if warn_key not in _OVERRIDE_WARNED_KEYS:
        _OVERRIDE_WARNED_KEYS.add(warn_key)
        logger.warning(
            "model_overrides: ignoring invalid %s value %r "
            "(expected a positive integer)", key, raw,
        )
    return None


def _override_context_window(provider: str, model: str) -> Optional[int]:
    """Return the EXPLICITLY overridden context_window, or None.

    Explicit-only on purpose: this runs early in the resolution chain
    (agent/model_metadata.py), where a ``_default`` must not preempt more
    specific sources. Fill-gap defaults are applied later by
    ``lookup_models_dev_context``.
    """
    ov = _explicit_model_override(provider, model)
    if ov is None:
        return None
    return _override_int(ov, "context_window")


def _default_override_context(provider: str) -> Optional[int]:
    """Fill-gap context from a ``_default`` override."""
    default = _default_model_override(provider)
    if default is None:
        return None
    return _override_int(default, "context_window")


# --------------------------------------------------------------------------- #
# Config-driven metadata lookups                                              #
# --------------------------------------------------------------------------- #

def lookup_models_dev_context(
    provider: str, model: str, *, allow_network: bool = False
) -> Optional[int]:
    """Context window for a provider+model, from ``model_overrides``.

    An explicit override wins; a ``_default`` entry fills the gap. Returns
    None when no override provides a context window. ``allow_network`` is
    accepted for API compatibility — no network is ever performed.
    """
    override_ctx = _override_context_window(provider, model)
    if override_ctx is not None:
        return override_ctx
    return _default_override_context(provider)


def get_model_capabilities(
    provider: str, model: str, *, allow_network: bool = False
) -> Optional[ModelCapabilities]:
    """Capability metadata from ``model_overrides`` + safe defaults.

    A model with no override returns None (the caller falls back to its
    own defaults). When an override exists, the unspecified fields start
    from safe defaults (200K context, tools on, vision/reasoning off) and
    the override patches the fields it sets. ``allow_network`` is accepted
    for API compatibility — no network is ever performed.
    """
    override = _override_for(provider, model, catalog_hit=False)
    if override is None:
        return None

    supports_tools = True
    supports_vision = False
    supports_reasoning = False
    context_window = 200000
    max_output_tokens = 8192
    model_family = ""
    if "supports_tools" in override:
        supports_tools = bool(override["supports_tools"])
    if "supports_vision" in override:
        supports_vision = bool(override["supports_vision"])
    if "supports_reasoning" in override:
        supports_reasoning = bool(override["supports_reasoning"])
    ctx_ov = _override_int(override, "context_window")
    if ctx_ov is not None:
        context_window = ctx_ov
    out_ov = _override_int(override, "max_output_tokens")
    if out_ov is not None:
        max_output_tokens = out_ov
    if "model_family" in override:
        model_family = str(override["model_family"] or "")

    return ModelCapabilities(
        supports_tools=supports_tools,
        supports_vision=supports_vision,
        supports_reasoning=supports_reasoning,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        model_family=model_family,
    )


def get_model_info(
    provider_id: str, model_id: str, *, allow_network: bool = False
) -> Optional[ModelInfo]:
    """Model metadata from ``model_overrides``; None when no override.

    Seeds safe defaults (200K context, tools on) so the two unknown-model
    paths agree; the override patches the fields it sets. ``allow_network``
    is accepted for API compatibility — no network is ever performed.
    """
    override = _override_for(provider_id, model_id, catalog_hit=False)
    if override is None:
        return None

    info = ModelInfo(
        id=model_id,
        name=model_id,
        provider_id=provider_id,
        context_window=200000,
        max_output=8192,
        tool_call=True,
    )
    if "supports_tools" in override:
        info.tool_call = bool(override["supports_tools"])
    if "supports_reasoning" in override:
        info.reasoning = bool(override["supports_reasoning"])
    if "supports_vision" in override:
        info.attachment = bool(override["supports_vision"])
        info.input_modalities = ("text", "image") if info.attachment else ("text",)
    ctx_ov = _override_int(override, "context_window")
    if ctx_ov is not None:
        info.context_window = ctx_ov
    out_ov = _override_int(override, "max_output_tokens")
    if out_ov is not None:
        info.max_output = out_ov
    if "model_family" in override:
        info.family = str(override["model_family"] or "")
    return info


