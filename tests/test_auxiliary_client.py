"""A task's ``reasoning_effort`` must travel in the endpoint's own wire shape."""

from __future__ import annotations

from agent.auxiliary_client import _build_call_kwargs


def _task_kwargs(**overrides) -> dict:
    call = {
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
    return _build_call_kwargs("qwen3.8-flash-next", [{"role": "user", "content": "summarize"}], **call)


def test_task_reasoning_reaches_the_route_in_its_own_dialect() -> None:
    # The endpoint's wire control is top-level reasoning_effort;
    # auxiliary.compaction.reasoning_effort: none must not go out as an
    # aggregator-shaped extra_body.reasoning the endpoint ignores.
    kwargs = _task_kwargs()
    assert kwargs.get("reasoning_effort") == "none"
    assert "reasoning" not in (kwargs.get("extra_body") or {})


def test_explicit_reasoning_config_wins_over_a_task_block() -> None:
    kwargs = _task_kwargs(reasoning_config={"enabled": True, "effort": "low"})
    assert kwargs.get("reasoning_effort") == "low"
    assert "reasoning" not in (kwargs.get("extra_body") or {})


def test_only_hard_capped_tasks_send_max_tokens() -> None:
    assert _task_kwargs(task="title_generation", max_tokens=64)["max_tokens"] == 64
    assert "max_tokens" not in _task_kwargs(max_tokens=64)
