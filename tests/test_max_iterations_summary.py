"""Contracts for the max-iterations wrap-up summary.

The wrap-up hand-builds its request instead of going through the transport,
so it must apply the same profile reasoning dialect as every other call, and
the OpenAI-compatible request must actually be sent when extra_body is
non-empty (the anthropic_messages removal left an else dangling on that
condition and skipped the call).
"""

from __future__ import annotations

import types

import pytest

from agent.chat_completion_helpers import handle_max_iterations


class _StubTransport:
    def normalize_response(self, response):
        return types.SimpleNamespace(content=response.choices[0].message.content)


class _StubCompletions:
    def __init__(self, agent):
        self.agent = agent

    def create(self, **kwargs):
        self.agent.created.append(kwargs)
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content="wrap-up")
                )
            ],
            usage=None,
        )


class _StubClient:
    def __init__(self, agent):
        self.chat = types.SimpleNamespace(completions=_StubCompletions(agent))


class _SummaryAgent:
    """The narrow AIAgent surface ``handle_max_iterations`` touches."""

    def __init__(self, provider="custom", reasoning_config=None):
        self.model = "qwen3.8-27b"
        self.provider = provider
        self.base_url = "http://127.0.0.1:4000/v1"
        self.api_mode = "chat_completions"
        self.max_tokens = None
        self.max_iterations = 3
        self.reasoning_config = reasoning_config
        self._cached_system_prompt = "system"
        self.ephemeral_system_prompt = ""
        self.prefill_messages = []
        self.providers_allowed = None
        self.providers_ignored = None
        self.providers_order = None
        self.provider_sort = None
        self.provider_require_parameters = False
        self.provider_data_collection = None
        self.openrouter_min_coding_score = None
        self.extra_body_supported = False
        self.created: list[dict] = []

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _should_sanitize_tool_calls(self):
        return False

    def _copy_reasoning_content_for_api(self, source, target):
        pass

    def _sanitize_api_messages(self, messages):
        return messages

    def _drop_thinking_only_and_merge_users(self, messages):
        return messages

    def _supports_reasoning_extra_body(self):
        return self.extra_body_supported

    def _is_openrouter_url(self):
        return self.provider == "openrouter"

    def _ensure_primary_openai_client(self, reason=""):
        return _StubClient(self)

    def _get_transport(self):
        return _StubTransport()


@pytest.fixture(autouse=True)
def _stub_relay(monkeypatch):
    from agent import relay_llm

    monkeypatch.setattr(
        relay_llm,
        "execute_current",
        lambda request, callback, **kwargs: callback(request),
    )
    monkeypatch.setattr(relay_llm, "complete_logical_call", lambda *a, **k: None)


def _user_turn():
    return [{"role": "user", "content": "keep going"}]


def test_custom_route_summary_gets_the_session_reasoning_effort() -> None:
    # The custom profile's control is top-level reasoning_effort; the
    # wrap-up bypasses the transport and must not lose it.
    agent = _SummaryAgent(
        "custom", {"enabled": True, "effort": "medium"}
    )
    result = handle_max_iterations(agent, _user_turn(), 3)
    assert result == "wrap-up"
    assert len(agent.created) == 1
    assert agent.created[0]["reasoning_effort"] == "medium"
    assert "reasoning" not in (agent.created[0].get("extra_body") or {})


def test_nonempty_extra_body_still_sends_the_request() -> None:
    # Regression guard: the removed anthropic_messages branch left the
    # OpenAI-compatible call in an else on `if summary_extra_body`, so an
    # OpenRouter-style nested reasoning block skipped it entirely.
    agent = _SummaryAgent(
        "openrouter", {"enabled": True, "effort": "low"}
    )
    agent.extra_body_supported = True
    result = handle_max_iterations(agent, _user_turn(), 3)
    assert result == "wrap-up"
    assert len(agent.created) == 1
    assert agent.created[0]["extra_body"]["reasoning"] == {
        "enabled": True,
        "effort": "low",
    }
