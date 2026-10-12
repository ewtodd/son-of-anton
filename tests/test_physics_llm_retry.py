"""How the physics LLM layer classifies failures and bounds its retries.

Two bugs these cover. A 429 whose body said "too many tokens" (a
tokens-per-minute rate limit) matched the context-overflow markers before the
status was looked at, so a request that a short wait would have fixed was
treated as too big for the model. And the retry budget was unbounded in
practice: the OpenAI SDK's own retries nested inside ours, the configured
``api_retry_max_delay`` was never read, and nothing limited the wall clock of
one call across its attempts.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from autophysicist import llm
from autophysicist.llm import ContextTooLongError, _create_with_retry, _raise_if_context_error


def _status_error(cls, status: int, message: str):
    request = httpx.Request("POST", "http://test.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": message}})
    return cls(message, response=response, body={"error": {"message": message}})


class _Config:
    model = "m"
    api_timeout = 30.0
    api_retry_max = 5
    api_retry_initial_delay = 10.0
    api_retry_max_delay = 15.0
    api_retry_total_timeout = 0.0
    reasoning_effort = ""


class _FlakyClient:
    """Fails with *errors* in order, then succeeds."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls: list[dict] = []

        outer = self

        class _Completions:
            @staticmethod
            def create(**kwargs):
                outer.calls.append(kwargs)
                if outer.errors:
                    raise outer.errors.pop(0)
                return "ok"

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def test_a_rate_limit_that_mentions_tokens_is_not_an_overflow() -> None:
    exc = _status_error(
        openai.RateLimitError, 429, "Rate limit: too many tokens per minute"
    )
    _raise_if_context_error(exc)  # must not raise
    assert llm.is_transient(exc)


def test_an_overflow_carries_the_window_and_prompt_size() -> None:
    exc = _status_error(
        openai.BadRequestError,
        400,
        "This model's maximum context length is 131072 tokens. However, you "
        "requested 150000 tokens (140000 in the messages, 10000 in the completion).",
    )
    with pytest.raises(ContextTooLongError) as caught:
        _raise_if_context_error(exc)
    assert caught.value.limit == 131072
    assert caught.value.input_tokens == 140000


def test_a_rate_limited_call_is_retried_not_reported_as_too_long(monkeypatch) -> None:
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    client = _FlakyClient(
        [_status_error(openai.RateLimitError, 429, "too many tokens per minute")]
    )
    assert _create_with_retry(client, "m", [], 10, _Config()) == "ok"
    assert len(client.calls) == 2


def test_backoff_never_sleeps_longer_than_the_configured_max(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    errors = [
        _status_error(openai.InternalServerError, 503, "busy") for _ in range(4)
    ]
    client = _FlakyClient(errors)
    _create_with_retry(client, "m", [], 10, _Config())
    assert slept, "transient errors must back off"
    assert max(slept) <= _Config.api_retry_max_delay


def test_the_total_timeout_bounds_every_attempt_and_the_retries(monkeypatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock["t"])

    def _sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(llm.time, "sleep", _sleep)

    config = _Config()
    config.api_retry_max = 50
    config.api_retry_total_timeout = 40.0
    errors = [
        _status_error(openai.InternalServerError, 503, "busy") for _ in range(50)
    ]
    client = _FlakyClient(errors)
    with pytest.raises(openai.InternalServerError):
        _create_with_retry(client, "m", [], 10, config)

    assert clock["t"] - 1000.0 <= config.api_retry_total_timeout
    assert len(client.calls) < 50, "the deadline, not the attempt count, stopped it"
    assert all(c["timeout"] <= config.api_timeout for c in client.calls)


def test_the_sdk_does_not_retry_underneath_us(monkeypatch) -> None:
    monkeypatch.setattr(
        llm,
        "_load_agent_config",
        lambda: {"physics": {"base_url": "http://127.0.0.1:9/v1"}},
    )
    client, _ = llm._resolve_endpoint(_Config())
    assert client.max_retries == 0
