"""Endpoint contracts — the fork speaks to one kind of endpoint: an
OpenAI-compatible ``custom_providers`` entry in config.toml.
"""

from __future__ import annotations

import importlib


def test_removed_provider_modules_are_gone() -> None:
    """Vendor adapters, auth flows and catalogs must not come back.

    Each was reachable only through a provider the fork no longer ships, so
    re-adding the module without the provider recreates unreachable code.
    """
    for mod in (
        "agent.bedrock_adapter",
        "agent.vertex_adapter",
        "agent.anthropic_adapter",
        "agent.transports.anthropic",
        "agent.gemini_native_adapter",
        "agent.copilot_acp_client",
        "agent.credential_pool",
        "agent.credential_persistence",
        "agent.models_dev",
        "agent.billing_links",
        "providers",
        "son_of_anton_cli.auth",
        "son_of_anton_cli.providers",
        "son_of_anton_cli.credential_lifecycle",
        "son_of_anton_cli.fallback_config",
        "son_of_anton_cli.model_normalize",
        "son_of_anton_cli.model_selection_guards",
        "son_of_anton_cli.model_setup_flows",
    ):
        try:
            importlib.import_module(mod)
        except ImportError:
            continue
        raise AssertionError(f"{mod} is importable again — dead provider resurrected")


def test_custom_providers_dict_form_normalizes() -> None:
    # The keyed dict form (custom_providers.custom.base_url = ...) is the
    # shape the fork's declarative configs use.
    from son_of_anton_cli.config import get_compatible_custom_providers

    providers = get_compatible_custom_providers(
        {"custom_providers": {"custom": {"base_url": "http://127.0.0.1:8080/v1"}}}
    )
    assert len(providers) == 1
    assert providers[0]["name"] == "custom"
    assert providers[0]["base_url"] == "http://127.0.0.1:8080/v1"


def test_runtime_resolves_the_declared_endpoint_and_its_key_env(monkeypatch) -> None:
    """``provider = "custom"`` resolves to the entry's base_url, with the key
    read from the env var its ``key_env`` names, on the chat-completions wire."""
    import os

    from son_of_anton_cli.runtime_provider import resolve_runtime_provider

    home = os.environ["SON_OF_ANTON_HOME"]
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as f:
        f.write(
            '[model]\nprovider = "custom"\ndefault = "m"\n'
            '[custom_providers.custom]\nbase_url = "http://10.0.0.2:4002/v1/"\n'
            'key_env = "SOA_TEST_VK"\n'
        )
    monkeypatch.setenv("SOA_TEST_VK", "vk-secret-value")

    runtime = resolve_runtime_provider()
    assert runtime["provider"] == "custom"
    assert runtime["api_mode"] == "chat_completions"
    assert runtime["base_url"] == "http://10.0.0.2:4002/v1"
    assert runtime["api_key"] == "vk-secret-value"

    # An explicit base_url never carries the entry's stored key elsewhere.
    other = resolve_runtime_provider(explicit_base_url="http://elsewhere.example/v1")
    assert other["base_url"] == "http://elsewhere.example/v1"
    assert other["api_key"] != "vk-secret-value"
