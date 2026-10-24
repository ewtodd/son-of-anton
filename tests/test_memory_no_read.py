"""A memory call without an action or operations writes nothing and fails.

Models call memory(target=...) alone to "read" it; that must not touch the
store, and the failure has to be distinguishable from a successful write.
"""

from __future__ import annotations

import json

from tools.memory_tool import ScopedMemoryStore, memory_tool


def test_actionless_call_fails_without_writing() -> None:
    store = ScopedMemoryStore(active_scope="gateway")
    store.load_from_disk()
    assert json.loads(memory_tool(action="add", target="user", content="Bike lock code is 4512.", store=store))[
        "success"
    ]
    before = store.format_for_system_prompt("user")

    result = json.loads(memory_tool(target="user", store=store))

    assert result.get("success") is False
    assert store.format_for_system_prompt("user") == before
