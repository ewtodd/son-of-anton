"""A per-model `context_length` in config.yaml has to reach every caller.

`custom_providers.<p>.models.<m>.context_length` was honoured only when the
caller threaded the provider list through as an argument. One of the three call
sites did. The others — the CLI's @-reference sizing and the context compressor
— skipped the override and fell through to the catalog's generic 128K.

The compressor is the worst place for it to land: it sizes compaction against a
window eight times smaller than the real one, so it compacts a conversation
with most of its context still free, and nothing about that looks like an
error.
"""

from __future__ import annotations

import pytest

from agent.model_metadata import get_model_context_length

BASE_URL = "http://provider.invalid/v1"
PROVIDERS = [
    {
        "name": "custom",
        "base_url": BASE_URL,
        "models": {
            "big-context-model": {"context_length": 1_048_576},
            "small-context-model": {"context_length": 131_072},
        },
    }
]
CONFIG = {"custom_providers": {"custom": {"base_url": BASE_URL, "models": PROVIDERS[0]["models"]}}}


@pytest.fixture
def configured(monkeypatch):
    """Make the resolver see this config when it loads one itself."""
    monkeypatch.setattr(
        "son_of_anton_cli.config.load_config", lambda *a, **k: CONFIG
    )
    monkeypatch.setattr(
        "son_of_anton_cli.config.get_compatible_custom_providers",
        lambda config=None: PROVIDERS,
    )


def test_the_override_applies_when_the_caller_passes_the_providers(configured) -> None:
    assert (
        get_model_context_length(
            "big-context-model",
            base_url=BASE_URL,
            provider="custom",
            custom_providers=PROVIDERS,
        )
        == 1_048_576
    )


def test_the_override_applies_when_the_caller_does_not(configured) -> None:
    """The regression: two of three call sites pass nothing."""
    assert (
        get_model_context_length(
            "big-context-model", base_url=BASE_URL, provider="custom"
        )
        == 1_048_576
    ), "fell through to the catalog default despite config.yaml declaring it"


def test_each_model_gets_its_own_value(configured) -> None:
    assert (
        get_model_context_length(
            "small-context-model", base_url=BASE_URL, provider="custom"
        )
        == 131_072
    )


def test_an_explicit_config_context_length_still_wins(configured) -> None:
    assert (
        get_model_context_length(
            "big-context-model",
            base_url=BASE_URL,
            provider="custom",
            config_context_length=4096,
        )
        == 4096
    )


def test_an_unlisted_model_does_not_borrow_another_entry(configured) -> None:
    result = get_model_context_length(
        "not-in-this-provider", base_url=BASE_URL, provider="custom"
    )
    assert result != 1_048_576
