"""A custom provider that declares its models shows exactly that subset.

Regression contract for the Bifrost-style shape: a Nix-managed
``custom_providers.<name>.models`` mapping whitelists a few models behind one
endpoint. The picker must show that subset, not the endpoint's full
``/v1/models`` catalog. ``show_all_models`` (``/model --all``) is the escape
hatch back to the full catalog.
"""

from __future__ import annotations

from son_of_anton_cli.model_switch import (
    _entry_catalog_pinned,
    list_authenticated_providers,
)

BASE_URL = "http://10.0.0.6:4002/v1"
DECLARED = {
    "vllm/qwen3.8-27b": {"reasoning_effort": "xhigh"},
    "strix/qwen3.8-flash-next": {"reasoning_effort": "xhigh"},
    "deepseek/deepseek-flash": {"reasoning_effort": "max"},
}
FULL_CATALOG = [
    "vllm/qwen3.8-27b",
    "strix/qwen3.8-flash-next",
    "deepseek/deepseek-flash",
    "vllm/a-model-the-user-did-not-pick",
    "extra/another-live-model",
]


def _custom_providers():
    return [
        {
            "name": "custom",
            "base_url": BASE_URL,
            "api_key": "test-key",
            "models": dict(DECLARED),
        }
    ]


def _row(show_all_models: bool, monkeypatch):
    monkeypatch.setattr(
        "son_of_anton_cli.model_switch._fetch_picker_live_models",
        lambda *args, **kwargs: list(FULL_CATALOG),
    )
    rows = list_authenticated_providers(
        current_provider="custom",
        current_base_url=BASE_URL,
        custom_providers=_custom_providers(),
        probe_custom_providers=True,
        probe_current_custom_provider=True,
        show_all_models=show_all_models,
    )
    row = next((r for r in rows if r.get("api_url") == BASE_URL), None)
    assert row is not None, f"custom row missing from {[r.get('slug') for r in rows]}"
    return row


def test_declared_mapping_is_a_pin() -> None:
    assert _entry_catalog_pinned(_custom_providers()[0]) is True


def test_discovered_or_explicit_discovery_do_not_pin() -> None:
    assert _entry_catalog_pinned({"models": {"a": {}}, "models_discovered": True}) is False
    assert _entry_catalog_pinned({"models": {"a": {}}, "discover_models": True}) is False
    assert _entry_catalog_pinned({"models": {}}) is False
    assert _entry_catalog_pinned({}) is False


def test_picker_shows_only_the_declared_subset(monkeypatch) -> None:
    row = _row(show_all_models=False, monkeypatch=monkeypatch)
    assert row["models"] == list(DECLARED)
    assert row["models_pinned"] is True


def test_show_all_widens_to_the_live_catalog(monkeypatch) -> None:
    row = _row(show_all_models=True, monkeypatch=monkeypatch)
    assert row["models"] == FULL_CATALOG
    assert row["models_pinned"] is True


def test_providers_keyed_catalog_pins_too(monkeypatch) -> None:
    """The newer ``providers:`` schema gets the same declared-subset default."""
    monkeypatch.setattr(
        "son_of_anton_cli.model_switch._fetch_picker_live_models",
        lambda *args, **kwargs: list(FULL_CATALOG),
    )
    rows = list_authenticated_providers(
        current_provider="custom",
        current_base_url=BASE_URL,
        user_providers={
            "custom": {
                "base_url": BASE_URL,
                "api_key": "test-key",
                "models": dict(DECLARED),
            }
        },
        probe_custom_providers=True,
        probe_current_custom_provider=True,
    )
    row = next((r for r in rows if r.get("api_url") == BASE_URL), None)
    assert row is not None
    assert row["models"] == list(DECLARED)
    assert row["models_pinned"] is True
