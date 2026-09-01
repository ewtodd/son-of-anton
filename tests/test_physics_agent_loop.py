"""``run_agent_loop`` honours the executor protocol the executors depend on.

The bug these cover: the fork's ``run_agent_loop`` ignored ``active_tools``,
``stop_after_round`` and ``end_round()`` — all three documented in
``ManagerToolExecutor``'s own docstring and all three load-bearing. The visible
consequences were that ``end_turn`` did not end the turn, ``submit_final_answer``
did not terminate the run, the per-iteration token budget never wound anything
down, and the computer agent could call ``execute_python`` before documenting
an approach. Every iteration instead ran to ``max_rounds``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from physics_intern.llm import run_agent_loop
from physics_intern.state.tool_call import ToolCall


class _Config:
    model = "test-model"
    api_timeout = 5.0
    api_retry_max = 0
    api_retry_initial_delay = 0.0
    workspace_dir = ""

    def max_tokens_for_agent(self, _name):
        return 1024


def _message(tool_calls=None, content=""):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _tool_call(call_id, name, arguments="{}"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(message, finish_reason="tool_calls"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class _Executor:
    """Minimal executor implementing the documented duck-type protocol."""

    def __init__(self, stop_on=None, tool_sets=None, end_round_message=None):
        self.stop_on = stop_on
        self.stop_after_round = False
        self.calls: list[str] = []
        self.tools_seen: list[list[dict]] = []
        self._tool_sets = tool_sets
        self._end_round_message = end_round_message
        self.end_round_calls = 0

    @property
    def active_tools(self):
        if self._tool_sets is None:
            return None
        index = min(len(self.calls), len(self._tool_sets) - 1)
        return self._tool_sets[index]

    def end_round(self):
        self.end_round_calls += 1
        return self._end_round_message

    def execute(self, tool_name, tool_input):
        self.calls.append(tool_name)
        if tool_name == self.stop_on:
            self.stop_after_round = True
        return ToolCall(
            tool_name=tool_name, tool_input=tool_input, output="ok", is_error=False
        )


@pytest.fixture
def capture(monkeypatch):
    """Stub the endpoint and record every request the loop makes."""
    requests: list[dict] = []

    monkeypatch.setattr(
        "physics_intern.llm._resolve_endpoint", lambda config: (None, "test-model")
    )

    def _create(client, model, messages, max_tokens, config, tools=None):
        requests.append({"messages": [dict(m) for m in messages], "tools": tools})
        # Always ask for one tool call; the loop is what must decide to stop.
        return _response(_message(tool_calls=[_tool_call(f"c{len(requests)}", "end_turn")]))

    monkeypatch.setattr("physics_intern.llm._create_with_retry", _create)
    return requests


def test_exit_tool_stops_the_loop(capture) -> None:
    executor = _Executor(stop_on="end_turn")
    result = run_agent_loop(
        system="s",
        user_content="u",
        config=_Config(),
        tool_executor=executor,
        tools=[{"type": "function", "function": {"name": "end_turn"}}],
        max_rounds=10,
    )
    assert result.rounds == 1, "end_turn must end the turn, not run to max_rounds"
    assert result.stop_reason == "exit_tool"
    assert executor.calls == ["end_turn"]


def test_without_an_exit_signal_the_loop_still_bounds_itself(capture) -> None:
    executor = _Executor(stop_on=None)
    result = run_agent_loop(
        system="s",
        user_content="u",
        config=_Config(),
        tool_executor=executor,
        tools=[{"type": "function", "function": {"name": "end_turn"}}],
        max_rounds=3,
    )
    assert result.rounds == 3
    assert result.stop_reason == "max_rounds"


def test_active_tools_gates_each_round(capture) -> None:
    initial = [{"type": "function", "function": {"name": "document_approach"}}]
    after = [{"type": "function", "function": {"name": "execute_python"}}]
    executor = _Executor(tool_sets=[initial, after, after], stop_on=None)
    run_agent_loop(
        system="s",
        user_content="u",
        config=_Config(),
        tool_executor=executor,
        tools=[{"type": "function", "function": {"name": "never_used"}}],
        max_rounds=2,
    )
    assert [r["tools"] for r in capture] == [initial, after]


def test_end_round_message_is_injected_as_a_user_turn(capture) -> None:
    executor = _Executor(stop_on=None, end_round_message="BUDGET WARNING")
    run_agent_loop(
        system="s",
        user_content="u",
        config=_Config(),
        tool_executor=executor,
        tools=[{"type": "function", "function": {"name": "end_turn"}}],
        max_rounds=2,
    )
    assert executor.end_round_calls == 2
    second_request = capture[1]["messages"]
    assert second_request[-1] == {"role": "user", "content": "BUDGET WARNING"}


def test_end_round_can_force_the_loop_to_stop(capture) -> None:
    class _HardStop(_Executor):
        def end_round(self):
            self.end_round_calls += 1
            self.stop_after_round = True
            return "HARD BUDGET LIMIT"

    executor = _HardStop(stop_on=None)
    result = run_agent_loop(
        system="s",
        user_content="u",
        config=_Config(),
        tool_executor=executor,
        tools=[{"type": "function", "function": {"name": "end_turn"}}],
        max_rounds=10,
    )
    assert result.rounds == 1
    assert result.stop_reason == "exit_tool"


def test_truncated_final_response_is_flagged(monkeypatch) -> None:
    monkeypatch.setattr(
        "physics_intern.llm._resolve_endpoint", lambda config: (None, "test-model")
    )
    monkeypatch.setattr(
        "physics_intern.llm._create_with_retry",
        lambda *a, **k: _response(_message(content="half a sent"), finish_reason="length"),
    )
    result = run_agent_loop(
        system="s",
        user_content="u",
        config=_Config(),
        tool_executor=_Executor(),
        tools=[],
        max_rounds=3,
    )
    assert result.truncated is True
    assert result.stop_reason == "max_tokens"


# --- request shape -----------------------------------------------------------


def test_a_truncated_one_shot_continues_without_duplicating_the_system_turn(
    monkeypatch,
) -> None:
    """[system, system, user, ...] is rejected: "System message must be at the
    beginning". call_llm passed the whole message list to the continuation
    helper, which prepends its own system turn — so every truncated one-shot
    response failed instead of continuing."""
    from physics_intern.llm import call_llm

    seen: list[list[dict]] = []
    replies = iter(
        [
            _response(_message(content="half a sent"), finish_reason="length"),
            _response(_message(content="ence."), finish_reason="stop"),
        ]
    )

    monkeypatch.setattr(
        "physics_intern.llm._resolve_endpoint", lambda config: (None, "m")
    )

    def _create(client, model, messages, max_tokens, config, tools=None):
        seen.append([dict(m) for m in messages])
        return next(replies)

    monkeypatch.setattr("physics_intern.llm._create_with_retry", _create)

    config = _Config()
    config.max_tokens_retries = 1
    result = call_llm("SYSTEM", "question", config)

    continuation = seen[1]
    assert [m["role"] for m in continuation].count("system") == 1
    assert continuation[0]["role"] == "system"
    assert result.text == "half a sentence."


def test_reasoning_effort_is_sent_only_when_configured(monkeypatch) -> None:
    """A thinking model's default effort is its highest."""
    from physics_intern.llm import call_llm

    seen: list[dict] = []

    monkeypatch.setattr(
        "physics_intern.llm._resolve_endpoint", lambda config: (None, "m")
    )

    class _Client:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    seen.append(kwargs)
                    return _response(_message(content="ok"), finish_reason="stop")

    monkeypatch.setattr(
        "physics_intern.llm._resolve_endpoint", lambda config: (_Client(), "m")
    )

    config = _Config()
    call_llm("s", "u", config)
    assert "reasoning_effort" not in seen[-1]

    config.reasoning_effort = "medium"
    call_llm("s", "u", config)
    assert seen[-1]["reasoning_effort"] == "medium"


