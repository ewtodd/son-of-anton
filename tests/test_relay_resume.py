"""``/resume`` of a terminal session on a single_user instance relays it.

Switching the chat onto a terminal session ran that work inside the gateway,
in the wrong directory, under the chat's prompt. The chat now binds the
session (the relay runs it where it lives) and the chat agent learns of it
through an internal event between turns. Everything else keeps switching.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from son_of_anton_state import AsyncSessionDB, SessionDB


CHAT_KEY = "agent:main:signal:group:group:abc:+1555"


class _Runner:
    """The pieces of GatewayRunner /resume touches, around a real SessionDB."""

    def __init__(self, db: SessionDB, *, single_user: bool) -> None:
        from gateway.slash_commands import GatewaySlashCommandsMixin

        self.__class__ = type("Runner", (_Runner, GatewaySlashCommandsMixin), {})
        self.config = types.SimpleNamespace(single_user=single_user)
        self._session_db = AsyncSessionDB(db)
        self.injected: list[tuple[str, dict]] = []
        self.switched: list[str] = []
        origin = types.SimpleNamespace(platform=types.SimpleNamespace(value="signal"),
                                       chat_id="group:abc", chat_type="group")
        entry = types.SimpleNamespace(session_id="chat-session", origin=origin)
        self.session_store = types.SimpleNamespace(
            _ensure_loaded=lambda: None, _entries={CHAT_KEY: entry},
        )

        async def _get_or_create(source):
            return entry

        async def _switch(key, target):
            self.switched.append(target)
            return types.SimpleNamespace(session_id=target)

        async def _transcript(sid):
            return []

        self.async_session_store = types.SimpleNamespace(
            get_or_create_session=_get_or_create,
            switch_session=_switch,
            load_transcript=_transcript,
        )

    def _session_key_for_source(self, source):
        return CHAT_KEY

    async def _inject_watch_notification(self, text, evt):
        self.injected.append((text, evt))
        return True

    def _release_running_agent_state(self, key):
        pass

    def _clear_conversation_scope(self, key, reason=""):
        pass

    def _evict_cached_agent(self, key):
        pass

    def _resume_caller_is_admin(self, source):
        return False


def _event(text: str):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    source = SessionSource(platform=Platform.SIGNAL, chat_id="group:abc", chat_type="group",
                           user_id="+1555")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)


@pytest.fixture
def db(son_of_anton_home, tmp_path):
    instance = SessionDB()
    instance.create_session("20261015_100000_aaaaaa", source="cli", cwd=str(tmp_path))
    instance.set_session_title("20261015_100000_aaaaaa", "fix the parser")
    instance.create_session("20261015_100000_bbbbbb", source="signal")
    try:
        yield instance
    finally:
        instance.close()


def test_resuming_a_terminal_session_binds_it_instead_of_switching(db, tmp_path) -> None:
    runner = _Runner(db, single_user=True)
    reply = asyncio.run(runner._handle_resume_command(_event("/resume fix the parser")))

    assert "Relaying" in reply
    assert runner.switched == []
    binding = db.get_relay_binding("20261015_100000_aaaaaa")
    assert binding["chat_key"] == CHAT_KEY
    (text, evt), = runner.injected
    assert "20261015_100000_aaaaaa" in text and str(tmp_path) in text
    assert evt == {"session_key": CHAT_KEY, "parent_session_id": "chat-session"}


def test_a_chat_session_still_switches(db) -> None:
    runner = _Runner(db, single_user=True)
    asyncio.run(runner._handle_resume_command(_event("/resume 20261015_100000_bbbbbb")))
    assert runner.switched == ["20261015_100000_bbbbbb"]
    assert db.list_relay_bindings() == []


def test_a_shared_instance_never_relays(db, monkeypatch) -> None:
    runner = _Runner(db, single_user=False)

    async def _allowed(*a, **k):
        return True

    monkeypatch.setattr(type(runner), "_resume_target_allowed", _allowed)
    asyncio.run(runner._handle_resume_command(_event("/resume 20261015_100000_aaaaaa")))
    assert runner.switched == ["20261015_100000_aaaaaa"]
    assert runner.injected == []
