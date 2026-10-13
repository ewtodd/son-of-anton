"""A gateway's "model not loaded" 503 is a routing answer, not an overload.

maitred fronts servers that share GPUs and run one set of models at a
time. A request for a model of the other set gets 503 with
``code: model_not_loaded`` and a message naming what to use instead; it
adds ``Retry-After`` only when waiting helps (the wanted set is loading or
the host is switching). Contracts, through the real OpenAI SDK error path:

- without Retry-After: not retried, fallback allowed, and the classified
  message is the gateway's own text;
- with Retry-After: retried like any 503;
- a plain 503 is unaffected.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from agent.error_classifier import classify_api_error

GATEWAY = "http://gateway.invalid:4002/v1"
MESSAGE = (
    "Qwen3.8-27B (radiance/qwen3.8-27b) is not loaded: son-of-anton is serving "
    "DeepSeek-V4-Flash right now. Use DeepSeek-V4-Flash (unholy/dsv4-flash) instead, "
    "or ask the admin to switch son-of-anton to the Qwen models (llm-mode)."
)


def _sdk_error(body: dict, headers: dict | None = None) -> openai.APIStatusError:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json=body, headers=headers or {})

    client = openai.OpenAI(
        base_url=GATEWAY,
        api_key="sk-test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(openai.APIStatusError) as caught:
        client.chat.completions.create(
            model="radiance/qwen3.8-27b",
            messages=[{"role": "user", "content": "hi"}],
        )
    return caught.value


def _gateway_body() -> dict:
    return {
        "error": {
            "message": MESSAGE,
            "type": "model_unavailable",
            "code": "model_not_loaded",
        }
    }


def test_other_mode_serving_is_not_retried_and_keeps_the_message() -> None:
    classified = classify_api_error(_sdk_error(_gateway_body()), provider="custom")
    assert classified.retryable is False
    assert classified.should_fallback is True
    assert classified.message == MESSAGE


def test_a_retry_after_means_wait_and_retry() -> None:
    classified = classify_api_error(
        _sdk_error(_gateway_body(), headers={"Retry-After": "60"}), provider="custom"
    )
    assert classified.retryable is True


def test_a_plain_503_is_still_retried() -> None:
    body = {"error": {"message": "Service Unavailable", "type": "server_error"}}
    classified = classify_api_error(_sdk_error(body), provider="custom")
    assert classified.retryable is True
