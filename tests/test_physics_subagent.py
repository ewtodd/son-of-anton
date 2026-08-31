"""Which sub-agent gets which model.

`physics.coder_model` routes on what the sub-agent is FOR, not on the fact
that it is a sub-agent. A dispatch with `execute_code` is a coding job and goes
to the coding model — and that is where the volume is, one call per script plus
up to three more each time a script fails. A dispatch without it (derive this,
check that derivation, argue the other side) is doing the same physics
reasoning the Manager does, and sending it to a coding model to save latency
trades away the reason it was dispatched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from physics_intern.autophysicist import subagent as subagent_module
from physics_intern.core.config import Config


@dataclass
class _Response:
    text: str = "reasoning only, no code block"
    input_tokens: int = 1
    output_tokens: int = 1
    log_path: str = ""


def models_used(monkeypatch, tmp_path: Path, *, execute_code: bool, model: str):
    """Dispatch one sub-agent and report the model each LLM call ran under."""
    seen: list[str] = []

    def fake_call_llm(system, user_content, config, agent_name="", iteration=0):
        seen.append(config.model)
        return _Response()

    monkeypatch.setattr(subagent_module, "call_llm", fake_call_llm)

    config = Config()
    config.model = "manager-model"
    subagent_module.dispatch_subagent(
        system_prompt="s",
        user_message="u",
        execute_code=execute_code,
        config=config,
        workspace_root=tmp_path,
        iteration=1,
        model=model,
    )
    return seen


def test_code_dispatches_use_the_coding_model(monkeypatch, tmp_path) -> None:
    assert models_used(
        monkeypatch, tmp_path, execute_code=True, model="coding-model"
    ) == ["coding-model"]


def test_reasoning_dispatches_keep_the_managers_model(monkeypatch, tmp_path) -> None:
    assert models_used(
        monkeypatch, tmp_path, execute_code=False, model="coding-model"
    ) == ["manager-model"], (
        "a sub-agent asked to derive a result or find an error in one is doing "
        "physics, not writing code — routing it to the coding model to save "
        "latency gives up what it was dispatched for"
    )


def test_no_override_leaves_everything_on_the_managers_model(
    monkeypatch, tmp_path
) -> None:
    assert models_used(monkeypatch, tmp_path, execute_code=True, model="") == [
        "manager-model"
    ]


def test_the_managers_own_config_is_not_mutated(monkeypatch, tmp_path) -> None:
    """The override is per dispatch — the next Manager turn must not inherit it."""
    seen: list[str] = []

    def fake_call_llm(system, user_content, config, agent_name="", iteration=0):
        seen.append(config.model)
        return _Response()

    monkeypatch.setattr(subagent_module, "call_llm", fake_call_llm)
    config = Config()
    config.model = "manager-model"
    subagent_module.dispatch_subagent(
        system_prompt="s",
        user_message="u",
        execute_code=True,
        config=config,
        workspace_root=tmp_path,
        iteration=1,
        model="coding-model",
    )
    assert seen == ["coding-model"]
    assert config.model == "manager-model"
