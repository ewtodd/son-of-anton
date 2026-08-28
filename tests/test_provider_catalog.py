"""Provider-catalog contracts — the fork ships exactly one API-key provider
(openai-api) plus config.yaml custom endpoints. These tests assert the catalog
structure that the /model picker, setup wizard, and provider resolution all
derive from, so a provider added or removed in one layer but not the others
fails loudly.

DeepSeek was the second API-key provider until 2026-08-27. Its models are
still reachable — as any other model is — by pointing a custom endpoint or an
aggregator at them; what went away is the direct-API integration.
"""

from __future__ import annotations

from son_of_anton_cli.auth import PROVIDER_REGISTRY
from son_of_anton_cli.models import CANONICAL_PROVIDERS
from son_of_anton_cli.providers import (
    determine_api_mode,
    get_label,
    get_provider,
    normalize_provider,
)


def test_registry_contains_exactly_the_fork_providers() -> None:
    assert set(PROVIDER_REGISTRY) == {"openai-api"}


def test_registry_entries_are_usable_api_key_providers() -> None:
    for pid, pconfig in PROVIDER_REGISTRY.items():
        assert pconfig.id == pid
        assert pconfig.auth_type == "api_key"
        assert pconfig.inference_base_url.startswith("https://")
        assert pconfig.api_key_env_vars, f"{pid} must declare an API key env var"


def test_canonical_providers_include_fork_providers() -> None:
    slugs = {p.slug for p in CANONICAL_PROVIDERS}
    assert "openai-api" in slugs
    assert "deepseek" not in slugs
    # The custom endpoint slot is the plugin-registered third surface.
    assert "custom" in slugs


def test_every_canonical_provider_resolves_to_a_known_surface() -> None:
    # Each canonical slug must resolve through either the auth registry
    # (api-key providers), the plugin profile registry (custom), or the
    # models.dev/overlay lookup. No orphan entries in the picker.
    from providers import get_provider_profile

    for entry in CANONICAL_PROVIDERS:
        slug = entry.slug
        in_registry = slug in PROVIDER_REGISTRY
        in_plugins = get_provider_profile(slug) is not None
        assert in_registry or in_plugins, f"canonical provider {slug!r} resolves nowhere"


def test_openai_alias_resolves_to_openai_api() -> None:
    assert normalize_provider("openai") == "openai-api"
    assert normalize_provider("OpenAI") == "openai-api"


def test_removed_providers_no_longer_resolve_offline() -> None:
    # Pruned providers must not resolve through overlays; with models.dev
    # unreachable (isolated test home, no cache) they resolve to None.
    for slug in (
        "nous", "openrouter", "gemini", "copilot",
        # Removed 2026-08-25: the dead-provider sweep. Each had a full
        # adapter + auth + pricing + runtime-resolution surface that could
        # never fire, because none of them are in SON_OF_ANTON_OVERLAYS.
        "bedrock", "aws", "aws-bedrock",
        "vertex", "google-vertex", "vertex-ai",
        "azure-foundry", "foundry",
        # Removed 2026-08-27: the direct DeepSeek API, in favour of reaching
        # those models over an OpenAI-compatible endpoint like everything
        # else. The alias went with it.
        "deepseek", "deep-seek",
    ):
        assert get_provider(slug, allow_network=False) is None, f"{slug} still resolves"


def test_removed_provider_adapters_are_gone() -> None:
    """The dead-provider adapters must not come back as importable modules.

    Each of these was reachable only through a provider id that
    ``get_provider`` rejects, so re-adding the module without re-adding the
    provider would recreate ~2,700 lines of unreachable code.
    """
    import importlib

    for mod in (
        "agent.bedrock_adapter",
        "agent.azure_identity_adapter",
        "agent.vertex_adapter",
        "agent.transports.bedrock",
        "son_of_anton_cli.azure_detect",
        # Removed 2026-08-27: wires no shipped provider could select.
        "agent.anthropic_adapter",
        "agent.transports.anthropic",
        "agent.gemini_native_adapter",
        "agent.gemini_schema",
        "agent.copilot_acp_client",
        "son_of_anton_cli.copilot_auth",
    ):
        try:
            importlib.import_module(mod)
        except ImportError:
            continue
        raise AssertionError(f"{mod} is importable again — dead provider resurrected")


def test_no_dead_provider_auth_types_registered() -> None:
    """PROVIDER_REGISTRY must not carry auth flows with no provider behind them."""
    from son_of_anton_cli.auth import PROVIDER_REGISTRY

    auth_types = {p.auth_type for p in PROVIDER_REGISTRY.values()}
    assert "aws_sdk" not in auth_types, "aws_sdk auth survives without Bedrock"
    assert "vertex" not in auth_types, "vertex auth survives without the adapter"


def test_api_mode_for_fork_providers() -> None:
    assert determine_api_mode("deepseek") == "chat_completions"
    assert determine_api_mode("openai-api") == "codex_responses"
    assert determine_api_mode("custom") == "chat_completions"


def test_labels_do_not_carry_upstream_copy() -> None:
    label = get_label("deepseek")
    assert "nous" not in label.lower()
    assert "openrouter" not in label.lower()


def test_custom_providers_dict_form_normalizes() -> None:
    # The keyed dict form (custom_providers.custom.base_url = ...) is the
    # shape the fork's examples and declarative configs use; the runtime
    # resolution must normalize it (gateway live-bug regression).
    from son_of_anton_cli.config import get_compatible_custom_providers

    providers = get_compatible_custom_providers(
        {
            "custom_providers": {
                "custom": {"base_url": "http://127.0.0.1:8080/v1"},
            },
        }
    )
    assert len(providers) == 1
    assert providers[0]["name"] == "custom"
    assert providers[0]["base_url"] == "http://127.0.0.1:8080/v1"


def test_the_anthropic_wire_cannot_be_selected() -> None:
    """The fork speaks OpenAI-compatible wires only.

    Three things used to route a turn onto the Anthropic Messages transport:
    ``provider: anthropic``, a base URL at api.anthropic.com, and any URL
    ending in ``/anthropic``. None of them can now, so the adapter behind
    them had nothing left to reach it.
    """
    from son_of_anton_cli.providers import (
        TRANSPORT_TO_API_MODE,
        determine_api_mode,
        host_mandated_api_mode,
    )

    assert "anthropic_messages" not in TRANSPORT_TO_API_MODE.values()
    assert determine_api_mode("anthropic") == "chat_completions"
    for url in (
        "https://api.anthropic.com",
        "https://example.test/anthropic",
        "https://api.kimi.com/coding",
    ):
        assert host_mandated_api_mode(url) != "anthropic_messages"
