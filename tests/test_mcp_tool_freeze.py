"""Contract: an agent's MCP tool list is frozen after the opening turns.

``tools=`` precedes the messages in the cached prompt prefix, so any change to
``agent.tools`` re-prefills the whole conversation. The between-turns refresh
may only ADD tools, only during the opening turns; after that a server that
parks keeps its tools in the schema and calls to them report the server as
unavailable instead of "Unknown tool".

Every snapshot test runs twice: with MCP tools listed directly
(``tools.tool_search = false``) and deferred behind the tool-search bridge
(the default).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tools.mcp_tool as mcp_tool
from agent.turn_context import (
    mcp_auto_refresh_window_open,
    run_between_turns_mcp_refresh,
)
from model_tools import handle_function_call
from tools.registry import registry
from tools.tool_search import BRIDGE_TOOL_NAMES

SERVER = "oracle"
TOOLSET = f"mcp-{SERVER}"
ASK = f"mcp__{SERVER}__ask"


def _schema(name: str) -> dict:
    return {
        "name": name,
        "description": f"fake {name}",
        "parameters": {"type": "object", "properties": {}},
    }


class FakeServer:
    """Registers/deregisters a fake MCP server's tools through the real paths."""

    def __init__(self) -> None:
        self._registered_tool_names: list[str] = []

    def register(self, *short_names: str) -> None:
        for short in short_names:
            name = f"mcp__{SERVER}__{short}"
            registry.register(
                name=name,
                toolset=TOOLSET,
                schema=_schema(name),
                handler=lambda args, _n=name, **kw: json.dumps({"ok": _n}),
            )
            mcp_tool._track_mcp_tool_server(name, SERVER)
            if name not in self._registered_tool_names:
                self._registered_tool_names.append(name)

    def park(self) -> None:
        # The real MCPServerTask method, bound to this fake's tool list.
        mcp_tool.MCPServerTask._deregister_tools(self, "reconnecting")


@pytest.fixture(params=["direct", "bridged"])
def tool_search(request, son_of_anton_home):
    if request.param == "direct":
        (son_of_anton_home / "config.toml").write_text(
            "[tools]\ntool_search = false\n", encoding="utf-8"
        )
    return request.param


@pytest.fixture
def server():
    srv = FakeServer()
    yield srv
    for name in list(srv._registered_tool_names):
        if registry.get_entry(name) is not None:
            registry.deregister(name)
        mcp_tool._forget_mcp_tool_server(name)
        mcp_tool._mcp_departed_tools.pop(name, None)


def _agent(*, refresh_turns: int = 3, tools: list | None = None):
    tools = list(tools or [])
    agent = SimpleNamespace(
        tools=tools,
        valid_tool_names={t["function"]["name"] for t in tools},
        enabled_toolsets=[TOOLSET],
        disabled_toolsets=None,
        _tool_snapshot_generation=0,
        _mcp_refresh_turns=refresh_turns,
        notices=[],
    )
    agent._emit_status = agent.notices.append
    return agent


def _reaches(agent, name: str) -> bool:
    """Can the model reach *name* through this agent's tool list?"""
    return name in agent.valid_tool_names or bool(
        agent.valid_tool_names & BRIDGE_TOOL_NAMES
    )


def _turn(agent, history=None) -> set:
    return run_between_turns_mcp_refresh(agent, history or [])


def test_slow_server_lands_within_opening_turns(server, tool_search):
    agent = _agent()
    _turn(agent)  # turn 1: server still connecting
    server.register("ask", "search")
    assert _turn(agent)  # turn 2 adds it
    assert _reaches(agent, ASK)
    assert {t["function"]["name"] for t in agent.tools} == agent.valid_tool_names


def test_window_is_additive_only(server, tool_search):
    agent = _agent()
    server.register("ask")
    _turn(agent)
    before = list(agent.tools)
    server.park()
    _turn(agent)  # still inside the window, but parking must not remove
    assert agent.tools == before and _reaches(agent, ASK)


