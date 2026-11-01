"""Built-in memory has two scopes so a TUI session carries no personal context.

``common`` is visible everywhere; ``gateway`` is personal and visible only to
Signal (and Signal-bound cron). The active scope is resolved from the session
platform, explicit writes can name a scope, and approved (staged) writes land
where they were staged. Legacy names (``shared``, ``cli``) read as ``common``.
"""

from __future__ import annotations

import json

import pytest

from son_of_anton_constants import get_son_of_anton_home
from tools.memory_tool import (
    MEMORY_SCOPES,
    MemoryStore,
    ScopedMemoryStore,
    apply_memory_pending,
    memory_tool,
    normalize_memory_scope,
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


def test_terminal_session_reads_common_but_not_gateway_entries() -> None:
    _write_entries("MEMORY.md", ["common deploy fact"])
    _write_entries("MEMORY.gateway.md", ["personal food fact"])
    store = ScopedMemoryStore(active_scope="common")
    store.load_from_disk()
    block = store.format_for_system_prompt("memory") or ""
    assert "common deploy fact" in block
    assert "personal food fact" not in block
    assert "[scope:" not in block


def test_gateway_session_reads_common_and_gateway_entries() -> None:
    _write_entries("MEMORY.md", ["common deploy fact"])
    _write_entries("MEMORY.gateway.md", ["personal food fact"])
    store = ScopedMemoryStore(active_scope="gateway")
    store.load_from_disk()
    block = store.format_for_system_prompt("memory") or ""
    assert "common deploy fact" in block
    assert "personal food fact" in block
    assert block.count("[scope: gateway]") == 1


def test_default_write_lands_in_the_active_scope() -> None:
    store = ScopedMemoryStore(active_scope="gateway")
    store.load_from_disk()
    result = json.loads(
        memory_tool(action="add", target="memory", content="personal fact", store=store)
    )
    assert result.get("success")
    assert "personal fact" in _read_entries("MEMORY.gateway.md")
    assert "personal fact" not in _read_entries("MEMORY.md")


@pytest.mark.parametrize("name", ["common", "shared", "cli", " Common "])
def test_common_aliases_write_to_the_common_store(name) -> None:
    store = ScopedMemoryStore(active_scope="gateway")
    store.load_from_disk()
    result = json.loads(
        memory_tool(
            action="add",
            target="memory",
            content="everywhere fact",
            scope=name,
            store=store,
        )
    )
    assert result.get("success")
    assert "everywhere fact" in _read_entries("MEMORY.md")
    assert "everywhere fact" not in _read_entries("MEMORY.gateway.md")
    assert _read_entries("MEMORY.cli.md") == []


def test_unknown_scope_is_rejected_before_any_write() -> None:
    store = ScopedMemoryStore(active_scope="common")
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
    error = result.get("error", "")
    assert "nowhere" in error and "common" in error and "gateway" in error
    assert _read_entries("MEMORY.md") == []


def test_normalization_maps_aliases_and_rejects_unknown() -> None:
    for alias in ("common", "shared", "cli", "COMMON"):
        assert normalize_memory_scope(alias) == "common"
    assert normalize_memory_scope("gateway") == "gateway"
    for bad in ("", "bogus", None, 3):
        assert normalize_memory_scope(bad) is None
    assert set(MEMORY_SCOPES) == {"common", "gateway"}


@pytest.mark.parametrize("scope", ["gateway", "cli", "shared", "common"])
def test_staged_payload_routes_by_scope(scope) -> None:
    store = ScopedMemoryStore(active_scope="common")
    store.load_from_disk()
    result = apply_memory_pending(
        {"action": "add", "target": "memory", "content": "staged fact", "scope": scope},
        store,
    )
    assert result.get("success")
    if scope == "gateway":
        assert "staged fact" in _read_entries("MEMORY.gateway.md")
        assert _read_entries("MEMORY.md") == []
    else:
        assert "staged fact" in _read_entries("MEMORY.md")
        assert _read_entries("MEMORY.gateway.md") == []


def test_scope_mapping_and_config_override() -> None:
    assert resolve_memory_scope("cli") == "common"
    assert resolve_memory_scope("tui") == "common"
    assert resolve_memory_scope("signal") == "gateway"
    assert resolve_memory_scope("cron") == "common"
    assert resolve_memory_scope("discord") == "common"
    assert resolve_memory_scope("cron", {"memory": {"scope": "gateway"}}) == "gateway"
    assert resolve_memory_scope("signal", {"memory": {"scope": "cli"}}) == "common"
    assert resolve_memory_scope("signal", {"memory": {"scope": "bogus"}}) == "gateway"


def test_legacy_cli_files_are_neither_rendered_nor_touched() -> None:
    _write_entries("MEMORY.cli.md", ["old terminal fact"])
    _write_entries("USER.cli.md", ["old terminal user fact"])
    store = ScopedMemoryStore(active_scope="common")
    store.load_from_disk()
    block = store.format_for_system_prompt("memory") or ""
    assert "old terminal fact" not in block
    result = json.loads(
        memory_tool(action="add", target="memory", content="new common fact", store=store)
    )
    assert result.get("success")
    assert _read_entries("MEMORY.cli.md") == ["old terminal fact"]
    assert _read_entries("USER.cli.md") == ["old terminal user fact"]


def _agent(platform: str):
    from run_agent import AIAgent
    from son_of_anton_cli.config import save_config

    save_config(
        {
            "model": {"default": "qwen3.6-35b-a3b", "provider": "custom"},
            "custom_providers": {"custom": {"base_url": "http://127.0.0.1:9/v1"}},
        }
    )
    return AIAgent(
        base_url="http://127.0.0.1:9/v1",
        provider="custom",
        model="qwen3.6-35b-a3b",
        api_mode="chat_completions",
        max_iterations=1,
        quiet_mode=True,
        platform=platform,
        skip_context_files=True,
    )


def test_agents_render_by_platform(son_of_anton_home) -> None:
    _write_entries("MEMORY.md", ["common deploy fact"])
    _write_entries("MEMORY.gateway.md", ["personal food fact"])
    _write_entries("MEMORY.cli.md", ["legacy terminal fact"])
    cli = _agent("cli")
    cli_block = cli._memory_store.format_for_system_prompt("memory") or ""
    assert cli._memory_scope == "common"
    assert "common deploy fact" in cli_block
    assert "personal food fact" not in cli_block
    assert "legacy terminal fact" not in cli_block
    signal = _agent("signal")
    signal_block = signal._memory_store.format_for_system_prompt("memory") or ""
    assert signal._memory_scope == "gateway"
    assert "common deploy fact" in signal_block
    assert "personal food fact" in signal_block
    assert "legacy terminal fact" not in signal_block


def test_plain_store_keeps_the_legacy_paths() -> None:
    store = MemoryStore()
    assert store._path_for("memory").name == "MEMORY.md"
    assert store._path_for("user").name == "USER.md"
