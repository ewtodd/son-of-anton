"""Reasoning effort on the wire for the custom OpenAI-compatible endpoint.

The internal effort ladder (``son_of_anton_constants.VALID_REASONING_EFFORTS``
plus ``none``) is wider than what any one endpoint accepts. A route's
vocabulary comes from ``custom_providers.<name>.models.<model>.reasoning_efforts``
or, when undeclared, from what the endpoint's ``/models`` lists for the model
(maitred lists ``reasoning_efforts`` and ``reasoning_effort_aliases``).
:func:`clamp_effort` maps a requested level onto it: a supported level is kept,
otherwise the nearest weaker one (never escalate cost), and only when nothing
weaker exists the weakest supported level.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

# Canonical low→high ordering used for nearest-level clamping.
EFFORT_LADDER: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
)

#: The widest OpenAI-compatible wire vocabulary (``ultra`` is internal only).
OPENAI_COMPAT_WIRE_EFFORTS: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)


def supported_efforts_for(
    model: Optional[str],
    *,
    provider: str = "",
    base_url: str = "",
    custom_providers: Optional[list] = None,
    config: Optional[dict] = None,
) -> tuple[str, ...]:
    """Effort levels to offer for the active route, in declared order.

    The route's declared set wins, then the set the endpoint's ``/models``
    listed on the agent's last probe (cache only, no request), then the
    widest OpenAI-compatible vocabulary.
    """
    if model:
        try:
            from son_of_anton_cli.config import get_custom_provider_reasoning_decl

            decl = get_custom_provider_reasoning_decl(
                str(model),
                base_url or None,
                custom_providers=custom_providers,
                config=config,
            ) or {}
        except Exception:
            decl = {}
        efforts = decl.get("efforts")
        if efforts:
            return tuple(str(level) for level in efforts)
        if base_url:
            try:
                from agent.model_metadata import get_endpoint_reasoning_decl

                efforts = get_endpoint_reasoning_decl(str(model), base_url).get("efforts")
            except Exception:
                efforts = None
            if efforts:
                return tuple(efforts)
    return OPENAI_COMPAT_WIRE_EFFORTS


def clamp_effort(
    effort: Optional[str],
    supported: Optional[Sequence[str]],
    overrides: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Clamp a requested reasoning effort onto a wire's supported levels.

    ``overrides`` (the endpoint's listed aliases) is consulted first. A level
    that is supported, or not a ladder level at all, or a route whose set is
    unknown passes through unchanged. Otherwise the nearest weaker supported
    level is returned; when nothing weaker exists, the weakest one.
    """
    requested = str(effort or "").strip().lower()
    if not requested or not supported:
        return effort
    supported_norm = [
        str(level).strip().lower()
        for level in supported
        if str(level).strip().lower() in EFFORT_LADDER
    ]
    if not supported_norm or requested in supported_norm:
        return effort
    if overrides:
        mapped = overrides.get(requested)
        if mapped in supported_norm:
            return mapped
    if requested not in EFFORT_LADDER:
        return effort
    # "none" disables reasoning — never a degradation target for an enabled ask.
    candidates = [level for level in supported_norm if level != "none"]
    if not candidates:
        return effort
    requested_idx = EFFORT_LADDER.index(requested)
    below = [level for level in candidates if EFFORT_LADDER.index(level) < requested_idx]
    if below:
        return max(below, key=EFFORT_LADDER.index)
    return min(candidates, key=EFFORT_LADDER.index)


def _route_effort_decl(model: Optional[str], base_url: Optional[str]):
    """Return ``(supported_tuple, known_set, aliases)`` for this route.

    The vocabulary is the route's declaration, or else what the endpoint's
    ``/models`` listed for the model on the agent's last probe (cache only).
    ``aliases`` are the endpoint's listed rewrites (``None`` for a declared
    set, which is the user's word and carries none).
    """
    if not model:
        return None, (), None
    try:
        from son_of_anton_cli.config import get_custom_provider_reasoning_decl

        decl = get_custom_provider_reasoning_decl(str(model), base_url) or {}
    except Exception:
        decl = {}
    efforts = decl.get("efforts")
    if efforts:
        return tuple(efforts), set(efforts), None
    if not base_url:
        return None, (), None
    try:
        from agent.model_metadata import get_endpoint_reasoning_decl

        listed = get_endpoint_reasoning_decl(str(model), str(base_url))
    except Exception:
        listed = {}
    efforts = listed.get("efforts")
    if efforts:
        return tuple(efforts), set(efforts), listed.get("aliases")
    return None, (), None


def custom_reasoning_extras(
    reasoning_config: Optional[dict],
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wire fields for a reasoning config: ``(extra_body, top_level_kwargs)``.

    - disabled (or ``none``) → top-level ``reasoning_effort: "none"`` (unless
      the route's declared set lacks ``none``) plus ``think: false`` and
      ``chat_template_kwargs.enable_thinking: false``, the switches the
      different servers honour;
    - an effort → top-level ``reasoning_effort`` clamped onto the route's set;
    - no effort → nothing, so the endpoint's own default applies.
    """
    extra_body: dict[str, Any] = {}
    top_level: dict[str, Any] = {}
    if not isinstance(reasoning_config, dict) or not reasoning_config:
        return extra_body, top_level
    effort = (reasoning_config.get("effort") or "").strip().lower()
    supported, decl, aliases = _route_effort_decl(model, base_url)
    if effort == "none" or reasoning_config.get("enabled", True) is False:
        if not decl or "none" in decl:
            top_level["reasoning_effort"] = "none"
        extra_body["think"] = False
        extra_body["chat_template_kwargs"] = {"enable_thinking": False}
    elif effort:
        top_level["reasoning_effort"] = clamp_effort(
            effort, supported or OPENAI_COMPAT_WIRE_EFFORTS, aliases
        )
    return extra_body, top_level
