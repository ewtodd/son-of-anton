"""A cron run takes the memory scope of where its job delivers.

Mirrors RAG's placement of the same session (``agent.rag._cron_identity``):
delivery target's platform, else the creating chat, else cli; a job that is
gone fails closed to the shared store.
"""

from __future__ import annotations

from son_of_anton_constants import get_son_of_anton_home
from tools.memory_tool import resolve_memory_scope

SIGNAL_ORIGIN = {"platform": "signal", "chat_id": "+15551230000"}


def _cron_session(**job_kwargs) -> str:
    from cron.jobs import create_job

    job = create_job(prompt="check in", schedule="every 2h", **job_kwargs)
    return f"cron_{job['id']}_20260101_080000"


def _scope(session_id, config=None) -> str:
    return resolve_memory_scope("cron", config, session_id=session_id)


def test_job_delivering_to_signal_is_gateway() -> None:
    assert _scope(_cron_session(deliver="signal:+15551230000")) == "gateway"


def test_local_job_with_signal_origin_is_gateway() -> None:
    assert _scope(_cron_session(deliver="local", origin=SIGNAL_ORIGIN)) == "gateway"


def test_local_job_without_origin_is_cli() -> None:
    assert _scope(_cron_session(deliver="local")) == "cli"


def test_missing_job_or_unplaceable_session_is_shared() -> None:
    assert _scope("cron_deadbeef0000_20260101_080000") == "shared"
    assert _scope("not-a-cron-session") == "shared"
    assert resolve_memory_scope("cron") == "shared"


def test_config_pin_wins() -> None:
    sid = _cron_session(deliver="signal:+15551230000")
    assert _scope(sid, {"memory": {"scope": "cli"}}) == "cli"


def test_session_id_does_not_affect_other_platforms() -> None:
    sid = _cron_session(deliver="signal:+15551230000")
    assert resolve_memory_scope("cli", None, session_id=sid) == "cli"
    assert resolve_memory_scope("discord", None, session_id=sid) == "shared"


def test_memory_scope_agrees_with_rag_identity() -> None:
    from agent.rag import _cron_identity

    for kwargs in (
        {"deliver": "signal:+15551230000"},
        {"deliver": "local", "origin": SIGNAL_ORIGIN},
        {"deliver": "local"},
    ):
        sid = _cron_session(**kwargs)
        identity = _cron_identity(sid, {})
        assert identity is not None
        assert _scope(sid) == identity[0]


def test_cron_agent_init_loads_gateway_memory(son_of_anton_home) -> None:
    from run_agent import AIAgent
    from son_of_anton_cli.config import save_config

    save_config(
        {
            "model": {"default": "qwen3.6-35b-a3b", "provider": "custom"},
            "custom_providers": {"custom": {"base_url": "http://127.0.0.1:9/v1"}},
        }
    )
    memories = get_son_of_anton_home() / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    (memories / "MEMORY.gateway.md").write_text("call the owner by first name", encoding="utf-8")
    (memories / "MEMORY.cli.md").write_text("terminal-only fact", encoding="utf-8")
    sid = _cron_session(deliver="signal:+15551230000")
    agent = AIAgent(
        base_url="http://127.0.0.1:9/v1",
        provider="custom",
        model="qwen3.6-35b-a3b",
        api_mode="chat_completions",
        max_iterations=1,
        quiet_mode=True,
        platform="cron",
        session_id=sid,
        skip_context_files=True,
    )
    assert agent._memory_scope == "gateway"
    block = agent._memory_store.format_for_system_prompt("memory") or ""
    assert "call the owner by first name" in block
    assert "terminal-only fact" not in block
