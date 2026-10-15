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

    for name in ("terminal", "read_file", "write_file", "web_search"):
        assert registry.get_entry(name) is not None, f"missing core tool {name!r}"


# Skills are not a model surface: the agent is never offered a tool to list,
# load, or write them.
_SKILL_TOOL_NAMES = frozenset({"skills_list", "skill_view", "skill_manage"})


def test_skill_tools_are_not_model_facing() -> None:
    import model_tools  # noqa: F401
    from tools.registry import registry

    registered = set(registry.get_all_tool_names())
    assert not registered & _SKILL_TOOL_NAMES, "a skill tool is registered"
    for ts_name, toolset_cfg in TOOLSETS.items():
        if isinstance(toolset_cfg, dict):
            listed = set(toolset_cfg.get("tools", []))
            assert not listed & _SKILL_TOOL_NAMES, f"toolset {ts_name!r} lists a skill tool"


def test_no_tool_schema_names_a_skill_tool() -> None:
    # A schema pointing the model at a tool it can never call sends it
    # chasing a dead end; every registered tool's model-facing text must
    # stay clear of the skill tools.
    import model_tools  # noqa: F401
    from tools.registry import registry

    for name in registry.get_all_tool_names():
        text = json.dumps(registry.get_schema(name) or {})
        for skill_tool in _SKILL_TOOL_NAMES:
            assert skill_tool not in text, f"{name!r} schema names {skill_tool!r}"


def test_removed_surface_tools_absent() -> None:
    # The bloat strip must stay in effect — hermes-era tools are gone.
    import model_tools  # noqa: F401
    from tools.registry import registry

    for name in ("browser_automation", "text_to_speech", "image_generation", "kanban"):
        assert registry.get_entry(name) is None, f"{name!r} resurfaced"
