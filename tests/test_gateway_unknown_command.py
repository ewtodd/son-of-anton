"""A chat `/word` is a command or nothing — never a skill.

Skill slash commands are gone: a skill installed on disk no longer turns
``/its-name`` into a turn that loads the skill's body. Typed in a chat, such a
word is an unknown command like any other, answered by the dispatcher and
never handed to the agent. Drives a real GatewayRunner._handle_message; only
authorization and the final agent run are stubbed.
"""

from __future__ import annotations

import asyncio

import pytest


def _write_skill(home, name: str) -> None:
    skill_dir = home / "skills" / "demo" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name = \"{name}\"\n"
        "description = \"Demo skill for the gateway command guard.\"\n"
        "---\n\n"
        f"# {name}\n\nDo the demo thing.\n",
        encoding="utf-8",
    )


def _send(text: str):
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = GatewayRunner(GatewayConfig())
    runner._is_user_authorized = lambda source: True
    sent: list = []

    async def fake_agent(event, source, quick_key, run_generation):
        sent.append(event.text)
        return {"final_response": "ok"}

    runner._handle_message_with_agent = fake_agent
    source = SessionSource(
        platform=Platform.SIGNAL, chat_id="group:cmd", chat_type="group",
        user_id="+10000000000", user_name="tester",
    )
    event = MessageEvent(
        text=text, message_type=MessageType.TEXT, source=source, message_id="m1",
    )
    reply = asyncio.run(runner._handle_message(event))
    return reply, sent


@pytest.mark.parametrize("word", ["demo-skill", "demo_skill"])
def test_an_installed_skills_name_is_an_unknown_command(son_of_anton_home, word) -> None:
    _write_skill(son_of_anton_home, "demo-skill")
    reply, sent = _send(f"/{word} do the thing")
    assert sent == [], "the /word reached the agent"
    assert f"Unknown command `/{word}`" in str(reply)


def test_plain_text_naming_a_skill_still_reaches_the_agent(son_of_anton_home) -> None:
    _write_skill(son_of_anton_home, "demo-skill")
    _reply, sent = _send("use demo-skill to do the thing")
    assert sent == ["use demo-skill to do the thing"]
