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


# --- the Manager can see its own workspace ----------------------------------
#
# It writes every sub-agent prompt but could not read what they produced, so a
# 19 KB script that failed on one line got paraphrased into the scratchpad and
# rewritten from the paraphrase. The file was on disk the whole time.


def _manager(tmp_path):
    from physics_intern.autophysicist.memory import PermanentMemory, Scratchpad
    from physics_intern.autophysicist.tools import ManagerToolExecutor
    from physics_intern.core.config import Config
    from physics_intern.utils.sandbox import SandboxPolicy

    return ManagerToolExecutor(
        config=Config(),
        permanent_memory=PermanentMemory(tmp_path),
        scratchpad=Scratchpad(tmp_path),
        workspace_root=tmp_path,
        iteration=1,
        policy=SandboxPolicy(workspace=tmp_path, mode="off"),
    )


def test_the_manager_is_offered_the_workspace_tools(tmp_path) -> None:
    names = [t["function"]["name"] for t in _manager(tmp_path).all_tools()]
    assert "list_workspace_files" in names
    assert "read_workspace_file" in names


def test_it_can_read_a_script_a_subagent_wrote(tmp_path) -> None:
    (tmp_path / "computations").mkdir()
    script = tmp_path / "computations" / "subagent_iter3_1_attempt1.py"
    script.write_text("import ROOT\ntree.SetMaxTreeError(0)\n", encoding="utf-8")

    call = _manager(tmp_path).execute(
        "read_workspace_file", {"path": "computations/subagent_iter3_1_attempt1.py"}
    )
    assert call.is_error is False
    assert "SetMaxTreeError" in call.output


def test_listing_finds_prior_work(tmp_path) -> None:
    (tmp_path / "computations").mkdir()
    (tmp_path / "computations" / "a.py").write_text("x", encoding="utf-8")
    call = _manager(tmp_path).execute("list_workspace_files", {"subdirectory": "computations"})
    assert "computations/a.py" in call.output


def test_reading_outside_the_workspace_is_refused(tmp_path) -> None:
    call = _manager(tmp_path).execute("read_workspace_file", {"path": "../../etc/passwd"})
    assert call.is_error is True
    assert "outside the workspace" in call.output


def test_an_absolute_path_cannot_escape_either(tmp_path) -> None:
    call = _manager(tmp_path).execute("read_workspace_file", {"path": "/etc/passwd"})
    assert call.is_error is True


def test_a_long_file_is_truncated(tmp_path) -> None:
    (tmp_path / "big.py").write_text("x" * 50_000, encoding="utf-8")
    call = _manager(tmp_path).execute(
        "read_workspace_file", {"path": "big.py", "max_chars": 1000}
    )
    assert "truncated" in call.output
    assert len(call.output) < 3000


def test_reading_survives_wind_down(tmp_path) -> None:
    """Writing up what was found needs the ability to look at it."""
    manager = _manager(tmp_path)
    manager._wind_down = True
    names = [t["function"]["name"] for t in manager.active_tools]
    assert "read_workspace_file" in names
    assert "dispatch_subagent" not in names


def test_the_retry_leads_with_the_exception(tmp_path, monkeypatch) -> None:
    """A retry given 5000 chars of traceback fixes whatever it notices first."""
    from physics_intern.autophysicist import subagent as subagent_module

    class _Result:
        stdout = ""
        stderr = (
            "Traceback (most recent call last):\n"
            '  File "s.py", line 2, in <module>\n'
            "AttributeError: 'TTree' object has no attribute 'SetMaxTreeError'"
        )

    assert (
        subagent_module._error_headline(_Result())
        == "AttributeError: 'TTree' object has no attribute 'SetMaxTreeError'"
    )


def test_subagent_output_is_saved_next_to_the_script(tmp_path) -> None:
    from physics_intern.autophysicist.subagent import _save_output

    class _Result:
        stdout = "centroid = 661.4\n"
        stderr = ""

    script = tmp_path / "s.py"
    script.write_text("print(1)", encoding="utf-8")
    _save_output(script, _Result())
    assert (tmp_path / "s.output").read_text() == "centroid = 661.4\n"


# --- per-role model assignment ---------------------------------------------
#
# `coder_model` answers "is this call writing code", which is not the same
# question as "which agent is this". The pipeline has nine roles and they are
# not the same job.


def _config(**kwargs):
    from physics_intern.core.config import Config

    config = Config()
    config.model = "reasoning-model"
    for key, value in kwargs.items():
        setattr(config, key, value)
    return config


def test_the_default_is_the_run_model() -> None:
    assert _config().model_for_agent("deep_critic") == "reasoning-model"


def test_a_coding_call_takes_the_coder_model() -> None:
    config = _config(coder_model="coding-model")
    assert config.model_for_agent("subagent", coding=True) == "coding-model"
    assert config.model_for_agent("subagent", coding=False) == "reasoning-model"


def test_an_explicit_role_beats_both() -> None:
    config = _config(
        coder_model="coding-model", agent_models={"computer": "special-model"}
    )
    assert config.model_for_agent("computer", coding=True) == "special-model"
    # ...and only that role: everything else still falls through.
    assert config.model_for_agent("reviewer") == "reasoning-model"


