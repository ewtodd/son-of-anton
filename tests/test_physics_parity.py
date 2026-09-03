"""The Autophysicist's run has the capabilities the machinery promises.

The runner wires a problem spec's ``data:`` list into the sandbox policy,
offers the lookup tools to the roles that have them, and splits the coding
model off from the reasoning model. These tests keep each of those contracts
from silently breaking: they are the things a physics run needs to actually
work against a real dataset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_intern.core.config import Config
from physics_intern.utils.mcp import DEFAULT_ROLES, MCPToolset

SPEC = {
    "name": "parity",
    "problem": "Analyse the data. Write answer to RESULTS.txt.",
    "checks": [{"id": "a", "key": "answer", "expected": 1.0, "tolerance": 0.1}],
}


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = ""
        self.inputSchema = {"type": "object", "properties": {}}


class _FakeToolset(MCPToolset):
    """A reachable toolset without an endpoint."""

    def __init__(self, roles=None):
        super().__init__(
            {
                "url": "http://gateway.invalid/mcp/",
                "headers": {},
                "roles": dict(roles or DEFAULT_ROLES),
                "timeout": 5.0,
            }
        )
        self._discovered = [
            _Tool("arxiv-get_abstract"),
            _Tool("context7-query-docs"),
        ]


@pytest.fixture
def runner_kwargs(tmp_path, monkeypatch):
    """What a run of the spec hands to the Autophysicist runner."""
    data_dir = tmp_path / "lab-data"
    data_dir.mkdir()
    spec = dict(SPEC, data=[str(data_dir)])

    monkeypatch.setattr(
        "physics_intern.autophysicist.runner.MCPToolset.from_config",
        staticmethod(lambda: _FakeToolset()),
    )

    seen: dict = {}

    def fake_runner(**kwargs):
        seen.update(kwargs)
        return tmp_path

    monkeypatch.setattr(
        "physics_intern.autophysicist.runner.run_autophysicist", fake_runner
    )

    from physics_intern.run import run_problem

    spec_path = tmp_path / "problem.yaml"
    import yaml

    spec_path.write_text(yaml.dump(spec), encoding="utf-8")
    run_problem(str(spec_path), mode="physics")
    return seen, data_dir


# --- the spec's data reaches the sandbox ------------------------------------


def test_the_specs_data_is_mounted_in_the_sandbox_policy(runner_kwargs) -> None:
    seen, data_dir = runner_kwargs
    assert seen["problem_def"]["data"] == [str(data_dir)], (
        "the spec's data: list did not reach the runner — the run would not "
        "see the dataset it was given"
    )


def test_declared_data_is_passed_to_the_sandbox(monkeypatch, tmp_path) -> None:
    """SandboxPolicy.from_config must actually mount the declared data."""
    from physics_intern.utils.sandbox import SandboxPolicy

    data_dir = tmp_path / "lab-data"
    data_dir.mkdir()

    policy = SandboxPolicy.from_config(extra_data_dirs=[str(data_dir)])
    assert data_dir.resolve() in policy.data_dirs


# --- lookups reach the roles that have them ----------------------------------


def test_the_manager_gets_literature_but_not_docs() -> None:
    """The Manager writes briefs, not code — it gets arXiv reading, not context7."""
    toolset = _FakeToolset()
    names = [t["function"]["name"] for t in toolset.tools_for("manager")]
    assert "arxiv-get_abstract" in names
    assert "context7-query-docs" not in names


def test_a_subagent_gets_documentation_but_not_literature() -> None:
    toolset = _FakeToolset()
    names = [t["function"]["name"] for t in toolset.tools_for("subagent")]
    assert "context7-query-docs" in names
    assert "arxiv-get_abstract" not in names


def test_a_role_cannot_call_a_tool_outside_its_allowlist() -> None:
    """Role-scoped, not global: a sub-agent asking for a paper is refused."""
    from physics_intern.utils.mcp import LookupExecutor

    toolset = _FakeToolset()
    assert toolset.handles("arxiv-get_abstract", "manager") is True
    assert toolset.handles("arxiv-get_abstract", "subagent") is False
    call = LookupExecutor(toolset, "subagent").execute("arxiv-get_abstract", {})
    assert call.is_error is True


def test_no_mcp_leaves_the_run_lookups_empty(monkeypatch, tmp_path) -> None:
    seen: dict = {}

    def fake_runner(**kwargs):
        seen.update(kwargs)
        return tmp_path

    monkeypatch.setattr(
        "physics_intern.autophysicist.runner.MCPToolset.from_config",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr(
        "physics_intern.autophysicist.runner.run_autophysicist", fake_runner
    )
    from physics_intern.run import run_problem

    run_problem("a plain question", mode="physics")
    assert seen["problem_def"] is None


# --- the coder model applies to whoever writes code --------------------------


def test_a_coding_dispatch_resolves_to_the_coder_model() -> None:
    config = Config()
    config.model = "reasoning-model"
    config.coder_model = "coding-model"
    assert config.model_for_agent("subagent", coding=True) == "coding-model"
    assert config.model_for_agent("subagent", coding=False) == "reasoning-model"
    # ...and the run's config is untouched.
    assert config.model == "reasoning-model"


def test_the_role_table_is_the_autophysicists() -> None:
    """The same names mean the same thing wherever they are consulted."""
    assert set(DEFAULT_ROLES) == {"manager", "subagent"}
