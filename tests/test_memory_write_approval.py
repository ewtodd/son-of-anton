"""A gated memory write waits for the user, then lands on approval.

With ``memory.write_approval`` on and no inline prompt channel (gateway,
script, cron), a memory write is staged instead of committed; ``/memory
pending`` lists it and ``/memory approve`` applies it to the store on disk.
With the gate off the same write lands at once. Real store, real config,
temp home.
"""

from __future__ import annotations

import json

_ENTRY = "Prefers tabs over spaces in shell scripts."


def _memory_text(home) -> str:
    memories = home / "memories"
    if not memories.is_dir():
        return ""
    return "".join(
        p.read_text(encoding="utf-8") for p in sorted(memories.rglob("*.md"))
    )


def _set_gate(enabled: bool) -> None:
    from son_of_anton_cli.config import save_config

    save_config({"memory": {"write_approval": enabled}})


def test_a_gated_write_is_staged_until_approved(son_of_anton_home) -> None:
    from son_of_anton_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store, memory_tool

    _set_gate(True)
    result = json.loads(
        memory_tool(action="add", target="memory", content=_ENTRY, store=load_on_disk_store())
    )

    assert result.get("staged") is True
    assert _ENTRY not in _memory_text(son_of_anton_home), "a gated write was committed"
    pending_id = result["pending_id"]
    assert pending_id in handle_pending_subcommand(wa.MEMORY, ["pending"])

    out = handle_pending_subcommand(
        wa.MEMORY, ["approve", pending_id], memory_store=load_on_disk_store()
    )

    assert "Approved 1 memory write" in out
    assert _ENTRY in _memory_text(son_of_anton_home)
    assert wa.list_pending(wa.MEMORY) == []


def test_a_rejected_write_never_lands(son_of_anton_home) -> None:
    from son_of_anton_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store, memory_tool

    _set_gate(True)
    result = json.loads(
        memory_tool(action="add", target="memory", content=_ENTRY, store=load_on_disk_store())
    )
    handle_pending_subcommand(wa.MEMORY, ["reject", result["pending_id"]])

    assert wa.list_pending(wa.MEMORY) == []
    assert _ENTRY not in _memory_text(son_of_anton_home)


def test_with_the_gate_off_a_write_lands_at_once(son_of_anton_home) -> None:
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store, memory_tool

    _set_gate(False)
    result = json.loads(
        memory_tool(action="add", target="memory", content=_ENTRY, store=load_on_disk_store())
    )

    assert result.get("success") is True and not result.get("staged")
    assert _ENTRY in _memory_text(son_of_anton_home)
    assert wa.list_pending(wa.MEMORY) == []
