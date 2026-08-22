"""Core-tool surface contracts — every tool in the core bundle must be
registered with a schema and a callable handler, or the agent would receive
a tool the registry cannot dispatch.
"""

from __future__ import annotations

import json

from toolsets import TOOLSETS, _SON_OF_ANTON_CORE_TOOLS


def test_core_tools_are_members_of_declared_toolsets() -> None:
    # A core tool listed in no toolset is never exposed to any agent.
    toolset_members: set[str] = set()
    for toolset_cfg in TOOLSETS.values():
        if isinstance(toolset_cfg, dict):
            toolset_members.update(toolset_cfg.get("tools", []))

    for name in _SON_OF_ANTON_CORE_TOOLS:
        assert name in toolset_members, f"core tool {name!r} is in no toolset"


def test_core_tools_are_registered_with_schemas() -> None:
    import model_tools  # noqa: F401 — triggers tool discovery
    from tools.registry import registry

    for name in sorted(_SON_OF_ANTON_CORE_TOOLS):
        entry = registry.get_entry(name)
        assert entry is not None, f"core tool {name!r} is not registered"
        assert callable(entry.handler), f"core tool {name!r} has no handler"
        schema = entry.schema
        assert schema is not None, f"core tool {name!r} has no schema"
        assert schema.get("name") == name
        assert schema.get("description"), f"core tool {name!r}: empty description"
        assert schema.get("parameters", {}).get("type") == "object"


def test_core_tool_schema_is_json_serializable() -> None:
    import model_tools  # noqa: F401
    from tools.registry import registry

    for name in _SON_OF_ANTON_CORE_TOOLS:
        schema = registry.get_schema(name)
        assert schema is not None
        json.dumps(schema)  # raises on non-serializable schema content


def test_fork_surface_tools_present() -> None:
    import model_tools  # noqa: F401
    from tools.registry import registry

    for name in ("terminal", "read_file", "write_file", "web_search", "skills_list"):
        assert registry.get_entry(name) is not None, f"missing core tool {name!r}"


def test_removed_surface_tools_absent() -> None:
    # The bloat strip must stay in effect — hermes-era tools are gone.
    import model_tools  # noqa: F401
    from tools.registry import registry

    for name in ("browser_automation", "text_to_speech", "image_generation", "kanban"):
        assert registry.get_entry(name) is None, f"{name!r} resurfaced"
