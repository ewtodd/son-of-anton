"""Approval-guard contract — the pre-exec guards must not raise on benign
commands (a live UnboundLocalError in check_all_command_guards killed every
terminal call until approval_mode was initialised unconditionally).
"""

from __future__ import annotations

from tools.approval import check_all_command_guards


def test_mode_off_bypasses_without_unbound_error(son_of_anton_home) -> None:
    from son_of_anton_cli.config import save_config

    save_config({"approvals": {"mode": "off"}})
    result = check_all_command_guards("echo hi", "local")
    assert result.get("approved") is True


def test_smart_mode_returns_a_decision_not_an_exception(son_of_anton_home) -> None:
    from son_of_anton_cli.config import save_config

    save_config({"approvals": {"mode": "smart", "timeout": 1}})
    result = check_all_command_guards(
        "echo hi", "local", approval_callback=lambda *a, **k: {"resolved": True, "choice": "approve"}
    )
    # Either approved via the callback or blocked-with-message — never a raise.
    assert "approved" in result or "blocked" in result or "requires_approval" in result
