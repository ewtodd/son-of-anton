"""Built-in memory has two scopes so a TUI session carries no personal context.

``common`` is visible everywhere; ``gateway`` is personal and visible only to
Signal (and Signal-bound cron). The active scope is resolved from the session
platform, explicit writes can name a scope, and approved (staged) writes land
where they were staged. Legacy names (``shared``, ``cli``) read as ``common``.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from son_of_anton_constants import get_son_of_anton_home
from tools.memory_tool import (
    MEMORY_SCOPES,
    ScopedMemoryStore,
    apply_memory_pending,
    memory_tool,
    normalize_memory_scope,
    resolve_memory_scope,
)
from utils import dump_toml


def _memories():
    directory = get_son_of_anton_home() / "memories"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_rules(rules: list) -> None:
    (_memories() / "rules.toml").write_text(dump_toml({"rule": rules}), encoding="utf-8")


def _read(filename: str, key: str) -> list:
    path = _memories() / filename
    if not path.exists():
        return []
    return tomllib.loads(path.read_text(encoding="utf-8")).get(key, [])


def _texts(filename: str, key: str) -> list:
    return [e["text"] for e in _read(filename, key)]


def _store(scope: str) -> ScopedMemoryStore:
    store = ScopedMemoryStore(active_scope=scope)
    store.load_from_disk()
    return store


def test_terminal_session_reads_common_but_not_gateway_rules() -> None:
    _write_rules(
        [
            {"id": "deploy", "text": "common deploy rule", "scope": "common"},
            {"id": "food", "text": "personal food rule", "scope": "gateway"},
        ]
    )
    block = _store("common").format_for_system_prompt() or ""
    assert "common deploy rule" in block
    assert "personal food rule" not in block
    assert "[scope:" not in block


def test_gateway_session_reads_common_and_gateway_rules() -> None:
    _write_rules(
        [
            {"id": "deploy", "text": "common deploy rule", "scope": "common"},
            {"id": "food", "text": "personal food rule", "scope": "gateway"},
        ]
    )
    block = _store("gateway").format_for_system_prompt() or ""
    assert "common deploy rule" in block
    assert "personal food rule" in block
    assert block.count("[scope: gateway]") == 1


def test_a_rule_without_a_scope_is_common_and_an_unknown_scope_renders_nowhere() -> None:
    _write_rules(
        [
            {"id": "bare", "text": "no scope given"},
            {"id": "typo", "text": "mistyped scope", "scope": "gatway"},
        ]
    )
    for scope in ("common", "gateway"):
        block = _store(scope).format_for_system_prompt() or ""
        assert "no scope given" in block
        assert "mistyped scope" not in block


def test_default_fact_write_lands_in_the_active_scope() -> None:
    store = _store("gateway")
    result = json.loads(
        memory_tool(kind="fact", action="add", text="personal fact", store=store)
    )
    assert result.get("success")
    assert _texts("facts.gateway.toml", "fact") == ["personal fact"]
    assert _read("facts.toml", "fact") == []


@pytest.mark.parametrize("name", ["common", "shared", "cli", " Common "])
def test_common_aliases_write_to_the_common_store(name) -> None:
    store = _store("gateway")
    result = json.loads(
        memory_tool(kind="fact", action="add", text="everywhere fact", scope=name, store=store)
    )
    assert result.get("success")
    assert _texts("facts.toml", "fact") == ["everywhere fact"]
    assert _read("facts.gateway.toml", "fact") == []


def test_unknown_scope_is_rejected_before_any_write() -> None:
    store = _store("common")
    result = json.loads(
        memory_tool(kind="fact", action="add", text="nowhere fact", scope="nowhere", store=store)
    )
    assert result.get("success") is False
    error = result.get("error", "")
    assert "nowhere" in error and "common" in error and "gateway" in error
    assert not (_memories() / "facts.toml").exists()


def test_normalization_maps_aliases_and_rejects_unknown() -> None:
    for alias in ("common", "shared", "cli", "COMMON"):
        assert normalize_memory_scope(alias) == "common"
    assert normalize_memory_scope("gateway") == "gateway"
    for bad in ("", "bogus", None, 3):
        assert normalize_memory_scope(bad) is None
    assert set(MEMORY_SCOPES) == {"common", "gateway"}


@pytest.mark.parametrize("scope", ["gateway", "cli", "shared", "common"])
def test_staged_payload_routes_by_scope(scope) -> None:
    store = _store("common")
    result = apply_memory_pending(
        {"kind": "fact", "action": "add", "text": "staged fact", "scope": scope}, store
    )
    assert result.get("success")
    if scope == "gateway":
        assert _texts("facts.gateway.toml", "fact") == ["staged fact"]
        assert _read("facts.toml", "fact") == []
    else:
        assert _texts("facts.toml", "fact") == ["staged fact"]
        assert _read("facts.gateway.toml", "fact") == []


def test_scope_mapping_and_config_override() -> None:
    assert resolve_memory_scope("cli") == "common"
    assert resolve_memory_scope("tui") == "common"
    assert resolve_memory_scope("signal") == "gateway"
    assert resolve_memory_scope("cron") == "common"
    assert resolve_memory_scope("discord") == "common"
    assert resolve_memory_scope("cron", {"memory": {"scope": "gateway"}}) == "gateway"
    assert resolve_memory_scope("signal", {"memory": {"scope": "cli"}}) == "common"
    assert resolve_memory_scope("signal", {"memory": {"scope": "bogus"}}) == "gateway"


def test_legacy_markdown_files_are_neither_rendered_nor_touched() -> None:
    legacy = {
        "MEMORY.md": "old note",
        "USER.md": "old profile",
        "MEMORY.gateway.md": "old gateway note",
        "MEMORY.cli.md": "old terminal note",
    }
    for name, body in legacy.items():
        (_memories() / name).write_text(body, encoding="utf-8")
    store = _store("gateway")
    assert store.format_for_system_prompt() is None
    result = json.loads(
        memory_tool(kind="fact", action="add", text="new common fact", store=store, scope="common")
    )
    assert result.get("success")
    for name, body in legacy.items():
        assert (_memories() / name).read_text(encoding="utf-8") == body


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
    _write_rules(
        [
            {"id": "deploy", "text": "common deploy rule", "scope": "common"},
            {"id": "food", "text": "personal food rule", "scope": "gateway"},
        ]
    )
    cli = _agent("cli")
    cli_block = cli._memory_store.format_for_system_prompt() or ""
    assert cli._memory_scope == "common"
    assert "common deploy rule" in cli_block
    assert "personal food rule" not in cli_block
    signal = _agent("signal")
    signal_block = signal._memory_store.format_for_system_prompt() or ""
    assert signal._memory_scope == "gateway"
    assert "common deploy rule" in signal_block
    assert "personal food rule" in signal_block
