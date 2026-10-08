"""The reasoning picker must only offer levels the route accepts.

``supported_efforts_for()`` mirrors the transport's clamp selection so the
``/reasoning`` picker cannot offer a level the wire would clamp or reject:
declared custom-route set → known wire hosts (Kimi, TokenHub) → widest
OpenAI-compatible vocabulary.
"""

from __future__ import annotations

from agent.reasoning_effort import (
    OPENAI_COMPAT_WIRE_EFFORTS,
    TOKENHUB_EFFORTS,
    supported_efforts_for,
)

BASE_URL = "http://provider.invalid/v1"
DECLARED_EFFORTS = ("none", "low", "medium", "xhigh")


def _declared(custom_base_url: str = BASE_URL, model: str = "model-a"):
    custom_providers = [
        {
            "name": "custom",
            "base_url": custom_base_url,
            "models": {model: {"reasoning_efforts": list(DECLARED_EFFORTS)}},
        }
    ]
    config = {"custom_providers": {"custom": custom_providers[0]}}
    return custom_providers, config


def test_declared_route_vocabulary_wins() -> None:
    custom_providers, config = _declared()
    efforts = supported_efforts_for(
        "model-a",
        provider="custom",
        base_url=BASE_URL,
        custom_providers=custom_providers,
        config=config,
    )
    assert efforts == DECLARED_EFFORTS


def test_declaration_beats_known_host_family() -> None:
    """A declared set is explicit user intent — it wins on a known host too."""
    custom_providers, config = _declared("https://api.kimi.com/v1", model="k3")
    efforts = supported_efforts_for(
        "k3",
        base_url="https://api.kimi.com/v1",
        custom_providers=custom_providers,
        config=config,
    )
    assert efforts == DECLARED_EFFORTS


def test_kimi_host_gets_the_kimi_vocabulary() -> None:
    assert supported_efforts_for("k3", base_url="https://api.kimi.com/v1") == (
        "low", "high", "max",
    )
    assert supported_efforts_for(
        "kimi-k2.6", base_url="https://api.moonshot.ai/v1"
    ) == ("low", "medium", "high")


def test_tokenhub_host_gets_the_tokenhub_vocabulary() -> None:
    assert supported_efforts_for(
        "hunyuan-t1", base_url="https://tokenhub.tencentmaas.com/v1"
    ) == TOKENHUB_EFFORTS


def test_unknown_route_gets_the_widest_openai_compatible_set() -> None:
    assert supported_efforts_for(
        "gpt-5.5", base_url="https://api.openai.com/v1"
    ) == OPENAI_COMPAT_WIRE_EFFORTS


def test_no_route_information_gets_the_default_set() -> None:
    assert supported_efforts_for("") == OPENAI_COMPAT_WIRE_EFFORTS
