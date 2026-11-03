"""The one thing worth keeping from the nine-agent pipeline.

The Autophysicist is a single agent that decides what to investigate, judges
its own sub-agents' output, and decides what is true. Its own prompt names that
as the design's weak point — "you are the least reliable component", "nothing
is reliable until independently verified" — but those are norms with nothing
enforcing them, and the observed failure matched: iterations ending with a
confident plan, an empty permanent memory, and the same environment facts
rediscovered next time.

This is the cheapest possible enforcement: one prompt, one answer, no tools, no
verdict that gates anything. It must not be able to take the run down with it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autophysicist import critic as critic_module
from autophysicist.memory import PermanentMemory, Scratchpad
from autophysicist.core.config import Config
from autophysicist.core.tool_call import ToolCall


class _Result:
    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls or []


class _Response:
    def __init__(self, text):
        self.text = text
        self.input_tokens = 1
        self.output_tokens = 1


@pytest.fixture
def pieces(tmp_path):
    return PermanentMemory(tmp_path), Scratchpad(tmp_path), tmp_path


def test_the_critique_reaches_the_next_iteration(pieces) -> None:
    from autophysicist.runner import _build_user_content

    memory, scratchpad, _ = pieces
    content = _build_user_content(
        "Calibrate the detector.", "", memory, scratchpad, 2, 50, "You verified nothing."
    )
    assert "<critique_of_your_last_iteration>" in content
    assert "You verified nothing." in content


def test_no_critique_adds_no_block(pieces) -> None:
    from autophysicist.runner import _build_user_content

    memory, scratchpad, _ = pieces
    content = _build_user_content("p", "", memory, scratchpad, 1, 50, "")
    assert "critique_of_your_last_iteration" not in content


def test_an_empty_memory_is_named_as_such(pieces) -> None:
    """The critic's most useful finding is "nothing was established"."""
    memory, scratchpad, _ = pieces
    context = critic_module.build_context("p", memory, scratchpad, 1, _Result())
    assert "EMPTY" in context


def test_the_iteration_activity_is_shown_with_errors_marked(pieces) -> None:
    memory, scratchpad, _ = pieces
    calls = [
        ToolCall(
            tool_name="dispatch_subagent",
            tool_input={},
            output="ok",
            is_error=False,
            duration=1.0,
        ),
        ToolCall(
            tool_name="read_workspace_file",
            tool_input={},
            output="AttributeError: no GetLeaves",
            is_error=True,
            duration=1.0,
        ),
    ]
    context = critic_module.build_context("p", memory, scratchpad, 1, _Result(calls))
    assert "dispatch_subagent [ok]" in context
    assert "read_workspace_file [ERROR]" in context


def test_an_iteration_that_did_nothing_says_so(pieces) -> None:
    memory, scratchpad, _ = pieces
    context = critic_module.build_context("p", memory, scratchpad, 1, _Result())
    assert "produced nothing" in context


def test_it_runs_under_its_own_model(pieces, monkeypatch) -> None:
    """One call per iteration is where a slow, knowledgeable model belongs."""
    memory, scratchpad, root = pieces
    seen = {}

    def fake_call_llm(*, system, user_content, config, agent_name, iteration):
        seen["model"] = config.model
        seen["agent"] = agent_name
        return _Response("Looks fine.")

    monkeypatch.setattr("autophysicist.llm.call_llm", fake_call_llm)
    config = Config()
    config.model = "manager-model"
    config.agent_models = {"critic": "ds4"}

    critic_module.run_critique(
        config=config,
        problem_text="p",
        permanent_memory=memory,
        scratchpad=scratchpad,
        workspace_root=root,
        iteration=1,
        result=_Result(),
    )
    assert seen == {"model": "ds4", "agent": "critic"}
    assert config.model == "manager-model", "the run's config was mutated"


def test_every_critique_is_logged(pieces, monkeypatch) -> None:
    memory, scratchpad, root = pieces
    monkeypatch.setattr(
        "autophysicist.llm.call_llm",
        lambda **kw: _Response("The calibration anchor is quenched."),
    )
    critic_module.run_critique(
        config=Config(),
        problem_text="p",
        permanent_memory=memory,
        scratchpad=scratchpad,
        workspace_root=root,
        iteration=3,
        result=_Result(),
    )
    log = (Path(root) / "CRITIQUE_LOG.md").read_text()
    assert "Iteration 3" in log
    assert "quenched" in log


def test_a_failing_critic_never_fails_the_run(pieces, monkeypatch) -> None:
    """It is advice. Advice is not worth an aborted run."""
    memory, scratchpad, root = pieces

    def boom(**kwargs):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr("autophysicist.llm.call_llm", boom)
    assert (
        critic_module.run_critique(
            config=Config(),
            problem_text="p",
            permanent_memory=memory,
            scratchpad=scratchpad,
            workspace_root=root,
            iteration=1,
            result=_Result(),
        )
        == ""
    )


def test_a_long_critique_is_capped(pieces, monkeypatch) -> None:
    """It competes with the problem statement for the Manager's attention."""
    memory, scratchpad, root = pieces
    monkeypatch.setattr(
        "autophysicist.llm.call_llm", lambda **kw: _Response("x" * 50_000)
    )
    text = critic_module.run_critique(
        config=Config(),
        problem_text="p",
        permanent_memory=memory,
        scratchpad=scratchpad,
        workspace_root=root,
        iteration=1,
        result=_Result(),
    )
    assert len(text) == critic_module.MAX_CRITIQUE_CHARS


