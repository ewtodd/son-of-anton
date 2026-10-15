"""Durable approvals route a headless coder's gates through the relay chat.

The process has no notifier and no TTY, and its ``-q`` marker would normally
auto-deny under ``approvals.single_query_mode``. Durable mode must instead
stage a request, block, and honor the answer — with timeouts failing closed
and the request consumed exactly once.
"""

from __future__ import annotations

import threading
import time

from tools import approval
from tools import write_approval as wa


def _answer_when_staged(choice: str, timeout: float = 5.0, after: float = 0.0) -> threading.Thread:
    def _worker() -> None:
        time.sleep(after)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = [r for r in wa.list_pending(wa.EXEC) if not r.get("decision")]
            if pending:
                wa.decide_pending(wa.EXEC, pending[0]["id"], choice, decided_by="test")
                return
            time.sleep(0.02)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    return worker


def _durable_env(monkeypatch) -> None:
    monkeypatch.setenv("SON_OF_ANTON_DURABLE_APPROVALS", "1")
    monkeypatch.setattr(approval, "_session_approved", {})
    monkeypatch.setattr(approval, "_DURABLE_APPROVAL_POLL_SECONDS", 0.02)


def test_terminal_gate_relays_an_approval(monkeypatch) -> None:
    _durable_env(monkeypatch)
    # The single-query marker must not auto-deny when a relay can answer.
    monkeypatch.setenv("SON_OF_ANTON_SINGLE_QUERY_SESSION", "1")
    worker = _answer_when_staged("session")
    result = approval.check_all_command_guards(
        "rm -rf /tmp/durable-approval-allow", "local"
    )
    worker.join(timeout=5)
    assert result["approved"] is True
    assert result.get("user_approved") is True


def test_terminal_gate_relays_a_denial(monkeypatch) -> None:
    _durable_env(monkeypatch)
    worker = _answer_when_staged("deny")
    result = approval.check_all_command_guards(
        "rm -rf /tmp/durable-approval-deny", "local"
    )
    worker.join(timeout=5)
    assert result["approved"] is False
    assert "denied by user" in result["message"]
    assert "End your turn" in result["message"], "a denial must stop the agent, not reroute it"


def test_durable_timeout_fails_closed(monkeypatch) -> None:
    """A configured (positive) timeout still fails closed, and stops the agent."""
    _durable_env(monkeypatch)
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 0.2)
    result = approval.check_all_command_guards(
        "rm -rf /tmp/durable-approval-timeout", "local"
    )
    assert result["approved"] is False
    assert "Silence is not consent" in result["message"]
    assert "End your turn" in result["message"]


def test_durable_wait_has_no_deadline_by_default(monkeypatch) -> None:
    """With no timeout (the default) a late answer is still the one honoured."""
    _durable_env(monkeypatch)
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 0)
    worker = _answer_when_staged("session", after=1.5)
    result = approval.check_all_command_guards(
        "rm -rf /tmp/durable-approval-late", "local"
    )
    worker.join(timeout=5)
    assert result["approved"] is True
    assert result.get("user_approved") is True


def test_durable_request_is_consumed_exactly_once(monkeypatch) -> None:
    _durable_env(monkeypatch)
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 0.2)
    approval.check_all_command_guards(
        "rm -rf /tmp/durable-approval-consumed", "local"
    )
    assert wa.list_pending(wa.EXEC) == []


def test_chat_key_falls_back_to_the_bridged_session_key(monkeypatch) -> None:
    """No explicit key: use the SON_OF_ANTON_SESSION_KEY the bridge carried in."""
    monkeypatch.setenv("SON_OF_ANTON_SESSION_KEY", "agent:main:signal:group:G")
    monkeypatch.setattr(approval, "_DURABLE_APPROVAL_POLL_SECONDS", 0.02)
    seen = {}

    def _capture_and_deny() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = [r for r in wa.list_pending(wa.EXEC) if not r.get("decision")]
            if pending:
                seen["chat_key"] = pending[0].get("chat_key")
                wa.decide_pending(wa.EXEC, pending[0]["id"], "deny", decided_by="test")
                return
            time.sleep(0.02)

    worker = threading.Thread(target=_capture_and_deny, daemon=True)
    worker.start()
    approval._await_durable_decision(
        "session-key",
        {
            "command": "rm -rf /tmp/durable-key",
            "description": "dangerous rm",
            "pattern_key": "rm -rf",
            "pattern_keys": ["rm -rf"],
        },
    )
    worker.join(timeout=5)
    assert seen["chat_key"] == "agent:main:signal:group:G"


def test_an_interrupt_ends_the_durable_wait(monkeypatch) -> None:
    """A stopped worker blocked on an approval must unwind, not hang.

    The wait runs on the agent's tool thread, the thread an interrupt flags;
    it resolves as a denial and consumes the request.
    """
    from tools.interrupt import set_interrupt

    _durable_env(monkeypatch)
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 0)  # no deadline
    outcome = {}

    def _wait() -> None:
        outcome["decision"] = approval._await_durable_decision(
            "session-key",
            {"command": "rm -rf /tmp/durable-stop", "description": "dangerous rm",
             "pattern_key": "rm -rf", "pattern_keys": ["rm -rf"]},
        )

    waiter = threading.Thread(target=_wait, daemon=True)
    waiter.start()
    deadline = time.monotonic() + 5
    while not wa.list_pending(wa.EXEC) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert wa.list_pending(wa.EXEC), "the request was staged"
    set_interrupt(True, waiter.ident)
    try:
        waiter.join(timeout=5)
        assert not waiter.is_alive(), "the durable wait ignored the interrupt"
    finally:
        set_interrupt(False, waiter.ident)
    assert outcome["decision"]["choice"] == "deny"
    assert wa.list_pending(wa.EXEC) == []
