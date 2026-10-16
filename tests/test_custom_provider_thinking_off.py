"""Turning thinking off must reach chat-template-driven local servers.

vLLM / llama.cpp serve Qwen3-family models whose thinking switch is a chat
template variable; ``reasoning_effort: "none"`` and Ollama's ``think: false``
do not reach it. Contract: a disabled reasoning config always carries
``chat_template_kwargs.enable_thinking = false``, and a user override's keys
merge into it per key.
"""

from __future__ import annotations

import pytest

from agent.reasoning_effort import custom_reasoning_extras

BASE_URL = "http://gateway.invalid:4002/v1"


def _extra_body(reasoning_config, model="local/plain"):
    extra_body, _top = custom_reasoning_extras(reasoning_config, model=model, base_url=BASE_URL)
    return extra_body


@pytest.mark.parametrize(
    "reasoning_config", [{"enabled": False}, {"enabled": True, "effort": "none"}]
)
def test_disabling_thinking_sets_the_template_switch(reasoning_config) -> None:
    body = _extra_body(reasoning_config)
    assert body["chat_template_kwargs"]["enable_thinking"] is False


def test_enabled_thinking_does_not_force_the_template_switch() -> None:
    body = _extra_body({"enabled": True, "effort": "medium"})
    assert "enable_thinking" not in body.get("chat_template_kwargs", {})


def _wire_extra_body(reasoning_config, request_overrides, model="local/plain"):
    from agent.transports.chat_completions import ChatCompletionsTransport

    kwargs = ChatCompletionsTransport().build_kwargs(
        model,
        [{"role": "user", "content": "hi"}],
        base_url=BASE_URL,
        reasoning_config=reasoning_config,
        request_overrides=request_overrides,
    )
    return kwargs.get("extra_body") or {}


def test_user_template_kwargs_overrides_keep_thinking_off() -> None:
    body = _wire_extra_body(
        {"enabled": False},
        {"extra_body": {"chat_template_kwargs": {"preserve_thinking": True}, "top_k": 20}},
    )
    assert body["chat_template_kwargs"] == {"enable_thinking": False, "preserve_thinking": True}
    assert body["top_k"] == 20


def test_a_user_override_key_wins_per_key() -> None:
    body = _wire_extra_body(
        {"enabled": False},
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}},
    )
    assert body["chat_template_kwargs"]["enable_thinking"] is True
