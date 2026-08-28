"""One instance can stop another through the MCP bridge.

e-play's agent could already talk to markets and ricky over their MCP
sockets; it could not tell them to stop. That matters because those two
share a GPU with the working day — the active-hours window covers the
predictable case, and this covers the rest of it, when the machine is
needed back mid-shift.

The primitive is the ESTOP sentinel rather than anything in-process: each
bridge is a separate `son-of-anton mcp serve` process that shares the
instance's home with its gateway, so a file in that home is what crosses
the boundary. It is also what `son-of-anton pause` already writes and what
the gateway already checks at the top of every turn, so this adds a caller,
not a mechanism.

New work stops; in-flight work is deliberately left alone.
"""

from __future__ import annotations

import json

import pytest

from agent import estop


@pytest.fixture()
def instance_home(tmp_path, monkeypatch):
    """An isolated SON_OF_ANTON_HOME standing in for a sibling instance."""
    home = tmp_path / ".son-of-anton"
    home.mkdir()
    monkeypatch.setenv("SON_OF_ANTON_HOME", str(home))
    yield home


class _FakeToolManager:
    def __init__(self):
        self._tools = {}

    def add_tool(self, fn):
        self._tools[fn.__name__] = fn


class _FakeMCPServer:
    """Stand-in for ``mcp.server.MCPServer`` — same shape test_mcp_serve uses."""

    def __init__(self, *args, **kwargs):
        self._tool_manager = _FakeToolManager()

    def tool(self):
        def decorator(fn):
            self._tool_manager.add_tool(fn)
            return fn

        return decorator


@pytest.fixture()
def tools(instance_home, monkeypatch):
    """The bridge's tool callables, by name."""
    import mcp_serve

    monkeypatch.setattr(mcp_serve, "_MCP_SERVER_AVAILABLE", True)
    monkeypatch.setattr(mcp_serve, "MCPServer", _FakeMCPServer)
    server = mcp_serve.create_mcp_server(event_bridge=mcp_serve.EventBridge())
    return server._tool_manager._tools


def _call(tools, name, **kwargs):
    return json.loads(tools[name](**kwargs))


def test_the_three_control_tools_are_exposed(tools) -> None:
    assert {"agent_pause", "agent_resume", "agent_status"} <= set(tools)


def test_pausing_writes_the_sentinel_the_gateway_reads(instance_home, tools) -> None:
    """Not an internal flag: the gateway is a different process."""
    out = _call(tools, "agent_pause", reason="GPU needed for a training run")
    assert out["paused"] is True
    assert estop.is_engaged() is True
    assert (instance_home / "ESTOP").exists()


def test_the_reason_reaches_the_notice_the_other_side_sends(instance_home, tools) -> None:
    _call(tools, "agent_pause", reason="doing work")
    assert "doing work" in (estop.paused_reply() or "")


def test_resume_lifts_it(instance_home, tools) -> None:
    _call(tools, "agent_pause")
    assert _call(tools, "agent_resume")["was_paused"] is True
    assert estop.is_engaged() is False
    assert estop.paused_reply() is None


def test_both_are_idempotent(instance_home, tools) -> None:
    """A second stop must not fail, and neither must a redundant resume —
    the caller is another agent, which will retry rather than reason."""
    assert _call(tools, "agent_pause")["already_paused"] is False
    assert _call(tools, "agent_pause")["already_paused"] is True
    assert _call(tools, "agent_resume")["was_paused"] is True
    assert _call(tools, "agent_resume")["was_paused"] is False


def test_status_reports_the_pause_and_its_reason(instance_home, tools) -> None:
    assert _call(tools, "agent_status")["paused"] is False
    _call(tools, "agent_pause", reason="benchmark running")
    out = _call(tools, "agent_status")
    assert out["paused"] is True
    assert out["pause_reason"] == "benchmark running"


def test_status_reports_the_window_the_instance_answers_in(instance_home, tools) -> None:
    """So the caller can tell "paused by me" from "asleep on its own"."""
    (instance_home / "config.yaml").write_text(
        "gateway:\n  active_hours: [20, 7]\n", encoding="utf-8"
    )
    assert _call(tools, "agent_status")["active_hours"] == [20, 7]


def test_an_unrestricted_instance_reports_no_window(instance_home, tools) -> None:
    assert _call(tools, "agent_status")["active_hours"] is None
