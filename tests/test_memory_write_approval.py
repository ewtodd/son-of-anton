"""A gated fact write waits for the user, then lands on approval.

With ``memory.write_approval`` on and no inline prompt channel (gateway,
script, cron), a fact write is staged instead of committed; ``/memory
pending`` lists it and ``/memory approve`` applies it to the store on disk.
With the gate off the same write lands at once. (Rules are always gated; see
test_memory_rules_facts.) Real store, real config, temp home.
"""

from __future__ import annotations

import json

_FACT = "The build box prefers tabs over spaces in shell scripts."


def _facts_text(home) -> str:
    path = home / "memories" / "facts.toml"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _set_gate(enabled: bool) -> None:
    from son_of_anton_cli.config import save_config

    save_config({"memory": {"write_approval": enabled}})


def _add_fact():
    from tools.memory_tool import load_on_disk_store, memory_tool

    return json.loads(
        memory_tool(kind="fact", action="add", text=_FACT, store=load_on_disk_store())
    )


def test_a_gated_write_is_staged_until_approved(son_of_anton_home) -> None:
    from son_of_anton_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store

    _set_gate(True)
    result = _add_fact()

    assert result.get("staged") is True
    assert _FACT not in _facts_text(son_of_anton_home), "a gated write was committed"
    pending_id = result["pending_id"]
    assert pending_id in handle_pending_subcommand(wa.MEMORY, ["pending"])

    out = handle_pending_subcommand(
        wa.MEMORY, ["approve", pending_id], memory_store=load_on_disk_store()
    )

    assert "Approved 1 memory write" in out
    assert _FACT in _facts_text(son_of_anton_home)
    assert wa.list_pending(wa.MEMORY) == []


def test_a_rejected_write_never_lands(son_of_anton_home) -> None:
    from son_of_anton_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    _set_gate(True)
    result = _add_fact()
    handle_pending_subcommand(wa.MEMORY, ["reject", result["pending_id"]])

    assert wa.list_pending(wa.MEMORY) == []
    assert _FACT not in _facts_text(son_of_anton_home)


def test_with_the_gate_off_a_write_lands_at_once(son_of_anton_home) -> None:
    from tools import write_approval as wa

    _set_gate(False)
    result = _add_fact()

    assert result.get("success") is True and not result.get("staged")
    assert _FACT in _facts_text(son_of_anton_home)
    assert wa.list_pending(wa.MEMORY) == []
