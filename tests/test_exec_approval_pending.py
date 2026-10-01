"""Durable exec approvals: the request goes in, the decision comes out.

A headless coder process stages a dangerous-command request here and blocks;
whatever chat is relaying it writes the decision. First decision wins, a
missing or expired request can never approve anything, and the notifier stamp
is written once so a poller does not re-send the prompt.
"""

from __future__ import annotations

import time

from tools import write_approval as wa


def test_staged_exec_request_round_trips() -> None:
    record = wa.stage_write(
        wa.EXEC,
        {
            "action": "exec",
            "session_id": "s1",
            "pattern_key": "rm -rf",
            "command": "rm -rf /tmp/x",
        },
        summary="run rm -rf /tmp/x",
        origin="foreground",
        expires_at=time.time() + 300,
        chat_key="agent:main:signal:group:G",
    )
    loaded = wa.get_pending(wa.EXEC, record["id"])
    assert loaded["chat_key"] == "agent:main:signal:group:G"
    assert loaded["payload"]["command"] == "rm -rf /tmp/x"
    assert loaded["expires_at"] > time.time()


def test_first_decision_wins() -> None:
    record = wa.stage_write(
        wa.EXEC, {"action": "exec"}, summary="x", origin="foreground"
    )
    decided = wa.decide_pending(wa.EXEC, record["id"], "session", decided_by="user")
    assert decided["decision"] == "session"

    again = wa.decide_pending(wa.EXEC, record["id"], "deny", decided_by="someone-else")
    assert again["decision"] == "session"
    assert wa.get_pending(wa.EXEC, record["id"])["decided_by"] == "user"


def test_notified_stamp_is_written_once() -> None:
    record = wa.stage_write(
        wa.EXEC, {"action": "exec"}, summary="x", origin="foreground"
    )
    assert wa.mark_pending_notified(wa.EXEC, record["id"]) is True
    first = wa.get_pending(wa.EXEC, record["id"])["notified_at"]

    assert wa.mark_pending_notified(wa.EXEC, record["id"]) is False
    assert wa.get_pending(wa.EXEC, record["id"])["notified_at"] == first


def test_expired_requests_are_pruned_and_dead() -> None:
    record = wa.stage_write(
        wa.EXEC,
        {"action": "exec"},
        summary="x",
        origin="foreground",
        expires_at=time.time() - 1,
    )
    assert wa.prune_expired_pending(wa.EXEC) == 1
    assert wa.get_pending(wa.EXEC, record["id"]) is None


def test_deciding_a_missing_request_returns_none() -> None:
    assert wa.decide_pending(wa.EXEC, "deadbeef", "once") is None
