"""Auxiliary-client contracts — the removed OpenRouter/Nous providers must
never be probed by the auto fallback chain, and a task's ``reasoning_effort``
must travel in the route's own wire shape.
"""

from __future__ import annotations

from agent.auxiliary_client import _build_call_kwargs, _get_provider_chain


def test_auto_chain_excludes_removed_aggregators() -> None:
    labels = [label for label, _ in _get_provider_chain()]
    assert labels
    assert "openrouter" not in labels
    assert "nous" not in labels
    assert "local/custom" in labels
    assert "api-key" in labels


def _task_kwargs(provider: str, **overrides) -> dict:
    call = {
        "provider": provider,
        "model": "qwen3.8-flash-next",
        "messages": [{"role": "user", "content": "summarize"}],
        "temperature": None,
        "max_tokens": None,
        "tools": None,
        "timeout": 300.0,
        "extra_body": {"reasoning": {"enabled": False}},
        "reasoning_config": None,
        "base_url": "http://127.0.0.1:4000/v1",
        "task": "compaction",
    }
    call.update(overrides)
    return _build_call_kwargs(**call)


def test_task_reasoning_reaches_a_custom_route_in_its_own_dialect() -> None:
    # The custom profile's wire control is top-level reasoning_effort. Before
    # the promotion, auxiliary.compaction.reasoning_effort: none went out only
    # as the OpenRouter-shaped extra_body.reasoning and was silently ignored.
    kwargs = _task_kwargs("custom")
    assert kwargs.get("reasoning_effort") == "none"
    assert "reasoning" not in (kwargs.get("extra_body") or {})


def test_profile_less_provider_keeps_the_nested_reasoning_block() -> None:
    kwargs = _task_kwargs("definitely-not-a-registered-provider")
    assert kwargs["extra_body"] == {"reasoning": {"enabled": False}}
    assert "reasoning_effort" not in kwargs


def test_explicit_reasoning_config_wins_over_a_task_block() -> None:
    kwargs = _task_kwargs(
        "custom", reasoning_config={"enabled": True, "effort": "low"}
    )
    assert kwargs.get("reasoning_effort") == "low"
    assert "reasoning" not in (kwargs.get("extra_body") or {})
