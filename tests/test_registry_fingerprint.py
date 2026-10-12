"""The gateway rebuilds a cached agent only when the tool schema set changes.

Every rebuild throws away the conversation's prompt cache. The registry's
``_generation`` counter moves on any mutation — including an MCP refresh that
re-registers identical tools — so it cannot be the rebuild key. Contracts:
the schema fingerprint is stable across identical re-registration and moves
when a schema, a tool, or the tool set changes; the gateway's cache-busting
signature follows the fingerprint, not the counter.
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

    _register(registry, _schema())
    try:
        before = GatewayRunner._extract_cache_busting_config({})
        registry.deregister("fp_probe")
        _register(registry, _schema())
        assert GatewayRunner._extract_cache_busting_config({}) == before
        _register(registry, _schema("changed"))
        assert GatewayRunner._extract_cache_busting_config({}) != before
    finally:
        registry.deregister("fp_probe")
