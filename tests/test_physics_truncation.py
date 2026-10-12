"""A tool loop's final answer is completed, and a sub-agent says why it failed.

``call_llm`` continues a response cut off at the output cap; ``run_agent_loop``
returned the same response half-finished. Its round cap returned ``text=""``.
And ``dispatch_subagent`` reported every one of those — a sub-agent that ran
out of rounds, one whose answer was truncated, and a reasoning model whose
whole output budget went to thinking — as ``no_code``, so the Manager read
"the model chose not to write code" and fixed the wrong thing.
"""

from __future__ import annotations

from types import SimpleNamespace

from autophysicist import subagent as subagent_module
from autophysicist.core.config import Config
from autophysicist.core.tool_call import ToolCall
from autophysicist.llm import (
    CONTINUATION_PROMPT,
    EMPTY_ANSWER_PROMPT,
    run_agent_loop,
)


class _Config:
    model = "m"
    api_timeout = 5.0
    api_retry_max = 0
    api_retry_initial_delay = 0.0
    workspace_dir = ""
    max_tokens_retries = 2

    def max_tokens_for_agent(self, _name):
        return 1024


class _Executor:
    stop_after_round = False

    def execute(self, name, args):
        return ToolCall(tool_name=name, tool_input=args, output="docs", is_error=False)


def _response(content="", tool_calls=None, finish="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def _lookup(n):
    call = SimpleNamespace(
        id=f"c{n}", function=SimpleNamespace(name="lookup", arguments="{}")
    )
    return _response(tool_calls=[call], finish="tool_calls")


TOOLS = [{"type": "function", "function": {"name": "lookup"}}]


def _install(monkeypatch, replies):
    replies = iter(replies)
    requests: list[dict] = []

    def _create(client, model, messages, max_tokens, config, tools=None, tool_choice=None):
        requests.append(
            {
                "messages": [dict(m) for m in messages],
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        return next(replies)

    monkeypatch.setattr("autophysicist.llm._resolve_endpoint", lambda c: (None, "m"))
    monkeypatch.setattr("autophysicist.llm._create_with_retry", _create)
    return requests


def _loop(**kwargs):
    defaults = dict(
        system="s",
        user_content="u",
        config=_Config(),
        tool_executor=_Executor(),
        tools=TOOLS,
        max_rounds=3,
    )
    defaults.update(kwargs)
    return run_agent_loop(**defaults)


# --- run_agent_loop ----------------------------------------------------------


def test_a_truncated_final_answer_is_continued(monkeypatch) -> None:
    requests = _install(
        monkeypatch,
        [
            _lookup(1),
            _response("```python\nprint(", finish="length"),
            _response("1)\n```", finish="stop"),
        ],
    )
    result = _loop()
    assert result.text == "```python\nprint(1)\n```"
    assert result.stop_reason == "end_turn"
    assert result.truncated is False

    continuation = requests[-1]
    assert continuation["messages"][-1]["content"] == CONTINUATION_PROMPT
    assert continuation["tool_choice"] == "none", "a continuation is text, not a call"
    roles = [m["role"] for m in continuation["messages"]]
    assert roles.count("system") == 1 and roles[0] == "system"


def test_a_reasoning_only_reply_is_asked_for_its_answer(monkeypatch) -> None:
    requests = _install(
        monkeypatch,
        [_response("", finish="length"), _response("the answer", finish="stop")],
    )
    result = _loop()
    assert requests[-1]["messages"][-1]["content"] == EMPTY_ANSWER_PROMPT
    assert result.text == "the answer"


def test_still_truncated_after_retries_is_reported_as_such(monkeypatch) -> None:
    _install(
        monkeypatch,
        [_response("a", finish="length")]
        + [_response("b", finish="length") for _ in range(2)],
    )
    result = _loop()
    assert result.stop_reason == "max_tokens"
    assert result.truncated is True
    assert result.text == "abb"


def test_the_round_cap_asks_once_for_the_answer(monkeypatch) -> None:
    requests = _install(
        monkeypatch,
        [_lookup(1), _lookup(2), _lookup(3), _response("final answer")],
    )
    result = _loop(final_answer_on_max_rounds=True)
    assert result.text == "final answer"
    assert result.stop_reason == "max_rounds"
    last = requests[-1]
    assert last["tool_choice"] == "none"
    roles = [m["role"] for m in last["messages"]]
    assert all(
        not (a == b == "user") for a, b in zip(roles, roles[1:])
    ), "never two user turns in a row"


def test_the_round_cap_without_a_final_call_keeps_the_last_words(monkeypatch) -> None:
    said = _lookup(2)
    said.choices[0].message.content = "I found the signature"
    _install(monkeypatch, [_lookup(1), said, _lookup(3)])
    result = _loop()
    assert result.stop_reason == "max_rounds"
    assert result.text == "I found the signature"


# --- dispatch_subagent -------------------------------------------------------


class _Lookups:
    def tools_for(self, role):
        return TOOLS


def _dispatch(monkeypatch, tmp_path, *, loop_result, execute_code=True):
    monkeypatch.setattr(
        subagent_module, "run_agent_loop", lambda **kwargs: loop_result
    )
    return subagent_module.dispatch_subagent(
        system_prompt="s",
        user_message="u",
        execute_code=execute_code,
        config=Config(),
        workspace_root=tmp_path,
        iteration=1,
        mcp=_Lookups(),
    )


def _loop_result(text, stop_reason):
    return SimpleNamespace(
        text=text,
        stop_reason=stop_reason,
        total_input_tokens=1,
        total_new_input_tokens=1,
        total_output_tokens=1,
    )


def test_a_subagent_that_ran_out_of_rounds_says_so(monkeypatch, tmp_path) -> None:
    result = _dispatch(
        monkeypatch, tmp_path, loop_result=_loop_result("", "max_rounds")
    )
    assert result.execution_status == "max_rounds"
    shown = result.format_for_manager()
    assert 'status="max_rounds"' in shown
    assert "tool rounds" in shown


def test_a_truncated_answer_is_not_called_no_code(monkeypatch, tmp_path) -> None:
    result = _dispatch(
        monkeypatch,
        tmp_path,
        loop_result=_loop_result("Here is the script:\n```python\nimport", "max_tokens"),
    )
    assert result.execution_status == "truncated"
    assert 'status="truncated"' in result.format_for_manager()


def test_an_empty_reply_is_not_called_no_code(monkeypatch, tmp_path) -> None:
    result = _dispatch(
        monkeypatch,
        tmp_path,
        loop_result=_loop_result("", "max_tokens"),
        execute_code=False,
    )
    assert result.execution_status == "empty_response"
    assert 'status="empty_response"' in result.format_for_manager()


def test_a_normal_reasoning_dispatch_is_unchanged(monkeypatch, tmp_path) -> None:
    result = _dispatch(
        monkeypatch,
        tmp_path,
        loop_result=_loop_result("the derivation", "end_turn"),
        execute_code=False,
    )
    assert result.execution_status == "no_code"
    assert "subagent_status" not in result.format_for_manager()


def test_a_missing_code_block_is_now_visible_to_the_manager(monkeypatch, tmp_path) -> None:
    result = _dispatch(
        monkeypatch, tmp_path, loop_result=_loop_result("prose only", "end_turn")
    )
    assert result.execution_status == "no_code"
    assert "No Python code block" in result.format_for_manager()
