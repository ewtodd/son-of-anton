"""A per-model custom-provider context window must bust the gateway agent cache.

The gateway caches one AIAgent per session for prompt-cache stability, keyed on
a signature computed from config. ``model.context_length`` was already a
signature input; the per-model
``custom_providers[].models.<id>.context_length`` form was not, so a Signal
session kept compacting against the old window until an unrelated eviction
(idle TTL, /model, restart). The nested form is flattened into the same
signature here.
"""

from __future__ import annotations

from gateway.run import GatewayRunner

BASE_URL = "http://10.0.0.6:4002/v1"


def _signature(config: dict) -> str:
    return GatewayRunner._agent_config_signature(
        "qwen3.8-flash-next",
        {
            "api_key": "test-key",
            "base_url": BASE_URL,
            "provider": "custom",
            "requested_provider": "custom",
            "api_mode": "chat_completions",
        },
        ["core"],
        "",
        cache_keys=GatewayRunner._extract_cache_busting_config(config),
    )


def _dict_form(window: int) -> dict:
    return {
        "custom_providers": {
            "custom": {
                "base_url": BASE_URL,
                "models": {"qwen3.8-flash-next": {"context_length": window}},
            }
        }
    }


def test_a_custom_provider_window_edit_rebuilds_the_cached_agent() -> None:
    assert _signature(_dict_form(262_144)) != _signature(_dict_form(131_072))


def test_the_keyed_providers_schema_is_covered_too() -> None:
    def config(window: int) -> dict:
        return {
            "providers": {
                "strix": {
                    "api": BASE_URL,
                    "models": {"qwen3.8-flash-next": {"context_length": window}},
                }
            }
        }

    assert _signature(config(262_144)) != _signature(config(131_072))


def test_list_form_custom_providers_are_covered_too() -> None:
    def config(window: int) -> dict:
        return {
            "custom_providers": [
                {
                    "name": "custom",
                    "base_url": BASE_URL,
                    "models": [
                        {"id": "qwen3.8-flash-next", "context_length": window}
                    ],
                }
            ]
        }

    assert _signature(config(262_144)) != _signature(config(131_072))


def test_only_per_model_windows_feed_the_signature() -> None:
    """Entry-level windows are not read by resolution; headers are not windows."""
    config = _dict_form(262_144)
    entry = config["custom_providers"]["custom"]
    entry["context_length"] = 999_999
    entry["extra_headers"] = {"X-Test": "1"}
    assert GatewayRunner._custom_provider_context_windows(config) == [
        ("custom_providers", "custom", "qwen3.8-flash-next", "262144")
    ]
