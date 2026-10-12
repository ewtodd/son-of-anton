"""The per-iteration token budget charges spend, not re-sent prompt.

The bug: the Manager's budget summed every round's ``prompt_tokens``. A tool
loop re-sends its whole conversation each round, so that sum grows
quadratically with the round count — a 262k budget wound the Manager down
after six to eight rounds of a conversation that had spent a fraction of it.
The budget now charges each prompt token once (the provider's cached-token
count when it reports one, otherwise the growth since the previous round) plus
every output token.
"""

from __future__ import annotations

from types import SimpleNamespace

from autophysicist.llm import new_prompt_tokens, run_agent_loop
from autophysicist.core.tool_call import ToolCall


def _usage(prompt, completion=10, cached=None, details=True):
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    if details:
        usage.prompt_tokens_details = (
            None if cached is None else SimpleNamespace(cached_tokens=cached)
        )
    return usage


# --- the accounting rule -----------------------------------------------------


def test_a_reported_cache_hit_charges_only_the_uncached_part() -> None:
    assert new_prompt_tokens(_usage(10_000, cached=9_200), 9_000) == 800


def test_without_cache_details_the_growth_is_charged() -> None:
    assert new_prompt_tokens(_usage(10_000, details=False), 9_000) == 1_000
    assert new_prompt_tokens(_usage(10_000, cached=None), 9_000) == 1_000


def test_a_placeholder_zero_cache_count_falls_back_to_growth() -> None:
    """Proxies fill prompt_tokens_details with cached_tokens=0 when they know
    nothing; reading that as "nothing was cached" re-creates the bug."""
    assert new_prompt_tokens(_usage(10_000, cached=0), 9_000) == 1_000


def test_a_conversation_that_shrank_is_charged_in_full() -> None:
    """Trimmed to fit the window, or a fresh conversation: the prompt is new."""
    assert new_prompt_tokens(_usage(6_000, details=False), 9_000) == 6_000


def test_a_first_request_is_charged_in_full() -> None:
    assert new_prompt_tokens(_usage(4_000, details=False), 0) == 4_000


# --- through the loop --------------------------------------------------------


class _Config:
    model = "m"
    api_timeout = 5.0
    api_retry_max = 0
    api_retry_initial_delay = 0.0
    workspace_dir = ""
    max_tokens_retries = 0

    def max_tokens_for_agent(self, _name):
        return 1024


class _Executor:
    stop_after_round = False

    def execute(self, name, args):
        return ToolCall(tool_name=name, tool_input=args, output="ok", is_error=False)


def _tool_response(usage):
    call = SimpleNamespace(
        id="c", function=SimpleNamespace(name="noop", arguments="{}")
    )
    message = SimpleNamespace(content="", tool_calls=[call])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        usage=usage,
    )


def _run(monkeypatch, usages):
    replies = iter(_tool_response(u) for u in usages)
    monkeypatch.setattr("autophysicist.llm._resolve_endpoint", lambda c: (None, "m"))
    monkeypatch.setattr(
        "autophysicist.llm._create_with_retry", lambda *a, **k: next(replies)
    )
    seen: list[dict] = []
    result = run_agent_loop(
        system="s",
        user_content="u",
        config=_Config(),
        tool_executor=_Executor(),
        tools=[{"type": "function", "function": {"name": "noop"}}],
        max_rounds=len(usages),
        on_round=lambda *args, **kwargs: seen.append(kwargs),
    )
    return result, seen


def test_a_growing_conversation_is_charged_its_final_size(monkeypatch) -> None:
    prompts = [5_000 + 2_000 * i for i in range(20)]
    result, seen = _run(monkeypatch, [_usage(p, details=False) for p in prompts])
    assert result.total_input_tokens == sum(prompts)
    assert result.total_new_input_tokens == prompts[-1]
    assert seen[-1]["total_new_input"] == prompts[-1]


def test_cache_reports_and_growth_agree(monkeypatch) -> None:
    prompts = [5_000 + 2_000 * i for i in range(10)]
    cached = [0] + prompts[:-1]
    with_cache, _ = _run(
        monkeypatch, [_usage(p, cached=c) for p, c in zip(prompts, cached)]
    )
    without, _ = _run(monkeypatch, [_usage(p, details=False) for p in prompts])
    assert with_cache.total_new_input_tokens == without.total_new_input_tokens


def test_a_reset_mid_loop_charges_the_rewritten_prompt(monkeypatch) -> None:
    prompts = [5_000, 9_000, 4_000, 6_000]
    result, _ = _run(monkeypatch, [_usage(p, details=False) for p in prompts])
    # 5000 + 4000 growth, then a rewrite charges 4000 in full, then +2000.
    assert result.total_new_input_tokens == 5_000 + 4_000 + 4_000 + 2_000


# --- the Manager's wind-down -------------------------------------------------


def test_the_manager_winds_down_on_spend_not_on_resent_prompt(
    monkeypatch, tmp_path
) -> None:
    """Thirty rounds of a conversation that ends at 60k tokens spent: the old
    accounting charged ~1.1M and wound down by round 5 of a 262k budget."""
    from autophysicist.core.config import Config
    from autophysicist.core.metrics import MetricsTracker
    from autophysicist.memory import PermanentMemory, Scratchpad
    from autophysicist.runner import _run_iteration
    from autophysicist.utils.sandbox import SandboxPolicy

    prompts = [6_000 + 1_800 * i for i in range(30)]
    replies = iter(_tool_response(_usage(p, details=False)) for p in prompts)
    requests: list[list[dict]] = []

    def _create(client, model, messages, max_tokens, config, tools=None, **_):
        requests.append([dict(m) for m in messages])
        return next(replies)

    monkeypatch.setattr("autophysicist.llm._resolve_endpoint", lambda c: (None, "m"))
    monkeypatch.setattr("autophysicist.llm._create_with_retry", _create)

    config = Config()
    result, executor = _run_iteration(
        system_prompt="s",
        problem_text="p",
        answer_template="",
        config=config,
        permanent_memory=PermanentMemory(tmp_path),
        scratchpad=Scratchpad(tmp_path),
        workspace_root=tmp_path,
        iteration=1,
        max_iterations=1,
        token_budget=262_144,
        tool_call_cap=1_000,
        max_rounds=30,
        sandbox_timeout=1,
        metrics=MetricsTracker(),
        policy=SandboxPolicy(interpreter="python3"),
    )

    assert result.rounds == 30
    assert executor.tokens_spent < 262_144
    injected = [
        m["content"]
        for m in requests[-1]
        if m["role"] == "user" and "BUDGET" in m["content"]
    ]
    assert injected == [], "no budget warning while spend is under budget"
