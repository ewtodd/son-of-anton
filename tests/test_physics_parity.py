"""The two physics modes get the same capabilities.

`physics` (one Research Manager) and `research` (nine agents over a claim
ledger) are different reasoning architectures, and that is the point of having
both. What they should NOT differ on is what the machinery underneath offers
them: the sandbox, the data the problem spec declares, the lookup tools, and
which model writes the code.

They did differ. MCP lookups and the coder-model split were built for the
Autophysicist and reached the pipeline not at all, and a problem spec's `data:`
list was threaded through the Autophysicist runner alone — so the pipeline's
computer agent could not see the data the run was given. These tests are the
thing that keeps a capability from landing in one mode and quietly not the
other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_intern.core.config import Config
from physics_intern.utils.mcp import DEFAULT_AGENTS, MCPToolset

SPEC = {
    "name": "parity",
    "problem": "Analyse the data. Write answer to RESULTS.txt.",
    "checks": [{"id": "a", "key": "answer", "expected": 1.0, "tolerance": 0.1}],
}


class _FakeToolset(MCPToolset):
    """A reachable toolset without an endpoint."""

    def __init__(self, agents=DEFAULT_AGENTS):
        super().__init__(
            {
                "url": "http://gateway.invalid/mcp/",
                "headers": {},
                "allow": ["arxiv", "context7"],
                "agents": list(agents),
                "timeout": 5.0,
            }
        )
        self._tools = [
            {
                "type": "function",
                "function": {"name": n, "description": "", "parameters": {}},
            }
            for n in ("arxiv-get_abstract", "context7-query-docs")
        ]
        self._names = frozenset({"arxiv-get_abstract", "context7-query-docs"})


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A research-mode engine with a spec, lookups, and a coder model."""
    from physics_intern.engine import PhysicsIntern

    data_dir = tmp_path / "lab-data"
    data_dir.mkdir()
    spec = dict(SPEC, data=[str(data_dir)])

    monkeypatch.setattr(
        "physics_intern.engine.MCPToolset.from_config", lambda: _FakeToolset()
    )
    config = Config()
    config.workspace_dir = str(tmp_path / "ws")
    Path(config.workspace_dir).mkdir()
    config.coder_model = "coding-model"
    config.model = "reasoning-model"

    built = PhysicsIntern(spec["problem"], config=config, problem_def=spec)
    built._data_dir = data_dir
    return built


# --- the spec's data reaches the pipeline's sandbox -------------------------


def test_the_specs_data_is_mounted_for_the_pipeline(engine) -> None:
    assert engine._data_dir.resolve() in engine.policy.data_dirs, (
        "the pipeline's computer agent could not see the data the run was "
        "given — only the Autophysicist threaded a spec's data: through"
    )


def test_the_computer_agent_runs_under_that_policy(engine) -> None:
    assert engine.computer.policy is engine.policy
    assert engine.policy.workspace == engine.workspace.root


# --- lookups reach the same roles in both modes -----------------------------


@pytest.mark.parametrize("role", ["surveyor", "researcher", "computer"])
def test_reasoning_and_coding_roles_get_lookups(engine, role) -> None:
    agent = getattr(engine, role)
    names = [t["function"]["name"] for t in agent.lookup_tools()]
    assert "arxiv-get_abstract" in names
    assert agent.uses_tools is True


def test_the_orchestrator_is_left_out_by_default(engine) -> None:
    """It dispatches over a state machine on a ten-round budget; lookups there
    risk it never dispatching."""
    assert engine.orchestrator.lookup_tools() == []


def test_the_computers_tool_set_includes_the_lookups(engine) -> None:
    from physics_intern.agents.computer.tools import ToolExecutor

    executor = ToolExecutor(
        workspace_root=engine.workspace.root,
        policy=engine.policy,
        mcp=engine.mcp,
    )
    names = [t["function"]["name"] for t in executor.computer_tools()]
    assert "execute_python" in names
    assert "context7-query-docs" in names


def test_a_lookup_only_agent_can_call_one(engine) -> None:
    from physics_intern.utils.mcp import LookupExecutor

    executor = LookupExecutor(engine.mcp, "researcher")
    assert executor.exit_tool_names == frozenset()
    call = executor.execute("nope", {})
    assert call.is_error and "arxiv-get_abstract" in call.output


def test_no_mcp_leaves_every_agent_one_shot(tmp_path, monkeypatch) -> None:
    from physics_intern.engine import PhysicsIntern

    monkeypatch.setattr(
        "physics_intern.engine.MCPToolset.from_config", lambda: None
    )
    config = Config()
    config.workspace_dir = str(tmp_path / "ws")
    Path(config.workspace_dir).mkdir()
    built = PhysicsIntern("plain question", config=config)
    for role in ("surveyor", "researcher", "critic", "reviewer"):
        assert getattr(built, role).lookup_tools() == []
        assert getattr(built, role).uses_tools is False


# --- the coder model applies to whoever writes code, in either mode ---------


def test_the_pipelines_computer_uses_the_coder_model(engine, monkeypatch) -> None:
    from physics_intern.state.task import Task, TaskType

    seen: list[str] = []

    def fake_loop(*, config, **kwargs):
        seen.append(config.model)
        from physics_intern.llm import AgentResult

        return AgentResult(text="{}")

    monkeypatch.setattr(
        "physics_intern.agents.computer.agent.run_agent_loop", fake_loop
    )
    task = Task(
        task_id="C-1", task_type=TaskType.COMPUTE, assigned_to="computer", iteration=1
    )
    engine.computer._call_with_tools("ctx", task, 1)
    assert seen == ["coding-model"]
    assert engine.config.model == "reasoning-model", "the run's config must not be mutated"


def test_a_non_coding_agent_keeps_the_reasoning_model(engine) -> None:
    assert engine.researcher.config.model == "reasoning-model"


def test_default_roles_are_the_same_two_in_both_modes() -> None:
    assert "manager" in DEFAULT_AGENTS  # the Autophysicist's reasoning role
    assert {"surveyor", "researcher", "computer"} <= set(DEFAULT_AGENTS)
    assert "orchestrator" not in DEFAULT_AGENTS
