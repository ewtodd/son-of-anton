"""Relay side of durable approvals: prompt text and chat decisions.

The watcher formats the staged command into the chat, and only the chat that
staged the request may answer it. First answer wins; a later /approve or a
different chat changes nothing.
"""

from __future__ import annotations

import time

from gateway.run import _format_durable_approval
from gateway.slash_commands import GatewaySlashCommandsMixin
from tools import approval, write_approval as wa


def _stage(chat_key: str = "key-a", command: str = "rm -rf /tmp/x") -> dict:
    return wa.stage_write(
        wa.EXEC,
        {
            "action": "exec_approval",
            "command": command,
            "description": "dangerous rm",
        },
        summary="dangerous rm",
        origin="foreground",
        expires_at=time.time() + 300,
        chat_key=chat_key,
    )


def test_prompt_shows_the_command_and_how_to_answer() -> None:
    record = _stage()
    text = _format_durable_approval(record)
    assert "dangerous rm" in text
    assert "rm -rf /tmp/x" in text
    assert f"/approve {record['id']}" in text
    assert f"/deny {record['id']}" in text


def test_only_the_staging_chat_can_answer() -> None:
    record = _stage(chat_key="key-a")
    assert GatewaySlashCommandsMixin._decide_durable_approval(
        "key-b", [record["id"], "session"], deny=False
    ) is None
    assert wa.get_pending(wa.EXEC, record["id"]).get("decision") is None

    reply = GatewaySlashCommandsMixin._decide_durable_approval(
        "key-a", [record["id"], "session"], deny=False
    )
    assert "Approved for this session" in reply
    assert wa.get_pending(wa.EXEC, record["id"])["decision"] == "session"


def test_deny_records_the_reason_and_the_coder_sees_it() -> None:
    record = _stage(chat_key="key-a")
    reply = GatewaySlashCommandsMixin._decide_durable_approval(
        "key-a", [record["id"], "too", "risky"], deny=True
    )
    assert "Denied" in reply
    stored = wa.get_pending(wa.EXEC, record["id"])
    assert stored["decision"] == "deny"
    assert stored["decision_reason"] == "too risky"

    result = approval._apply_approval_decision(
        "key-a",
        stored["payload"].get("pattern_key", "rm"),
        "dangerous rm",
        {"resolved": True, "choice": "deny", "reason": stored["decision_reason"]},
    )
    assert result["approved"] is False
    assert "too risky" in result["message"]


def test_a_second_answer_is_refused() -> None:
    record = _stage(chat_key="key-a")
    first = GatewaySlashCommandsMixin._decide_durable_approval(
        "key-a", [record["id"]], deny=False
    )
    assert "Approved once" in first
    second = GatewaySlashCommandsMixin._decide_durable_approval(
        "key-a", [record["id"], "always"], deny=False
    )
    assert "already answered" in second
    assert wa.get_pending(wa.EXEC, record["id"])["decision"] == "once"
