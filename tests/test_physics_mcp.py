"""Lookup tools for the Research Manager, and the limits on them.

The physics modes run their own agent loop, so none of the main agent's MCP
machinery reached them: a run could compute anything and look up nothing.

The interesting constraint is not "can it call a tool" but "how few". The
gateway this points at aggregates 25 tools, 19 of them arXiv, against a Manager
whose budget is a handful of tools and fifteen calls an iteration — so an
explicit allowlist is required rather than optional, and an unreachable
endpoint must degrade to no lookups instead of failing the run.
"""

from __future__ import annotations

import pytest

from physics_intern.autophysicist.tools import ManagerToolExecutor
from physics_intern.utils.mcp import MCPToolset, is_allowed, resolve_mcp_config

GATEWAY = {
    "mcp_servers": {
        "oracle": {
            "url": "http://gateway.invalid/mcp/",
            "headers": {"Authorization": "Bearer ${TEST_MCP_KEY}"},
        }
    },
    "physics": {"mcp": {"server": "oracle", "allow": ["context7", "arxiv-get_abstract"]}},
}


def test_allow_matches_whole_servers_and_exact_tools() -> None:
    allow = ["context7", "arxiv-get_abstract"]
    assert is_allowed("context7-query-docs", allow)
    assert is_allowed("context7-resolve-library-id", allow)
    assert is_allowed("arxiv-get_abstract", allow)
    assert not is_allowed("arxiv-download_paper", allow)
    assert not is_allowed("nixos-nix", allow)


def test_a_server_name_does_not_match_a_longer_server_name() -> None:
    assert not is_allowed("context7x-tool", ["context7"])


def test_no_allowlist_means_no_mcp() -> None:
    config = {"physics": {"mcp": {"server": "oracle", "allow": []}}}
    assert resolve_mcp_config(config) is None
    assert MCPToolset.from_config(config) is None


def test_missing_mcp_section_means_no_mcp() -> None:
    assert resolve_mcp_config({"physics": {}}) is None


def test_server_indirection_reuses_the_declared_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MCP_KEY", "secret-token")
    settings = resolve_mcp_config(GATEWAY)
    assert settings["url"] == "http://gateway.invalid/mcp/"
    assert settings["headers"]["Authorization"] == "Bearer secret-token"


def test_api_key_env_builds_the_header_when_none_is_declared(monkeypatch) -> None:
    monkeypatch.setenv("SOME_KEY", "abc123")
    settings = resolve_mcp_config(
        {
            "physics": {
                "mcp": {
                    "url": "http://gateway.invalid/mcp/",
                    "api_key_env": "SOME_KEY",
                    "allow": ["arxiv"],
                }
            }
        }
    )
    assert settings["headers"]["Authorization"] == "Bearer abc123"


def test_an_unreachable_endpoint_degrades_to_no_tools() -> None:
    """A documentation server being down must not abort a research run."""
    toolset = MCPToolset(
        {
            "url": "http://127.0.0.1:1/mcp/",
            "headers": {},
            "allow": ["arxiv"],
            "timeout": 2.0,
        }
    )
    assert toolset.tools() == []
    assert toolset.handles("arxiv-get_abstract") is False


# --- how the Manager sees them ---------------------------------------------


class _FakeToolset:
    def __init__(self, names):
        self._names = list(names)

    def tools(self):
        return [
            {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
            for n in self._names
        ]

    def handles(self, name):
        return name in self._names

    def call(self, name, arguments):
        return f"called {name} with {sorted(arguments)}", False


@pytest.fixture
def executor(tmp_path):
    from physics_intern.autophysicist.memory import PermanentMemory, Scratchpad
    from physics_intern.core.config import Config
    from physics_intern.utils.sandbox import SandboxPolicy

    return ManagerToolExecutor(
        config=Config(),
        permanent_memory=PermanentMemory(tmp_path),
        scratchpad=Scratchpad(tmp_path),
        workspace_root=tmp_path,
        iteration=1,
        policy=SandboxPolicy(workspace=tmp_path, mode="off"),
        mcp=_FakeToolset(["arxiv-get_abstract", "context7-query-docs"]),
    )


def test_mcp_tools_are_offered_alongside_the_managers_own(executor) -> None:
    names = [t["function"]["name"] for t in executor.all_tools()]
    assert "dispatch_subagent" in names
    assert "arxiv-get_abstract" in names
    assert "context7-query-docs" in names


def test_the_manager_can_call_an_mcp_tool(executor) -> None:
    call = executor.execute("arxiv-get_abstract", {"paper_id": "2110.01992"})
    assert call.is_error is False
    assert "arxiv-get_abstract" in call.output


def test_wind_down_withdraws_the_lookups(executor) -> None:
    """Wind-down means write down what you found, not start a new search."""
    executor._wind_down = True
    names = [t["function"]["name"] for t in executor.active_tools]
    assert "arxiv-get_abstract" not in names
    assert "dispatch_subagent" not in names
    assert "end_turn" in names

    call = executor.execute("arxiv-get_abstract", {"paper_id": "x"})
    assert call.is_error is True
    assert "wind-down" in call.output


def test_unknown_tool_error_names_the_mcp_tools_too(executor) -> None:
    call = executor.execute("nixos-nix", {})
    assert call.is_error is True
    assert "arxiv-get_abstract" in call.output


def test_no_mcp_configured_leaves_the_manager_unchanged(tmp_path) -> None:
    from physics_intern.autophysicist.memory import PermanentMemory, Scratchpad
    from physics_intern.core.config import Config
    from physics_intern.utils.sandbox import SandboxPolicy

    executor = ManagerToolExecutor(
        config=Config(),
        permanent_memory=PermanentMemory(tmp_path),
        scratchpad=Scratchpad(tmp_path),
        workspace_root=tmp_path,
        iteration=1,
        policy=SandboxPolicy(workspace=tmp_path, mode="off"),
    )
    assert executor.all_tools() == list(ManagerToolExecutor.ALL_TOOLS)