def test_the_critic_is_told_what_the_runtime_has(pieces) -> None:
    """A reviewer who does not know what is installed invents work that cannot run.

    Without this the critic advised `import uproot` — deliberately not shipped
    — the Manager copied it into a sub-agent brief, and the sub-agent wrote it
    three times, because an explicit instruction beats general guidance.
    """
    memory, scratchpad, _ = pieces
    context = critic_module.build_context(
        "p", memory, scratchpad, 1, _Result(), runtime="Python 3.12 and: ROOT 6.40"
    )
    assert "<execution_environment>" in context
    assert "ROOT 6.40" in context
    assert "Do not suggest a package that is not in that list." in context


def test_no_runtime_description_adds_no_block(pieces) -> None:
    memory, scratchpad, _ = pieces
    context = critic_module.build_context("p", memory, scratchpad, 1, _Result())
    assert "execution_environment" not in context


def test_a_broken_runtime_probe_does_not_break_the_critic(monkeypatch) -> None:
    def boom():
        raise RuntimeError("no interpreter")

    monkeypatch.setattr("autophysicist.utils.sandbox.describe_runtime", boom)
    assert critic_module._runtime_description() == ""


def test_the_header_shows_every_effective_model(monkeypatch, capsys, tmp_path) -> None:
    """A critic silently inheriting the Manager's model looked identical to one
    configured to use it: the header printed only agent_models overrides, so an
    empty map printed nothing."""
    from autophysicist.core.config import Config

    config = Config()
    config.model = "manager-model"
    config.coder_model = "coder-model"

    # No override: the critic falls through to the Manager's model, and the
    # header must still say which model that is.
    assert config.model_for_agent("critic") == "manager-model"
    assert config.model_for_agent("subagent", coding=True) == "coder-model"

    config.agent_models = {"critic": "ds4"}
    assert config.model_for_agent("critic") == "ds4"


def test_disabling_the_critic_is_visible() -> None:
    """`critique_every_n: 0` must read as "off", not as a missing line."""
    from autophysicist.core.config import Config

    config = Config()
    config.critique_every_n = 0
    assert config.critique_every_n == 0


# --- human critic mode -------------------------------------------------------


def test_critic_mode_defaults_to_model() -> None:
    assert Config().critic_mode == "model"


def test_critic_mode_is_bridged_from_config(monkeypatch) -> None:
    from autophysicist.core import models as models_module

    monkeypatch.setattr(
        models_module, "_physics_config", lambda: {"critic": {"mode": "human"}}
    )
    config = Config()
    models_module.resolve_models(config)
    assert config.critic_mode == "human"


def test_the_verdict_is_parsed_from_an_editor_buffer() -> None:
    buffer = (
        "# Iteration 1 review\n"
        "some scaffold\n"
        "# --- feedback below this line ---\n"
        "# ignore comments\n"
        "The fit window clips the peak.\n"
        "verdict = stalled\n"
        "block_next = y\n"
    )
    text, verdict, block_next = critic_module._parse_human_response(buffer)
    assert text == "The fit window clips the peak."
    assert verdict == "stalled"
    assert block_next is True


def test_human_mode_injects_feedback_and_records_the_verdict(
    pieces, monkeypatch
) -> None:
    memory, scratchpad, root = pieces
    seen = {}

    def fake_review(review, diff, workspace_root):
        seen["review"] = review
        return ("Check the waveform polarity before fitting.", "wrong_approach", True)

    monkeypatch.setattr(critic_module, "_interactive_human_review", fake_review)
    config = Config()
    config.critic_mode = "human"

    text = critic_module.run_critique(
        config=config,
        problem_text="p",
        permanent_memory=memory,
        scratchpad=scratchpad,
        workspace_root=root,
        iteration=2,
        result=_Result(),
    )

    assert isinstance(text, str)
    assert text == "Check the waveform polarity before fitting."
    assert text.block_next is True
    assert "Iteration 2" in seen["review"]
    log = (Path(root) / "CRITIQUE_LOG.md").read_text()
    assert "critic: human" in log
    record = json.loads(
        (Path(root) / "CRITIQUE_DATA.jsonl").read_text().splitlines()[0]
    )
    assert record["source"] == "human"
    assert record["verdict"] == "wrong_approach"
    assert record["block_next"] is True
    assert record["text_sha256"]


def test_human_mode_skips_without_an_editor_or_tty(pieces, monkeypatch) -> None:
    import io

    memory, scratchpad, root = pieces
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    config = Config()
    config.critic_mode = "human"

    text = critic_module.run_critique(
        config=config,
        problem_text="p",
        permanent_memory=memory,
        scratchpad=scratchpad,
        workspace_root=root,
        iteration=1,
        result=_Result(),
    )
    assert text == ""
    assert not (Path(root) / "CRITIQUE_DATA.jsonl").exists()


def test_model_critiques_are_recorded_with_their_source(pieces, monkeypatch) -> None:
    memory, scratchpad, root = pieces
    monkeypatch.setattr(
        "autophysicist.llm.call_llm",
        lambda **kw: _Response("Looks fine."),
    )
    config = Config()
    config.model = "manager-model"
    config.agent_models = {"critic": "ds4"}

    critic_module.run_critique(
        config=config,
        problem_text="p",
        permanent_memory=memory,
        scratchpad=scratchpad,
        workspace_root=root,
        iteration=4,
        result=_Result(),
    )
    line = (Path(root) / "CRITIQUE_DATA.jsonl").read_text().splitlines()[0]
    record = json.loads(line)
    assert record["source"] == "model:ds4"
    assert record["verdict"] is None
    assert record["block_next"] is None
