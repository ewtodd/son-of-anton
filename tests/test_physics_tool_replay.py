"""What ``run_agent_loop`` replays to the model after a tool round.

Three faults in the replayed assistant turn. Its reasoning was dropped, so
Qwen3's template (which renders ``reasoning_content`` for the turns after the
last user message) lost the model's interleaved reasoning and the server's
prefix cache missed every round. Its tool-call arguments were replayed
verbatim even when a truncated response left them invalid JSON — a string
the next request's template cannot parse. And a call whose arguments did not
parse was executed anyway, with ``{}``, so the model never learned its call
had been cut off.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from autophysicist.llm import run_agent_loop
from autophysicist.core.tool_call import ToolCall


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
    def __init__(self):
        self.stop_after_round = False
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name, args):
        self.calls.append((name, args))
        return ToolCall(tool_name=name, tool_input=args, output="ok", is_error=False)


def _call(name, arguments, call_id="c1"):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def _response(message, finish="tool_calls"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def _final():
    return _response(SimpleNamespace(content="done", tool_calls=None), "stop")


def _run(monkeypatch, first):
    """One tool round from *first*, then a plain answer. Returns the second
    request's messages and the executor."""
    replies = iter([first, _final()])
    requests: list[list[dict]] = []

    def _create(client, model, messages, max_tokens, config, tools=None, **_):
        requests.append(json.loads(json.dumps(messages)))
        return next(replies)

    monkeypatch.setattr("autophysicist.llm._resolve_endpoint", lambda c: (None, "m"))
    monkeypatch.setattr("autophysicist.llm._create_with_retry", _create)
    executor = _Executor()
    run_agent_loop(
        system="s",
        user_content="u",
        config=_Config(),
        tool_executor=executor,
        tools=[{"type": "function", "function": {"name": "write"}}],
        max_rounds=3,
    )
    return requests[1], executor


def _assistant_turn(messages):
    return next(m for m in messages if m["role"] == "assistant")


def test_reasoning_is_replayed_on_the_tool_call_turn(monkeypatch) -> None:
    message = SimpleNamespace(
        content="",
        tool_calls=[_call("write", '{"x": 1}')],
        reasoning_content="first I should write x",
    )
    second, _ = _run(monkeypatch, _response(message))
    assert _assistant_turn(second)["reasoning_content"] == "first I should write x"


def test_reasoning_under_the_newer_field_name_is_replayed_too(monkeypatch) -> None:
    message = SimpleNamespace(
        content="", tool_calls=[_call("write", "{}")], reasoning="thinking"
    )
    second, _ = _run(monkeypatch, _response(message))
    assert _assistant_turn(second)["reasoning_content"] == "thinking"


def test_no_reasoning_adds_no_field(monkeypatch) -> None:
    message = SimpleNamespace(content="", tool_calls=[_call("write", "{}")])
    second, _ = _run(monkeypatch, _response(message))
    assert "reasoning_content" not in _assistant_turn(second)


def test_a_truncated_call_is_not_executed_and_the_model_is_told(monkeypatch) -> None:
    message = SimpleNamespace(
        content="", tool_calls=[_call("write", '{"content": "half of a resu')]
    )
    second, executor = _run(monkeypatch, _response(message, finish="length"))

    assert executor.calls == [], "a cut-off call must not run with {}"
    tool_turn = next(m for m in second if m["role"] == "tool")
    assert tool_turn["content"].startswith("ERROR")
    assert "NOT executed" in tool_turn["content"]
    replayed = _assistant_turn(second)["tool_calls"][0]["function"]["arguments"]
    assert isinstance(json.loads(replayed), dict), "replay must be valid JSON"


def test_invalid_arguments_are_rejected_without_truncation_too(monkeypatch) -> None:
    message = SimpleNamespace(content="", tool_calls=[_call("write", "[1, 2]")])
    second, executor = _run(monkeypatch, _response(message))
    assert executor.calls == []
    replayed = _assistant_turn(second)["tool_calls"][0]["function"]["arguments"]
    assert isinstance(json.loads(replayed), dict)


def test_empty_arguments_on_a_complete_response_mean_no_arguments(monkeypatch) -> None:
    message = SimpleNamespace(content="", tool_calls=[_call("end_turn", "")])
    second, executor = _run(monkeypatch, _response(message))
    assert executor.calls == [("end_turn", {})]
    replayed = _assistant_turn(second)["tool_calls"][0]["function"]["arguments"]
    assert json.loads(replayed) == {}


def test_valid_arguments_are_replayed_exactly(monkeypatch) -> None:
    """Re-serialising valid arguments would change the bytes the server
    generated and cost the prefix cache for nothing."""
    raw = '{"content":  "spacing kept"}'
    message = SimpleNamespace(content="", tool_calls=[_call("write", raw)])
    second, executor = _run(monkeypatch, _response(message))
    assert executor.calls == [("write", {"content": "spacing kept"})]
    assert _assistant_turn(second)["tool_calls"][0]["function"]["arguments"] == raw


def test_a_missing_call_id_gets_a_stable_one_paired_with_its_result(
    monkeypatch,
) -> None:
    def ids():
        message = SimpleNamespace(
            content="",
            tool_calls=[_call("write", "{}", None), _call("write", "{}", None)],
        )
        second, _ = _run(monkeypatch, _response(message))
        assistant = [c["id"] for c in _assistant_turn(second)["tool_calls"]]
        results = [m["tool_call_id"] for m in second if m["role"] == "tool"]
        return assistant, results

    assistant, results = ids()
    assert all(assistant) and len(set(assistant)) == 2
    assert assistant == results
    assert ids() == (assistant, results), "ids are deterministic"
