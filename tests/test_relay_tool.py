"""The relay tool exists only where a chat can relay: a single_user gateway.

It is one service-gated tool (footprint rung 3). Its check reads the live
gateway runner, so the terminal never carries it, a shared gateway never
carries it, and a single_user gateway's messaging chats always do — directly,
never deferred behind tool search, because its schema is the relay's whole
instruction set. A subagent never gets it.
"""

from __future__ import annotations

import types

import pytest


def _definitions(platform: str) -> set[str]:
    import model_tools
    from son_of_anton_cli.tools_config import _get_platform_tools

    toolsets = sorted(_get_platform_tools({}, platform))
    return {
        d["function"]["name"]
        for d in model_tools.get_tool_definitions(enabled_toolsets=toolsets, quiet_mode=True)
    }


@pytest.fixture
def live_runner(monkeypatch):
    import gateway.run

    def _install(single_user: bool):
        runner = types.SimpleNamespace(config=types.SimpleNamespace(single_user=single_user))
        monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: runner)
        return runner

    return _install


def test_the_terminal_never_carries_the_relay(live_runner) -> None:
    assert "relay" not in _definitions("cli")
    live_runner(single_user=True)  # even inside a single_user gateway process
    assert "relay" not in _definitions("cli")


def test_no_gateway_means_no_relay(monkeypatch) -> None:
    import gateway.run
    from tools.relay_tool import check_relay_requirements

    monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: None)
    assert check_relay_requirements() is False
    assert "relay" not in _definitions("signal")


def test_a_shared_gateway_has_no_relay(live_runner) -> None:
    from tools.relay_tool import check_relay_requirements

    live_runner(single_user=False)
    assert check_relay_requirements() is False
    assert "relay" not in _definitions("signal")


@pytest.mark.parametrize("platform", ["signal", "discord", "slack"])
def test_a_single_user_gateway_chat_has_the_relay_up_front(live_runner, platform) -> None:
    live_runner(single_user=True)
    from tools.tool_search import is_deferrable_tool_name

    assert "relay" in _definitions(platform)
    assert is_deferrable_tool_name("relay") is False  # never only behind tool search


def test_the_relay_is_never_handed_to_a_subagent() -> None:
    from tools.delegate_tool import _strip_blocked_tools

    assert "relay" not in _strip_blocked_tools(["terminal", "relay", "file"])


def test_the_relay_call_needs_a_live_single_user_runner(live_runner) -> None:
    import json

    from tools.relay_tool import relay_tool

    live_runner(single_user=False)
    assert "single_user" in json.loads(relay_tool({"action": "list"}))["error"]
