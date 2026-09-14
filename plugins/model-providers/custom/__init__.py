"""Custom / Ollama (local) provider profile.

Covers any endpoint registered as provider="custom", including local
Ollama instances and OpenAI-compatible reasoning endpoints (GLM-5.2 on
Volcengine ARK, vLLM, llama.cpp). Key quirks:
  - ollama_num_ctx → extra_body.options.num_ctx (local context window)
  - reasoning_config disabled → top-level reasoning_effort="none"
    (Ollama /v1/chat/completions ignores think=False — ollama#14820)
    + extra_body.think = False for /api/chat and proxies
  - reasoning_config enabled + effort → top-level reasoning_effort
    (the native OpenAI-compatible format GLM/ARK expect; unset omits it
    so the endpoint's server default applies)
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class CustomProfile(ProviderProfile):
    """Custom/Ollama local provider — think=false and num_ctx support."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        ollama_num_ctx: int | None = None,
        **ctx: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Ollama context window
        if ollama_num_ctx:
            options = extra_body.get("options", {})
            options["num_ctx"] = ollama_num_ctx
            extra_body["options"] = options

        # Reasoning / thinking control for custom OpenAI-compatible endpoints
        # (GLM-5.2 on Volcengine ARK, vLLM, Ollama, llama.cpp, …).
        #
        #   - disabled  → extra_body.think = False (Ollama's thinking-off flag)
        #   - enabled + effort set → TOP-LEVEL reasoning_effort string, the
        #     format GLM-5.2/ARK and other OpenAI-compatible reasoning APIs
        #     expect (GLM documents "high" and "max"; "max" is its default).
        #   - enabled + no effort  → omit both, so the endpoint applies its own
        #     server-side default (do NOT force a level the user didn't pick).
        #
        # We deliberately do NOT emit ``think=True`` on enable: it is an
        # Ollama-only flag and thinking is already server-default-on for these
        # backends, so forcing it risks a 400 on GLM/vLLM endpoints that don't
        # recognize it. Mirrors the DeepSeek/Zai profile precedent.
        if reasoning_config and isinstance(reasoning_config, dict):
            _effort = (reasoning_config.get("effort") or "").strip().lower()
            _enabled = reasoning_config.get("enabled", True)
            # Per-route accepted-effort declaration (optional):
            # ``custom_providers.<name>.models.<model>.reasoning_efforts``.
            # Custom endpoints disagree on the ``reasoning_effort`` wire
            # vocabulary — a Qwen3-on-vLLM route through LiteLLM accepts
            # exactly none/low/medium/xhigh and 400s on minimal/high/max/
            # ultra (verified live). When the user declares the set, clamp
            # against it; otherwise fall back to the widest OpenAI-compat
            # vocabulary (permissive endpoints ignore unknown levels, and
            # we must not guess a narrower set).
            _supported, _decl = _custom_route_effort_decl(
                ctx.get("model"), ctx.get("base_url")
            )
            if _effort == "none" or _enabled is False:
                # Ollama's /v1/chat/completions silently ignores
                # extra_body.think (only /api/chat honours it — ollama#14820)
                # but respects the top-level reasoning_effort field, so both
                # are needed to actually stop a thinking-capable model from
                # reasoning (#25758). Endpoints that recognize neither simply
                # ignore them.
                #
                # A declared set that does not include "none" rejects the
                # field with HTTP 400 — in that case only think=False goes
                # out (the endpoint's own thinking-off mechanism). With no
                # declaration, keep the historical "none" (permissive
                # endpoints ignore it).
                if not _decl or "none" in _decl:
                    top_level["reasoning_effort"] = "none"
                extra_body["think"] = False
            elif _effort:
                # Clamp the internal ladder onto the route's vocabulary
                # (shared policy in agent.reasoning_effort). Nearest-weaker
                # semantics give exactly the right mappings on a
                # none/low/medium/xhigh route: high→medium, max/ultra→xhigh
                # (the endpoint's own top), minimal→low. Forwarding "ultra"
                # verbatim is a guaranteed 400 (#89503).
                from agent.reasoning_effort import (
                    OPENAI_COMPAT_WIRE_EFFORTS,
                    clamp_effort,
                )

                top_level["reasoning_effort"] = clamp_effort(
                    _effort, _supported or OPENAI_COMPAT_WIRE_EFFORTS
                )

        return extra_body, top_level

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Custom/Ollama: base_url is user-configured; fetch if set."""
        if not (base_url or self.base_url):
            return None
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)


def _custom_route_effort_decl(model: str | None, base_url: str | None):
    """Return ``(supported_tuple, declared_set)`` for this route, or ``(None, ())``.

    Reads the optional per-route accepted-effort declaration
    (``custom_providers.<name>.models.<model>.reasoning_efforts``) via the
    shared config helper. ``supported_tuple`` is the declared vocabulary to
    clamp against (or ``None`` → caller falls back to the widest
    OpenAI-compat set); ``declared_set`` is the same levels as a set for the
    disable-path ``"none" in _decl`` check (empty when undeclared).
    """
    if not model:
        return None, ()
    try:
        from son_of_anton_cli.config import get_custom_provider_reasoning_decl
    except Exception:
        return None, ()
    try:
        decl = get_custom_provider_reasoning_decl(str(model), base_url) or {}
    except Exception:
        return None, ()
    efforts = decl.get("efforts")
    if efforts:
        return tuple(efforts), set(efforts)
    return None, ()


custom = CustomProfile(
    name="custom",
    aliases=(
        "ollama",
        "local",
        "vllm",
        "llamacpp",
        "llama.cpp",
        "llama-cpp",
    ),
    env_vars=(),  # No fixed key — custom endpoint
    base_url="",  # User-configured
    # Without this, no max_tokens is sent and Ollama falls back to its internal
    # num_predict=128, truncating responses after a few tokens (#39281). This is
    # only a floor used when the user hasn't set model.max_tokens — they can
    # override per-model — so we set it generously rather than lowballing it.
    default_max_tokens=65536,
)

register_provider(custom)
