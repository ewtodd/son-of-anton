"""Agent-build contract — constructing an AIAgent must not raise.

This catches the dead-reference class of regression (e.g. an orphaned call
into a removed subsystem that only fails at agent construction, invisible to
compile and import sweeps). The live gateway builds one AIAgent per message,
so this is exactly its first touchpoint.
"""

from __future__ import annotations

from run_agent import AIAgent


def test_agent_builds_cleanly(son_of_anton_home) -> None:
    from son_of_anton_cli.config import save_config

    save_config(
        {
            "model": {"default": "qwen3.6-35b-a3b", "provider": "custom"},
            "custom_providers": {
                "custom": {"base_url": "http://127.0.0.1:9/v1"},
            },
        }
    )

    agent = AIAgent(
        base_url="http://127.0.0.1:9/v1",  # unreachable — no LLM calls at build
        provider="custom",
        model="qwen3.6-35b-a3b",
        api_mode="chat_completions",
        max_iterations=1,
        quiet_mode=True,
        platform="cli",
        skip_memory=True,
        skip_context_files=True,
    )
    assert agent.provider == "custom"
    assert agent.model == "qwen3.6-35b-a3b"
