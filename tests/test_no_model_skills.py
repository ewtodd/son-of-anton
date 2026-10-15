"""Skills are not a model surface.

The model is never offered a skill tool, never sees a skills index in its
system prompt, and no model-facing text (prompt, tool result, compaction
summary) tells it to call a skill tool it does not have. Skills that remain
are reached only by the user (``/skill-name``, cron jobs that already carry
them). These tests fail if any of that comes back.
"""

from __future__ import annotations

import json

from run_agent import AIAgent

_SKILL_TOOL_NAMES = ("skills_list", "skill_view", "skill_manage")


def _write_skill(home, name: str) -> None:
    skill_dir = home / "skills" / "demo" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name = \"{name}\"\n"
        "description = \"Demo skill for the prompt guard.\"\n"
        "---\n\n"
        f"# {name}\n\nDo the demo thing.\n",
        encoding="utf-8",
    )


def _build_agent() -> AIAgent:
    from son_of_anton_cli.config import save_config

    save_config(
        {
            "model": {"default": "qwen3.6-35b-a3b", "provider": "custom"},
            "custom_providers": {
                "custom": {"base_url": "http://127.0.0.1:9/v1"},
            },
        }
    )
    return AIAgent(
        base_url="http://127.0.0.1:9/v1",  # unreachable — no LLM calls
        provider="custom",
        model="qwen3.6-35b-a3b",
        api_mode="chat_completions",
        max_iterations=1,
        quiet_mode=True,
        platform="cli",
        skip_memory=True,
        skip_context_files=True,
    )


def test_installed_skills_never_reach_the_system_prompt(son_of_anton_home) -> None:
    # A skill on disk is exactly what used to be rendered into an
    # <available_skills> index; it must not reach the model now.
    _write_skill(son_of_anton_home, "demo-skill")
    agent = _build_agent()

    offered = {t["function"]["name"] for t in (agent.tools or [])}
    assert not offered & set(_SKILL_TOOL_NAMES), "the agent was offered a skill tool"

    from agent.system_prompt import build_system_prompt

    prompt = build_system_prompt(agent)
    assert "demo-skill" not in prompt, "an installed skill leaked into the system prompt"
    assert "<available_skills>" not in prompt
    for name in _SKILL_TOOL_NAMES:
        assert name not in prompt, f"the system prompt names {name!r}"


def test_cronjob_tool_refuses_skills_instead_of_dropping_them() -> None:
    import model_tools  # noqa: F401
    from tools.registry import registry

    schema = registry.get_schema("cronjob")
    assert "skills" not in schema["parameters"]["properties"]

    # A model replaying an older call shape must learn the skill was not
    # attached, not get a job silently created without it.
    result = json.loads(
        registry.dispatch(
            "cronjob",
            {"action": "create", "schedule": "30m", "prompt": "x", "skills": ["demo"]},
        )
    )
    assert result.get("success") is False
    assert "prompt" in result.get("error", "")


def test_compaction_never_asks_for_a_skill_reload() -> None:
    # Old sessions still carry skill_view results. Compacting them must not
    # emit a "reload with skill_view(...)" instruction the model cannot obey.
    from agent.context_compactor import _summarize_tool_result

    summary = _summarize_tool_result(
        "skill_view", json.dumps({"name": "demo-skill"}), "x" * 20_000
    )
    assert "skill_view(" not in summary


def test_a_prompt_stored_with_a_skills_index_is_rebuilt() -> None:
    # A session resumed after the upgrade must not keep a stored prompt that
    # tells the model to load skills with tools it is no longer offered.
    from types import SimpleNamespace

    from agent.conversation_loop import _stored_prompt_matches_runtime

    agent = SimpleNamespace(model="m", provider="custom", platform="cli")
    current = "You are an agent.\nModel: m\nProvider: custom\nPlatform: cli"
    stale = current.replace(
        "You are an agent.",
        "You are an agent.\n<available_skills>\n  demo:\n    - demo-skill\n</available_skills>",
    )
    assert _stored_prompt_matches_runtime(agent, current) is True
    assert _stored_prompt_matches_runtime(agent, stale) is False