def test_snapshot_frozen_after_window_through_park_revive_and_new_tools(
    server, tool_search
):
    agent = _agent()
    server.register("ask")
    for _ in range(3):
        _turn(agent)
    frozen_tools = list(agent.tools)
    frozen_names = set(agent.valid_tool_names)

    server.park()
    _turn(agent)
    assert agent.tools == frozen_tools and agent.valid_tool_names == frozen_names

    server.register("ask")  # revival
    _turn(agent)
    server.register("brand_new")  # tools/list_changed adds a tool
    _turn(agent)
    _turn(agent)
    assert agent.tools == frozen_tools and agent.valid_tool_names == frozen_names
    # The notice never enters the conversation; it goes to the status channel,
    # once. With the bridge, the live tool_search catalog already reaches the
    # new tool, so there is nothing to announce.
    if tool_search == "direct":
        assert len(agent.notices) == 1 and "/reload-mcp" in agent.notices[0]
    else:
        assert agent.notices == []


def test_late_server_after_window_is_not_added(server, tool_search):
    agent = _agent()
    for _ in range(3):
        _turn(agent)
    server.register("ask")
    assert _turn(agent) == set()
    assert agent.tools == []


def test_refresh_turns_zero_disables_automatic_refresh(server, tool_search):
    agent = _agent(refresh_turns=0)
    server.register("ask")
    assert _turn(agent) == set() and agent.tools == []


def test_resumed_session_only_fills_an_empty_mcp_surface(server, tool_search):
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]

    # Resumed with no MCP tools yet: the late server lands once...
    agent = _agent()
    server.register("ask")
    assert _turn(agent, history)
    frozen = list(agent.tools)
    # ...then the snapshot freezes even inside the turn window.
    server.park()
    server.register("search")
    assert _turn(agent, history) == set()
    assert agent.tools == frozen

    # Resumed with MCP tools already present: frozen from the first turn.
    agent2 = _agent(tools=[{"type": "function", "function": _schema(ASK)}])
    assert _turn(agent2, history) == set()
    assert [t["function"]["name"] for t in agent2.tools] == [ASK]


def test_window_predicate():
    fresh = dict(refresh_turns=3, resumed=False, has_mcp_tools=True)
    assert mcp_auto_refresh_window_open(turns_seen=2, **fresh)
    assert not mcp_auto_refresh_window_open(turns_seen=3, **fresh)
    assert not mcp_auto_refresh_window_open(
        turns_seen=0, refresh_turns=3, resumed=True, has_mcp_tools=True
    )
    assert mcp_auto_refresh_window_open(
        turns_seen=0, refresh_turns=3, resumed=True, has_mcp_tools=False
    )


UNAVAILABLE = "MCP server 'oracle' is currently unavailable (reconnecting)"


def test_parked_server_tool_call_reports_unavailable_then_recovers(server):
    server.register("ask")
    server.park()
    result = json.loads(handle_function_call(ASK, {}))
    assert UNAVAILABLE in result["error"]

    server.register("ask")  # revival: same schema, calls work again
    assert json.loads(handle_function_call(ASK, {})) == {"ok": ASK}


def test_parked_server_via_tool_search_bridge_reports_unavailable(server):
    server.register("ask")
    server.park()
    call = json.loads(
        handle_function_call("tool_call", {"name": ASK, "arguments": {}})
    )
    assert UNAVAILABLE in call["error"]
    describe = json.loads(handle_function_call("tool_describe", {"name": ASK}))
    assert UNAVAILABLE in describe["error"]

    server.register("ask")
    assert json.loads(
        handle_function_call("tool_call", {"name": ASK, "arguments": {}})
    ) == {"ok": ASK}


def test_never_registered_mcp_name_is_still_unknown():
    result = json.loads(handle_function_call("mcp__oracle__nope", {}))
    assert "Unknown tool" in result["error"]
