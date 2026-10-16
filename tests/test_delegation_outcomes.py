"""delegate_task child-outcome contracts.

A delegated child is driven through the real ``_run_single_child`` with a stub
in place of the agent (no network). Three behaviors are pinned:

  * the reported ``status`` is ``completed`` only when the child's own turn
    produced an answer — a turn ``run_conversation`` marks failed, partial or
    interrupted is never reported as completed, and its error text is never
    passed off as the child's summary;
  * the single output-schema retry continues the child's own conversation
    (it sees the transcript of the turn being corrected), runs only after a
    turn that produced an answer, and a failed retry never replaces that
    answer;
  * every child turn — the task and the retry — runs under the same harness:
    a worker thread with the subagent approval callback installed, inside the
    delegated-child context.
"""

from __future__ import annotations

import copy
import json
import threading

import pytest

from tools import delegate_tool


class _StubChild:
    """Stands in for AIAgent: replays canned run_conversation results."""

    def __init__(self, *results, schema=None):
        self._results = list(results)
        self.calls: list = []
        self.closed = False
        if schema is not None:
            self._delegate_output_schema = schema

    def run_conversation(self, user_message, conversation_history=None, task_id=None,
                         stream_callback=None):
        from agent.delegation_context import is_delegated_child_context
        from tools.terminal_tool import _get_approval_callback

        self.calls.append({
            "user_message": user_message,
            "conversation_history": copy.deepcopy(conversation_history),
            "thread": threading.current_thread(),
            "approval_callback": _get_approval_callback(),
            "delegated_context": is_delegated_child_context(),
        })
        return self._results.pop(0)

    def get_activity_summary(self):
        return {}

    def close(self):
        self.closed = True


def _run(child, goal="compute 17*23"):
    return delegate_tool._run_single_child(0, goal, child, None)


def _turn(final, *, messages=None, **flags):
    result = {"final_response": final, "messages": messages or [], "api_calls": 1}
    result.update(flags)
    return result


_API_FAILURE = _turn(
    "API call failed after 3 retries: Non-streaming API call timed out after 180s",
    completed=False, failed=True, error="Non-streaming API call timed out after 180s",
)
_TRUNCATED = _turn(
    "The answer is 39", completed=False, partial=True,
    error="Response remained truncated after 4 continuation attempts",
)
_DEFERRED = _turn(
    "Context compaction is running; retry in a moment.", completed=False,
    partial=True, failed=False,
    error="Context compaction is running; retry in a moment.",
)
_STOPPED = _turn("Operation interrupted.", completed=False, interrupted=True)
_EMPTY = _turn("(empty)", completed=True)
_FINISHED = _turn("391", completed=True)
_OUT_OF_ITERATIONS = _turn("Partial: computed 391, did not verify", completed=False)


@pytest.mark.parametrize(
    "result",
    [_API_FAILURE, _TRUNCATED, _DEFERRED, _STOPPED, _EMPTY, _FINISHED, _OUT_OF_ITERATIONS],
    ids=["api-failure", "truncated", "deferred", "stopped", "empty", "finished", "out-of-iterations"],
)
def test_completed_only_when_the_childs_turn_produced_an_answer(result) -> None:
    entry = _run(_StubChild(copy.deepcopy(result)))

    turn_without_answer = bool(
        result.get("failed") or result.get("partial") or result.get("interrupted")
    ) or result["final_response"].strip() == "(empty)"
    assert (entry["status"] == "completed") == (not turn_without_answer)

    if result.get("failed") or result.get("partial"):
        # The parent can tell failure apart from truncated-but-summarised work.
        assert entry["status"] == "failed"
        assert entry["exit_reason"] == "failed"
        assert entry["truncated"] is False
        assert entry["error"]
        # Loop error text is reported as the error, never as the summary.
        assert entry["summary"] != entry["error"]
        assert not (entry["summary"] or "").startswith("API call failed")
    if entry["status"] == "completed":
        assert entry["summary"] == result["final_response"]
        assert entry["truncated"] == (not result.get("completed"))
    # The entry must serialise for the parent (no stray objects).
    json.dumps({k: v for k, v in entry.items() if not k.startswith("_")})


def test_truncated_partial_output_is_kept_as_partial_summary() -> None:
    entry = _run(_StubChild(copy.deepcopy(_TRUNCATED)))
    assert entry["status"] == "failed"
    assert entry["summary"] == _TRUNCATED["final_response"]
    assert entry["error"] == _TRUNCATED["error"]


_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
}


def _transcript(*pairs):
    messages = []
    for role, content in pairs:
        messages.append({"role": role, "content": content})
    return messages


def test_schema_retry_continues_the_childs_own_conversation() -> None:
    first_messages = _transcript(
        ("user", "compute 17*23"),
        ("assistant", "391"),
    )
    first = _turn("391", completed=True, messages=first_messages)
    retry = _turn(
        '{"answer": 391}', completed=True,
        messages=first_messages + _transcript(("user", "<retry>"), ("assistant", '{"answer": 391}')),
    )
    expected_api_calls = first["api_calls"] + retry["api_calls"]
    child = _StubChild(first, retry, schema=_SCHEMA)

    entry = _run(child)

    assert len(child.calls) == 2, "exactly one retry"
    # The retry turn sees the transcript of the answer it is correcting.
    assert child.calls[1]["conversation_history"] == first_messages
    assert entry["status"] == "completed"
    assert entry["schema_valid"] is True
    assert entry["schema_retries"] == 1
    assert json.loads(entry["summary"]) == {"answer": 391}
    assert entry["api_calls"] == expected_api_calls


@pytest.mark.parametrize("result", [_API_FAILURE, _TRUNCATED, _STOPPED],
                         ids=["api-failure", "truncated", "stopped"])
def test_a_turn_without_an_answer_is_never_schema_retried(result) -> None:
    child = _StubChild(copy.deepcopy(result), schema=_SCHEMA)
    entry = _run(child)
    assert len(child.calls) == 1
    assert entry["status"] != "completed"
    assert "schema_retries" not in entry


@pytest.mark.parametrize("retry", [_API_FAILURE, _TRUNCATED], ids=["api-failure", "truncated"])
def test_a_failed_retry_keeps_the_childs_answer(retry) -> None:
    first = _turn("391", completed=True, messages=_transcript(("user", "go"), ("assistant", "391")))
    child = _StubChild(first, copy.deepcopy(retry), schema=_SCHEMA)
    entry = _run(child)
    assert len(child.calls) == 2
    assert entry["status"] == "completed"
    assert entry["summary"] == "391"
    assert entry["schema_valid"] is False


def test_a_retry_stopped_midway_reports_the_interruption() -> None:
    first = _turn("391", completed=True, messages=_transcript(("user", "go"), ("assistant", "391")))
    child = _StubChild(first, copy.deepcopy(_STOPPED), schema=_SCHEMA)
    entry = _run(child)
    assert entry["status"] == "interrupted"
    assert entry["summary"] == "391"


def test_every_child_turn_runs_under_the_subagent_harness() -> None:
    first = _turn("391", completed=True, messages=_transcript(("user", "go"), ("assistant", "391")))
    retry = _turn('{"answer": 391}', completed=True)
    child = _StubChild(first, retry, schema=_SCHEMA)
    expected_callback = delegate_tool._subagent_auto_deny

    _run(child)

    assert len(child.calls) == 2
    for call in child.calls:
        assert call["thread"] is not threading.current_thread()
        assert call["approval_callback"] is expected_callback
        assert call["delegated_context"] is True
