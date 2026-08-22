"""Provider-catalog contracts — the fork ships exactly two API-key providers
(deepseek, openai-api) plus config.yaml custom endpoints. These tests assert
the catalog structure that the /model picker, setup wizard, and provider
resolution all derive from, so a provider added or removed in one layer but
not the others fails loudly.
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
    assert set(PROVIDER_REGISTRY) == {"deepseek", "openai-api"}


def test_registry_entries_are_usable_api_key_providers() -> None:
    for pid, pconfig in PROVIDER_REGISTRY.items():
        assert pconfig.id == pid
        assert pconfig.auth_type == "api_key"
        assert pconfig.inference_base_url.startswith("https://")
        assert pconfig.api_key_env_vars, f"{pid} must declare an API key env var"


def test_canonical_providers_include_fork_providers() -> None:
    slugs = {p.slug for p in CANONICAL_PROVIDERS}
    assert {"deepseek", "openai-api"} <= slugs
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
    for slug in ("nous", "openrouter", "gemini", "copilot", "bedrock"):
        assert get_provider(slug, allow_network=False) is None, f"{slug} still resolves"


def test_api_mode_for_fork_providers() -> None:
    assert determine_api_mode("deepseek") == "chat_completions"
    assert determine_api_mode("openai-api") == "codex_responses"
    assert determine_api_mode("custom") == "chat_completions"


def test_labels_do_not_carry_upstream_copy() -> None:
    label = get_label("deepseek")
    assert "nous" not in label.lower()
    assert "openrouter" not in label.lower()
