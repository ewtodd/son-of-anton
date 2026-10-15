"""/plan: a planning turn handed to the agent, from the terminal or a chat.

Like /commit it is a prompt, not a tool. The gateway case drives a
real GatewayRunner._handle_message; only authorization and the final agent
run are stubbed, so command resolution and the rewrite are the real ones.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from son_of_anton_cli.plan_command import build_plan_prompt


def test_plan_prompt_names_its_file_and_carries_the_task() -> None:
    prompt = build_plan_prompt("add a cache to the picker", now=datetime(2026, 10, 15, 9, 8, 7))
    assert prompt.startswith("Plan only")
    assert ".son-of-anton/plans/2026-10-15_090807-<slug>.md" in prompt
    assert "The task to plan: add a cache to the picker" in prompt
    assert "No task was given" not in prompt


def test_plan_without_a_task_plans_the_conversation() -> None:
    prompt = build_plan_prompt("   ")
    assert "No task was given" in prompt
    assert "The task to plan" not in prompt


def test_plan_from_a_chat_hands_the_agent_the_plan_prompt() -> None:
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = GatewayRunner(GatewayConfig())
    runner._is_user_authorized = lambda source: True
    sent: list = []

    async def fake_agent(event, source, quick_key, run_generation):
        sent.append(event.text)
        return {"final_response": "planned"}

    runner._handle_message_with_agent = fake_agent
    source = SessionSource(
        platform=Platform.SIGNAL, chat_id="group:plan", chat_type="group",
        user_id="+10000000000", user_name="tester",
    )
    event = MessageEvent(
        text="/plan add a cache to the picker", message_type=MessageType.TEXT,
        source=source, message_id="m1",
    )
    asyncio.run(runner._handle_message(event))
    assert len(sent) == 1, "the /plan message never reached the agent"
    assert sent[0].startswith("Plan only")
    assert "The task to plan: add a cache to the picker" in sent[0]
