"""Turning thinking off must reach chat-template-driven local servers.

vLLM / llama.cpp serve Qwen3-family models whose thinking switch is a chat
template variable; ``reasoning_effort: "none"`` and Ollama's ``think: false``
do not reach it. Contracts: a disabled reasoning config always carries
``chat_template_kwargs.enable_thinking = false``; per-model
``custom_providers.<name>.models.<model>.chat_template_kwargs`` from
config.toml passes through to ``extra_body`` and its keys win.
"""

from __future__ import annotations

import pytest

from providers import get_provider_profile

BASE_URL = "http://gateway.invalid:4002/v1"


@pytest.fixture
def profile():
    p = get_provider_profile("custom")
    assert p is not None
    return p


@pytest.fixture
def configured(son_of_anton_home):
    (son_of_anton_home / "config.toml").write_text(
        f"""
[custom_providers.custom]
base_url = "{BASE_URL}"

[custom_providers.custom.models."local/qwen"]
reasoning_efforts = ["none", "low", "medium", "xhigh"]

[custom_providers.custom.models."local/qwen".chat_template_kwargs]
preserve_thinking = true

[custom_providers.custom.models."local/forced"]
chat_template_kwargs = {{ enable_thinking = true }}
""",
        encoding="utf-8",
    )


def _extra_body(profile, reasoning_config, model="local/plain"):
    extra_body, _top = profile.build_api_kwargs_extras(
        reasoning_config=reasoning_config, model=model, base_url=BASE_URL
    )
    return extra_body


@pytest.mark.parametrize(
    "reasoning_config", [{"enabled": False}, {"enabled": True, "effort": "none"}]
)
def test_disabling_thinking_sets_the_template_switch(profile, reasoning_config) -> None:
    body = _extra_body(profile, reasoning_config)
    assert body["chat_template_kwargs"]["enable_thinking"] is False


def test_enabled_thinking_does_not_force_the_template_switch(profile) -> None:
    body = _extra_body(profile, {"enabled": True, "effort": "medium"})
    assert "enable_thinking" not in body.get("chat_template_kwargs", {})


def test_declared_template_kwargs_pass_through(profile, configured) -> None:
    on = _extra_body(profile, {"enabled": True, "effort": "medium"}, model="local/qwen")
    assert on["chat_template_kwargs"] == {"preserve_thinking": True}
    off = _extra_body(profile, {"enabled": False}, model="local/qwen")
    assert off["chat_template_kwargs"] == {"preserve_thinking": True, "enable_thinking": False}


def test_a_declared_key_wins_over_the_thinking_off_default(profile, configured) -> None:
    body = _extra_body(profile, {"enabled": False}, model="local/forced")
    assert body["chat_template_kwargs"]["enable_thinking"] is True
