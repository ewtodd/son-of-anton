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


def test_identity_strings_do_not_claim_nous() -> None:
    """The agent's self-identity must not associate the fork with Nous.

    The rebrand removed the upstream vendor from the runtime identity: the
    seeded SOUL.md, the default agent identity, and the self-help guidance.
    The docs reference must point at the fork's own repo, not the dead
    upstream docs domain.
    """
    from agent.prompt_builder import (
        DEFAULT_AGENT_IDENTITY,
        SON_OF_ANTON_AGENT_HELP_GUIDANCE,
    )
    from son_of_anton_cli.default_soul import DEFAULT_SOUL_MD

    for text in (DEFAULT_AGENT_IDENTITY, SON_OF_ANTON_AGENT_HELP_GUIDANCE, DEFAULT_SOUL_MD):
        assert "Nous" not in text, text
        assert "Son of Anton Agent" in text
    assert "github.com/ewtodd/son-of-anton" in SON_OF_ANTON_AGENT_HELP_GUIDANCE
    assert "nousresearch.com" not in SON_OF_ANTON_AGENT_HELP_GUIDANCE


def test_cmd_gateway_entrypoint_has_no_orphaned_calls(monkeypatch) -> None:
    """``son-of-anton gateway`` startup must not hit a dead helper.

    Deployed regressions: the TUI/Nix-only sweeps removed helpers from
    ``son_of_anton_cli/main.py`` while ``cmd_gateway`` kept calling them, so
    the gateway service died with ``NameError`` on every restart. Compile and
    import sweeps cannot see function-body NameErrors — only execution can.
    """
    import types

    import son_of_anton_cli.main as main_mod
    from son_of_anton_cli import gateway

    seen = {}

    def fake_gateway_command(args) -> None:
        seen["args"] = args

    monkeypatch.setattr(gateway, "gateway_command", fake_gateway_command)

    args = types.SimpleNamespace(gateway_command="run")
    main_mod.cmd_gateway(args)
    assert seen["args"] is args
