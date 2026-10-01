"""Built-in memory is scoped so a TUI coding session carries no personal context.

``shared`` is visible everywhere; ``cli`` and ``gateway`` are per-surface.
The active scope is resolved from the session platform, explicit writes can
name a scope, and approved (staged) writes land where they were staged.
"""

from __future__ import annotations

import json

from son_of_anton_constants import get_son_of_anton_home
from tools.memory_tool import (
    MEMORY_SCOPES,
    MemoryStore,
    ScopedMemoryStore,
    apply_memory_pending,
    memory_tool,
    resolve_memory_scope,
)

DELIMITER = "\n§\n"


def _write_entries(filename: str, entries: list) -> None:
    directory = get_son_of_anton_home() / "memories"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(DELIMITER.join(entries), encoding="utf-8")


def _read_entries(filename: str) -> list:
    path = get_son_of_anton_home() / "memories" / filename
    if not path.exists():
        return []
    return [e for e in path.read_text(encoding="utf-8").split(DELIMITER) if e]


def test_cli_session_reads_shared_but_not_gateway_entries() -> None:
    _write_entries("MEMORY.md", ["shared deploy fact"])
    _write_entries("MEMORY.gateway.md", ["personal food fact"])
    store = ScopedMemoryStore(active_scope="cli")
    store.load_from_disk()
    block = store.format_for_system_prompt("memory") or ""
    assert "shared deploy fact" in block
    assert "personal food fact" not in block


def test_gateway_session_reads_shared_and_gateway_entries() -> None:
    _write_entries("MEMORY.md", ["shared deploy fact"])
    _write_entries("MEMORY.gateway.md", ["personal food fact"])
    store = ScopedMemoryStore(active_scope="gateway")
    store.load_from_disk()
    block = store.format_for_system_prompt("memory") or ""
    assert "shared deploy fact" in block
    assert "personal food fact" in block


def test_default_write_lands_in_the_active_scope() -> None:
    store = ScopedMemoryStore(active_scope="cli")
    store.load_from_disk()
    result = json.loads(
        memory_tool(action="add", target="memory", content="cli-only fact", store=store)
    )
    assert result.get("success")
    assert "cli-only fact" in _read_entries("MEMORY.cli.md")
    assert "cli-only fact" not in _read_entries("MEMORY.md")


def test_explicit_shared_scope_writes_to_the_shared_store() -> None:
    store = ScopedMemoryStore(active_scope="cli")
    store.load_from_disk()
    result = json.loads(
        memory_tool(
            action="add",
            target="memory",
            content="everywhere fact",
            scope="shared",
            store=store,
        )
    )
    assert result.get("success")
    assert "everywhere fact" in _read_entries("MEMORY.md")
    assert "everywhere fact" not in _read_entries("MEMORY.cli.md")


def test_unknown_scope_is_rejected_before_any_write() -> None:
    store = ScopedMemoryStore(active_scope="cli")
    store.load_from_disk()
    result = json.loads(
        memory_tool(
            action="add",
            target="memory",
            content="nowhere fact",
            scope="nowhere",
            store=store,
        )
    )
    assert result.get("success") is False
    assert "nowhere" in result.get("error", "")
    assert _read_entries("MEMORY.cli.md") == []


def test_approved_write_routes_by_staged_scope() -> None:
    store = ScopedMemoryStore(active_scope="cli")
    store.load_from_disk()
    result = apply_memory_pending(
        {
            "action": "add",
            "target": "memory",
            "content": "staged personal fact",
            "scope": "gateway",
        },
        store,
    )
    assert result.get("success")
    assert "staged personal fact" in _read_entries("MEMORY.gateway.md")


def test_scope_mapping_and_config_override() -> None:
    assert resolve_memory_scope("cli") == "cli"
    assert resolve_memory_scope("signal") == "gateway"
    assert resolve_memory_scope("discord") == "gateway"
    assert resolve_memory_scope("slack") == "gateway"
    assert resolve_memory_scope("cron") == "shared"
    assert resolve_memory_scope("cron", {"memory": {"scope": "cli"}}) == "cli"
    assert resolve_memory_scope("signal", {"memory": {"scope": "bogus"}}) == "gateway"


def test_plain_store_keeps_the_legacy_shared_paths() -> None:
    store = MemoryStore()
    assert store._path_for("memory").name == "MEMORY.md"
    assert store._path_for("user").name == "USER.md"
    assert set(MEMORY_SCOPES) == {"shared", "cli", "gateway"}
