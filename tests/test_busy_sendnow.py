"""Busy-follow-up steering: one queue behavior, /sendnow + /sendall.

A message that arrives while the gateway agent is running is queued for its
own next turn. ``/sendnow`` steers the live turn onto the oldest queued text
(or onto arbitrary text), and ``/sendall`` steers every queued text message at
once. Attachments and synthetic events cannot ride a redirect, so they stay
queued. These tests drive the GatewayRunner handler helpers against fakes —
the full runner needs a live platform adapter.
"""

from __future__ import annotations

import asyncio
import types

from gateway.platforms.base import MessageEvent, MessageType


class _FakeAgent:
    _supports_active_turn_redirect = True

    def __init__(self) -> None:
        self.redirects: list = []
        self.accept = True

    def redirect(self, text) -> bool:
        if not self.accept:
            return False
        self.redirects.append(text)
        return True


def _source():
    return types.SimpleNamespace(
        chat_id="chat:1",
        platform=types.SimpleNamespace(value="signal"),
        user_id="u1",
        user_name="alice",
    )


def _event(text: str, **kwargs) -> MessageEvent:
    return MessageEvent(text=text, source=_source(), message_id="m1", **kwargs)


def _text_event(text: str) -> MessageEvent:
    event = _event(text)
    event.message_type = MessageType.TEXT
    return event


def _media_event(text: str = "look at this") -> MessageEvent:
    event = _event(text, media_urls=["/tmp/photo.jpg"], media_types=["image/jpeg"])
    event.message_type = MessageType.PHOTO
    return event


def _runner(agent, *, slot=None, overflow=()):
    from gateway.run import GatewayRunner

    gw = object.__new__(GatewayRunner)
    state = types.SimpleNamespace(
        turn=types.SimpleNamespace(agent=agent),
        conversation=types.SimpleNamespace(queued_events=list(overflow)),
    )
    adapter = types.SimpleNamespace(_pending_messages={})
    if slot is not None:
        adapter._pending_messages["k"] = slot
    gw._peek_session_state = lambda key: state
    gw._adapter_for_source = lambda source: adapter
    return gw, state, adapter


def _queue(state, adapter):
    """Snapshot the FIFO as plain strings ('<media>' for attachment events)."""
    events = []
    slot = adapter._pending_messages.get("k")
    if slot is not None:
        events.append(slot)
    events.extend(state.conversation.queued_events)
    out = []
    for event in events:
        if getattr(event, "media_urls", None):
            out.append("<media>")
        else:
            out.append(event.text)
    return out


def _sendnow(gw, text: str = "") -> str:
    event = _event(f"/sendnow {text}".strip())
    return asyncio.run(gw._busy_sendnow_command(event, "k", _source()))


def _sendall(gw) -> str:
    return asyncio.run(gw._busy_sendall_command(_event("/sendall"), "k", _source()))


def test_sendnow_steers_the_oldest_queued_text() -> None:
    agent = _FakeAgent()
    gw, state, adapter = _runner(
        agent, slot=_text_event("first"), overflow=[_text_event("second"), _media_event()]
    )
    reply = _sendnow(gw)
    assert agent.redirects == ["first"]
    assert "Steered" in reply
    # The second message is promoted into the head slot; the photo stays last.
    assert _queue(state, adapter) == ["second", "<media>"]


def test_sendnow_skips_media_automation_and_finds_text() -> None:
    goal = _text_event("[Continuing toward your standing goal]\nGoal: keep going")
    internal = _text_event("plugin event")
    internal.internal = True
    agent = _FakeAgent()
    gw, state, adapter = _runner(
        agent,
        slot=_media_event(),
        overflow=[goal, internal, _text_event("real message")],
    )
    reply = _sendnow(gw)
    assert agent.redirects == ["real message"]
    assert "other queued item" in reply
    assert _queue(state, adapter) == ["<media>", goal.text, "plugin event"]


def test_sendnow_with_text_queues_when_redirect_declines() -> None:
    agent = _FakeAgent()
    agent.accept = False
    gw, state, adapter = _runner(agent)
    reply = _sendnow(gw, "steer me")
    assert agent.redirects == []
    assert "queued for the next turn" in reply
    assert _queue(state, adapter) == ["steer me"]


def test_sendnow_with_text_steers_without_touching_the_queue() -> None:
    agent = _FakeAgent()
    gw, state, adapter = _runner(agent, slot=_text_event("already queued"))
    reply = _sendnow(gw, "correction")
    assert agent.redirects == ["correction"]
    assert _queue(state, adapter) == ["already queued"]


def test_sendnow_with_nothing_queued_says_so() -> None:
    agent = _FakeAgent()
    gw, state, adapter = _runner(agent)
    assert "Nothing queued" in _sendnow(gw)
    assert agent.redirects == []


def test_sendall_joins_every_queued_text_and_keeps_media() -> None:
    agent = _FakeAgent()
    gw, state, adapter = _runner(
        agent,
        slot=_text_event("one"),
        overflow=[_media_event(), _text_event("two")],
    )
    reply = _sendall(gw)
    assert agent.redirects == ["one\ntwo"]
    assert "2 queued message" in reply
    assert "1 other queued item" in reply
    assert _queue(state, adapter) == ["<media>"]


def test_sendall_leaves_the_queue_alone_when_redirect_declines() -> None:
    agent = _FakeAgent()
    agent.accept = False
    gw, state, adapter = _runner(agent, slot=_text_event("one"), overflow=[_text_event("two")])
    reply = _sendall(gw)
    assert "queue is unchanged" in reply
    assert _queue(state, adapter) == ["one", "two"]


def test_sendall_with_only_media_reports_nothing_to_steer() -> None:
    agent = _FakeAgent()
    gw, state, adapter = _runner(agent, slot=_media_event())
    assert "attachments" in _sendall(gw)
    assert agent.redirects == []
    assert _queue(state, adapter) == ["<media>"]