def test_roles_match_by_longest_prefix() -> None:
    """Sub-agents are named per dispatch: subagent_iter3_2."""
    config = _config(agent_models={"subagent": "a", "subagent_iter9": "b"})
    assert config.model_for_agent("subagent_iter3_2") == "a"
    assert config.model_for_agent("subagent_iter9_1") == "b"


def test_an_agent_runs_under_its_resolved_model(tmp_path) -> None:
    from physics_intern.agents.critic.agent import CriticAgent
    from physics_intern.core.metrics import MetricsTracker
    from physics_intern.core.workspace import WorkspaceManager

    # The critic answers to "deep_critic" — an agent_models key has to match
    # the agent's own name, not the directory it lives in.
    config = _config(agent_models={"deep_critic": "critic-model"})
    config.workspace_dir = str(tmp_path)
    agent = CriticAgent(config, WorkspaceManager(config), MetricsTracker())
    assert agent.name == "deep_critic"
    assert agent.agent_config.model == "critic-model"
    assert agent.config.model == "reasoning-model", "the run's config was mutated"


def test_an_agent_with_no_override_shares_the_run_config(tmp_path) -> None:
    """No copy when nothing changes — nine agents share one object."""
    from physics_intern.agents.critic.agent import CriticAgent
    from physics_intern.core.metrics import MetricsTracker
    from physics_intern.core.workspace import WorkspaceManager

    config = _config()
    config.workspace_dir = str(tmp_path)
    agent = CriticAgent(config, WorkspaceManager(config), MetricsTracker())
    assert agent.agent_config is config


def test_the_computer_is_the_coding_role(tmp_path) -> None:
    from physics_intern.agents.computer.agent import ComputerAgent
    from physics_intern.core.metrics import MetricsTracker
    from physics_intern.core.workspace import WorkspaceManager

    config = _config(coder_model="coding-model")
    config.workspace_dir = str(tmp_path)
    agent = ComputerAgent(config, WorkspaceManager(config), MetricsTracker())
    assert agent.writes_code is True
    assert agent.agent_config.model == "coding-model"


def test_a_missing_module_retry_names_what_is_installed(monkeypatch, tmp_path) -> None:
    """"Fix the error" gets the same import back.

    A ModuleNotFoundError is a wrong premise about the environment, not a bug
    in the script — and when the Manager's own instructions named the package,
    the sub-agent will keep writing it. Three byte-identical attempts, all
    dying on `import uproot`, is what that looks like.
    """
    from physics_intern.autophysicist import subagent as subagent_module
    from physics_intern.core.config import Config
    from physics_intern.utils.sandbox import SandboxPolicy

    prompts: list[str] = []

    class _Resp:
        text = "```python\nimport uproot\n```"
        input_tokens = 1
        output_tokens = 1

    def fake_call_llm(*, system, user_content, config, agent_name, iteration):
        prompts.append(user_content)
        return _Resp()

    class _Failed:
        stdout = ""
        stderr = "ModuleNotFoundError: No module named 'uproot'"
        returncode = 1
        timed_out = False

    monkeypatch.setattr(subagent_module, "call_llm", fake_call_llm)
    monkeypatch.setattr(subagent_module, "execute_python", lambda *a, **k: _Failed())
    monkeypatch.setattr(
        "physics_intern.utils.sandbox.describe_runtime",
        lambda _i=None: "Python 3.12, and: ROOT 6.40.00, analysis_utilities 26.8.27",
    )

    subagent_module.dispatch_subagent(
        system_prompt="s",
        user_message="use uproot to open the file",
        execute_code=True,
        config=Config(),
        workspace_root=tmp_path,
        iteration=1,
        policy=SandboxPolicy(workspace=tmp_path, mode="off"),
    )

    retry = prompts[1]
    assert "THAT PACKAGE IS NOT INSTALLED" in retry
    assert "analysis_utilities" in retry
    assert "there is no network" in retry
    assert "your instructions named that package, they were wrong" in retry


def test_an_ordinary_error_retry_does_not_lecture_about_packages(
    monkeypatch, tmp_path
) -> None:
    from physics_intern.autophysicist import subagent as subagent_module
    from physics_intern.core.config import Config
    from physics_intern.utils.sandbox import SandboxPolicy

    prompts: list[str] = []

    class _Resp:
        text = "```python\nx = 1/0\n```"
        input_tokens = 1
        output_tokens = 1

    class _Failed:
        stdout = ""
        stderr = "ZeroDivisionError: division by zero"
        returncode = 1
        timed_out = False

    monkeypatch.setattr(
        subagent_module,
        "call_llm",
        lambda **kw: (prompts.append(kw["user_content"]), _Resp())[1],
    )
    monkeypatch.setattr(subagent_module, "execute_python", lambda *a, **k: _Failed())

    subagent_module.dispatch_subagent(
        system_prompt="s",
        user_message="divide",
        execute_code=True,
        config=Config(),
        workspace_root=tmp_path,
        iteration=1,
        policy=SandboxPolicy(workspace=tmp_path, mode="off"),
    )
    assert "THAT PACKAGE IS NOT INSTALLED" not in prompts[1]
    assert "ZeroDivisionError" in prompts[1]
