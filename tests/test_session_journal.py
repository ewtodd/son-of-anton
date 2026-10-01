"""The per-session journal mirrors persisted rows as readable JSONL.

The session DB is the source of truth; the journal is the plain-file view a
human or the model can grep. It is written incrementally as rows are
persisted, self-describing via a header, and append-only.
"""

from __future__ import annotations

import json

from agent.session_journal import append_session_journal, journal_path


def _read_lines(session_id: str) -> list:
    path = journal_path(session_id)
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_journal_writes_a_header_then_message_rows() -> None:
    rows = [
        {"role": "user", "content": "hello", "timestamp": 1.0},
        {
            "role": "tool",
            "tool_name": "read_file",
            "tool_call_id": "call_1",
            "content": "full tool output",
            "timestamp": 2.0,
        },
    ]
    append_session_journal(
        "20261001_000000_abcdef",
        rows,
        session_header={"cwd": "/home/e-play/Software/son-of-anton", "model": "qwen"},
    )
    lines = _read_lines("20261001_000000_abcdef")
    assert lines[0]["type"] == "session"
    assert lines[0]["cwd"] == "/home/e-play/Software/son-of-anton"
    assert lines[0]["model"] == "qwen"
    assert [line["type"] for line in lines[1:]] == ["message", "message"]
    assert lines[1]["content"] == "hello"
    assert lines[2]["tool_name"] == "read_file"
    assert lines[2]["content"] == "full tool output"
    assert lines[2]["tool_call_id"] == "call_1"


def test_second_append_keeps_one_header() -> None:
    append_session_journal("s1", [{"role": "user", "content": "a"}])
    append_session_journal("s1", [{"role": "assistant", "content": "b"}])
    lines = _read_lines("s1")
    assert [line["type"] for line in lines] == ["session", "message", "message"]
    assert [line.get("content") for line in lines[1:]] == ["a", "b"]


def test_absent_rows_never_create_a_file() -> None:
    append_session_journal("s1", [])
    assert not journal_path("s1").exists()
