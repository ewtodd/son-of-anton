"""``reasoning_effort`` written on a custom-provider route must take effect.

Regression contract for two silent-failure shapes:

1. An effort declared next to ``context_length`` in
   ``custom_providers.<p>.models.<m>.reasoning_effort`` was never read —
   ``resolve_reasoning_config`` only consulted the ``agent`` section, so the
   endpoint quietly ran at its own default.
2. The wire vocabulary is endpoint-specific (a Qwen3-on-vLLM route through
   LiteLLM accepts exactly none/low/medium/xhigh and 400s on
   minimal/high/max/ultra). The declared ``reasoning_efforts`` set is the
   data a transport clamps against; without it the helper must stay out of
   the way (no opinion) rather than guess a narrower set.

These tests pin the *relations* between the declared values and the
resolved ones, not current endpoint facts.
"""

from __future__ import annotations

import pytest

from son_of_anton_constants import resolve_reasoning_config
from son_of_anton_cli.config import get_custom_provider_reasoning_decl

BASE_URL = "http://provider.invalid/v1"
DECLARED_EFFORTS = ("none", "low", "medium", "xhigh")


@pytest.fixture
def custom_providers():
    return [
        {
            "name": "custom",
            "base_url": BASE_URL,
            "models": {
                "model-a": {
                    "context_length": 262144,
                    "reasoning_effort": "medium",
                    "reasoning_efforts": list(DECLARED_EFFORTS),
                },
                "model-b": {
                    "context_length": 262144,
                    "reasoning_efforts": list(DECLARED_EFFORTS),
                },
            },
        }
    ]


@pytest.fixture
def config(custom_providers):
    return {"custom_providers": {"custom": custom_providers[0]}}


def test_the_declared_effort_comes_back_verbatim(config, custom_providers) -> None:
    decl = get_custom_provider_reasoning_decl(
        "model-a", BASE_URL, custom_providers=custom_providers, config=config
    )
    assert decl.get("effort") == "medium"


def test_declared_vocabulary_round_trips(config, custom_providers) -> None:
    decl = get_custom_provider_reasoning_decl(
        "model-a", BASE_URL, custom_providers=custom_providers, config=config
    )
    assert decl.get("efforts") == DECLARED_EFFORTS


def test_vocabulary_without_an_effort_still_declares(config, custom_providers) -> None:
    """model-b sets only the accepted set — no effort opinion of its own."""
    decl = get_custom_provider_reasoning_decl(
        "model-b", BASE_URL, custom_providers=custom_providers, config=config
    )
    assert decl.get("effort") is None
    assert decl.get("efforts") == DECLARED_EFFORTS


def test_no_declaration_means_no_opinion(config, custom_providers) -> None:
    """Callers keep the legacy behavior (widest OpenAI-compat set) until
    the user actually declares a vocabulary — we must not guess narrower."""
    bare = [
        {
            "name": "custom",
            "base_url": BASE_URL,
            "models": {"model-a": {"context_length": 262144}},
        }
    ]
    decl = get_custom_provider_reasoning_decl(
        "model-a", BASE_URL, custom_providers=bare, config={}
    )
    assert "efforts" not in decl
    assert "effort" not in decl


def test_route_identity_prevents_cross_route_leakage(config, custom_providers) -> None:
    other = get_custom_provider_reasoning_decl(
        "model-a",
        "http://another.invalid/v1",
        custom_providers=custom_providers,
        config=config,
    )
    assert other == {}


def test_unlisted_model_borrows_nothing(config, custom_providers) -> None:
    decl = get_custom_provider_reasoning_decl(
        "model-z", BASE_URL, custom_providers=custom_providers, config=config
    )
    assert decl == {}


def test_resolver_honours_the_per_route_effort(config) -> None:
    """The regression: without this, an effort written next to
    ``context_length`` resolved to the agent global (None here) and the
    endpoint ran at its own default."""
    resolved = resolve_reasoning_config(
        config, "model-a", base_url=BASE_URL, provider="custom"
    )
    assert resolved == {"enabled": True, "effort": "medium"}


def test_resolver_without_declaration_falls_through_to_agent(config) -> None:
    bare = dict(config)
    bare = {
        "custom_providers": {
            "custom": {
                "base_url": BASE_URL,
                "models": {"model-a": {"context_length": 262144}},
            }
        },
        "agent": {"reasoning_effort": "low"},
    }
    resolved = resolve_reasoning_config(
        bare, "model-a", base_url=BASE_URL, provider="custom"
    )
    assert resolved == {"enabled": True, "effort": "low"}


def test_resolver_per_model_override_still_wins(config) -> None:
    cfg = {
        **config,
        "agent": {"reasoning_overrides": {"model-a": "xhigh"}},
    }
    resolved = resolve_reasoning_config(
        cfg, "model-a", base_url=BASE_URL, provider="custom"
    )
    assert resolved == {"enabled": True, "effort": "xhigh"}
