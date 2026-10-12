"""Tool-call arguments must replay as the bytes the model generated.

A local server (vLLM / llama.cpp) keeps the tokens it generated in its KV
cache; the next request only reuses them if the replayed tool call matches.
Re-serializing with sorted keys and ASCII escapes (μ → \\u03bc) broke that
match at every tool call. Contracts: valid arguments replay verbatim and
deterministically; malformed arguments still become valid JSON, with
non-ASCII text intact.
"""

from __future__ import annotations

import json

from agent.conversation_loop import _canonicalize_api_tool_calls


def _messages(arguments):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "fit", "arguments": arguments},
                }
            ],
        }
    ]


def _wire_args(arguments):
    msgs = _messages(arguments)
    _canonicalize_api_tool_calls(msgs)
    return msgs[0]["tool_calls"][0]["function"]["arguments"]


def test_valid_arguments_replay_verbatim() -> None:
    generated = '{"z": 1, "a": "μ ± σ", "nested": {"b": 2, "a": [1, 2]}}'
    assert _wire_args(generated) == generated


def test_replay_is_deterministic_across_turns() -> None:
    generated = '{"σ":"0.5 ± 0.1","x":[1,2]}'
    assert _wire_args(generated) == _wire_args(generated)


def test_the_stored_history_is_not_rewritten() -> None:
    msgs = _messages('{"a": 1,}')
    stored_fn = msgs[0]["tool_calls"][0]["function"]
    api = [{**msgs[0], "tool_calls": [dict(msgs[0]["tool_calls"][0])]}]
    _canonicalize_api_tool_calls(api)
    assert stored_fn["arguments"] == '{"a": 1,}'


def test_malformed_arguments_become_valid_json() -> None:
    for broken in ('{"a": 1,', '{"a": 1,}', "", "None", '{"path": "x", "content": "unterminated'):
        json.loads(_wire_args(broken))


def test_repaired_arguments_keep_non_ascii_text() -> None:
    # A literal tab inside a string is invalid strict JSON (llama.cpp emits
    # these); the repair must not turn μ into an escape on the way through.
    wire = _wire_args('{"label": "μ\tσ"}')
    assert json.loads(wire) == {"label": "μ\tσ"}
    assert "μ" in wire and "\\u03bc" not in wire
