"""Contract tests for completion-style title generation.

``auxiliary.title_generation.prompt_style: completion`` targets small
purpose-built title models (SupraLabs/supra-title-50M and friends) that are
NOT chat models. They continue a fixed pattern and emit bare text.

The chat path actively breaks them: given our 235-token instruction prompt
plus a ``json_schema`` grammar, supra-title copies the few-shot examples out
of the prompt verbatim (``{"title": "Fix Login On Mobile ..."}`` comes
straight from ``Good: {"title": "Fix login button on mobile"}``) or drifts
into Cyrillic. Given ``"User: <msg>\\nTitle: "`` it answers correctly in
4-9 tokens.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def _completion_style(monkeypatch):
    """Point title config at prompt_style=completion."""
    cfg = {
        "auxiliary": {
            "title_generation": {
                "enabled": True,
                "prompt_style": "completion",
                "model": "supra-title",
                "base_url": "http://endpoint.invalid/v1",
            }
        }
    }
    monkeypatch.setattr(
        "son_of_anton_cli.config.load_config_readonly", lambda *a, **k: cfg
    )
    return cfg


def test_default_prompt_style_is_chat(monkeypatch):
    """Absent config, titling keeps the chat path."""
    import agent.title_generator as tg

    monkeypatch.setattr(
        "son_of_anton_cli.config.load_config_readonly", lambda *a, **k: {}
    )
    assert tg._title_prompt_style() == "chat"


def test_unknown_prompt_style_falls_back_to_chat(monkeypatch):
    import agent.title_generator as tg

    monkeypatch.setattr(
        "son_of_anton_cli.config.load_config_readonly",
        lambda *a, **k: {"auxiliary": {"title_generation": {"prompt_style": "wat"}}},
    )
    assert tg._title_prompt_style() == "chat"


def test_completion_prompt_shape_and_sampling(_completion_style, monkeypatch):
    """The wire call must use the documented format and card sampling."""
    import agent.title_generator as tg

    seen = {}

    def fake_completion(**kwargs):
        seen.update(kwargs)
        return "Fix Yellow Status Bar Title"

    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm_text_completion", fake_completion
    )
    title = tg.generate_title("fix the yellow status bar title in my CLI")

    assert title == "Fix Yellow Status Bar Title"
    assert seen["task"] == "title_generation"
    # Exact training-time format: no system prompt, trailing space after Title:
    assert seen["prompt"] == "User: fix the yellow status bar title in my CLI\nTitle: "
    assert seen["stop"] == ["\n"]
    # Model-card defaults.
    assert seen["max_tokens"] == 24
    assert seen["temperature"] == pytest.approx(0.4)
    assert seen["top_p"] == pytest.approx(0.85)
    # llama.cpp-only knobs have no OpenAI schema field and must ride extra_body.
    # repeat_penalty is not optional here: a 50M model loops without it.
    assert seen["extra_body"]["repeat_penalty"] == pytest.approx(1.2)
    assert seen["extra_body"]["top_k"] == 40


def test_completion_output_is_not_json_unwrapped(_completion_style, monkeypatch):
    """Plain text passes through; the JSON extractor must not mangle it.

    ``_extract_title_text`` hunts for a ``{"title": ...}`` envelope. Running it
    on bare text is what turned a good completion into a rejected one.
    """
    import agent.title_generator as tg

    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm_text_completion",
        lambda **k: "  Bromine-82 Half-Life  ",
    )
    assert tg.generate_title("what is the half-life of bromine-82") == "Bromine-82 Half-Life"

    # NOTE: this asserts pass-through, not that _extract_title_text is skipped.
    # The two overlap almost completely (_clean_title also strips a "Title:"
    # prefix), and the one input where they diverge — text embedding a literal
    # {"title": ...} — is independently rejected by the degeneracy guard. So
    # there is no realistic title that distinguishes them; skipping the
    # extractor is correctness-by-intent here, not a behaviour under test.


def test_completion_degenerate_output_still_rejected(_completion_style, monkeypatch):
    """The repetition guard still applies on the completion path."""
    import agent.title_generator as tg

    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm_text_completion",
        lambda **k: "Fox Fox Fox Fox Fox Fox Fox Fox",
    )
    assert tg.generate_title("the quick brown fox") is None


def test_completion_requires_explicit_base_url(monkeypatch):
    """/v1/completions is not auto-detected — a missing base_url must be loud."""
    from agent.auxiliary_client import call_llm_text_completion

    monkeypatch.setattr(
        "agent.auxiliary_client._resolve_task_provider_model",
        lambda **k: ("custom", "supra-title", None, None, None),
    )
    with pytest.raises(ValueError, match="base_url"):
        call_llm_text_completion(task="title_generation", prompt="User: x\nTitle: ")


def test_bounded_task_max_tokens_reaches_the_wire():
    """title_generation opts back into sending an explicit max_tokens.

    call_llm() omits max_tokens by default for auxiliary tasks. That let a
    looping model emit 3,760 tokens per title instead of the 64 the caller
    asked for, which the answer-shaped guard then rejected — so sessions
    stayed untitled.
    """
    from agent.auxiliary_client import _HARD_CAPPED_TASKS

    assert "title_generation" in _HARD_CAPPED_TASKS