def test_a_malformed_request_is_not_reported_as_context_too_long() -> None:
    """Every 400 used to become ContextTooLongError, so the loops responded by
    compacting a request that was never too big and never saw the real fault."""
    from physics_intern.llm import ContextTooLongError, _raise_if_context_error

    class _Resp:
        status_code = 400

        def json(self):
            return {"error": {"message": "System message must be at the beginning."}}

    class _Bad(Exception):
        status_code = 400
        response = _Resp()

        def __str__(self):
            return "System message must be at the beginning."

    _raise_if_context_error(_Bad())  # must not raise

    class _TooLong(_Bad):
        def __str__(self):
            return "This model's maximum context length is 131072 tokens"

    with pytest.raises(ContextTooLongError):
        _raise_if_context_error(_TooLong())


def test_every_round_is_logged(monkeypatch, tmp_path) -> None:
    """The Manager runs entirely through this loop and logged nothing.

    A run's EVENT_LOG.jsonl showed only its sub-agents, so where the Manager's
    time actually went — and questions like "was that iteration faster because
    of the model or because the prefix was cached" — had no data behind them.
    """
    import json

    monkeypatch.setattr(
        "physics_intern.llm._resolve_endpoint", lambda config: (None, "test-model")
    )
    replies = iter(
        [
            _response(_message(tool_calls=[_tool_call("c1", "end_turn")])),
            _response(_message(content="done"), finish_reason="stop"),
        ]
    )
    monkeypatch.setattr(
        "physics_intern.llm._create_with_retry",
        lambda *a, **k: next(replies),
    )

    config = _Config()
    config.workspace_dir = str(tmp_path)

    run_agent_loop(
        system="s",
        user_content="u",
        config=config,
        tool_executor=_Executor(),
        tools=[{"type": "function", "function": {"name": "end_turn"}}],
        max_rounds=5,
        agent_name="manager",
        iteration=3,
    )

    entries = [
        json.loads(line)
        for line in (tmp_path / "EVENT_LOG.jsonl").read_text().splitlines()
    ]
    assert len(entries) == 2, "one entry per round"
    assert [e["round"] for e in entries] == [1, 2]
    assert all(e["agent"] == "manager" and e["iter"] == 3 for e in entries)
    assert all(e["duration_s"] >= 0 for e in entries)
