"""The gateway rebuilds a cached agent only when the tool schema set changes.

Every rebuild throws away the conversation's prompt cache. The registry's
``_generation`` counter moves on any mutation — including an MCP refresh that
re-registers identical tools — so it cannot be the rebuild key. Contracts:
the schema fingerprint is stable across identical re-registration and moves
when a schema, a tool, or the tool set changes; the gateway's cache-busting
signature follows the fingerprint, not the counter — and ignores MCP tools
entirely, because a parked/revived MCP server is governed by the MCP freeze
and /reload-mcp, not by rebuilding every cached agent.
"""

from __future__ import annotations

from tools.registry import ToolRegistry, registry


def _schema(desc: str = "Echo text.") -> dict:
    return {
        "name": "fp_probe",
        "description": desc,
        "parameters": {"type": "object", "properties": {"t": {"type": "string"}}},
    }


def _register(reg, schema, name="fp_probe", toolset="mcp-fp"):
    reg.register(name=name, toolset=toolset, schema=schema, handler=lambda args, **kw: "{}")


def test_identical_reregistration_keeps_the_fingerprint() -> None:
    reg = ToolRegistry()
    _register(reg, _schema())
    before = (reg.schema_fingerprint(), reg._generation)
    reg.deregister("fp_probe")
    _register(reg, _schema())  # new handler object, same schema
    assert reg._generation != before[1]
    assert reg.schema_fingerprint() == before[0]


def test_schema_and_set_changes_move_the_fingerprint() -> None:
    reg = ToolRegistry()
    _register(reg, _schema())
    seen = {reg.schema_fingerprint()}
    _register(reg, _schema("Echo text, louder."))
    seen.add(reg.schema_fingerprint())
    _register(reg, {**_schema(), "name": "fp_other"}, name="fp_other")
    seen.add(reg.schema_fingerprint())
    reg.deregister("fp_other")
    reg.deregister("fp_probe")
    seen.add(reg.schema_fingerprint())
    assert len(seen) == 4


def test_gateway_signature_ignores_a_no_op_refresh() -> None:
    from gateway.run import GatewayRunner

    _register(registry, _schema(), toolset="fp-builtin")
    try:
        before = GatewayRunner._extract_cache_busting_config({})
        registry.deregister("fp_probe")
        _register(registry, _schema(), toolset="fp-builtin")
        assert GatewayRunner._extract_cache_busting_config({}) == before
        _register(registry, _schema("changed"), toolset="fp-builtin")
        assert GatewayRunner._extract_cache_busting_config({}) != before
    finally:
        registry.deregister("fp_probe")


def test_gateway_signature_ignores_mcp_servers_parking_and_reviving() -> None:
    from gateway.run import GatewayRunner

    before = GatewayRunner._extract_cache_busting_config({})
    # A server comes up: its tools and its server-name alias register.
    _register(registry, _schema(), name="mcp_fp_probe", toolset="mcp-fp")
    registry.register_toolset_alias("fp", "mcp-fp")
    try:
        assert GatewayRunner._extract_cache_busting_config({}) == before
        # It changes a schema, parks (tools leave), and revives.
        _register(registry, _schema("louder"), name="mcp_fp_probe", toolset="mcp-fp")
        assert GatewayRunner._extract_cache_busting_config({}) == before
        registry.deregister("mcp_fp_probe")
        assert GatewayRunner._extract_cache_busting_config({}) == before
        _register(registry, _schema(), name="mcp_fp_probe", toolset="mcp-fp")
        assert GatewayRunner._extract_cache_busting_config({}) == before
    finally:
        registry.deregister("mcp_fp_probe")
        with registry._lock:
            registry._toolset_aliases.pop("fp", None)


def test_the_full_fingerprint_still_sees_mcp_tools() -> None:
    reg = ToolRegistry()
    _register(reg, _schema(), toolset="fp-builtin")
    full, without = reg.schema_fingerprint(), reg.schema_fingerprint(exclude_mcp=True)
    _register(reg, _schema(), name="mcp_fp_probe", toolset="mcp-fp")
    assert reg.schema_fingerprint() != full
    assert reg.schema_fingerprint(exclude_mcp=True) == without
