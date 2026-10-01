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


def _answer_when_staged(choice: str, timeout: float = 5.0) -> threading.Thread:
    def _worker() -> None:
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


def test_durable_timeout_fails_closed(monkeypatch) -> None:
    _durable_env(monkeypatch)
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 0)
    result = approval.check_all_command_guards(
        "rm -rf /tmp/durable-approval-timeout", "local"
    )
    assert result["approved"] is False
    assert "Silence is not consent" in result["message"]


def test_durable_request_is_consumed_exactly_once(monkeypatch) -> None:
    _durable_env(monkeypatch)
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 0)
    approval.check_all_command_guards(
        "rm -rf /tmp/durable-approval-consumed", "local"
    )
    assert wa.list_pending(wa.EXEC) == []
