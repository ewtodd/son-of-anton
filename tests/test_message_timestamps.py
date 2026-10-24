"""User messages carry their send time on the API copy, rendered once.

The prefix uses son-of-anton's zone (the host's unless `timezone` is set), is
persisted in the api_content sidecar, and replays byte-identically even after
the zone changes, so the provider prompt cache keeps its prefix.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import son_of_anton_time
from agent.turn_context import (
    compose_user_api_content,
    message_sent_at,
    prepend_sent_at_to_multimodal_content,
    substitute_api_content,
)
from son_of_anton_state import SessionDB

# Saturday 2026-10-24 19:32 UTC = 14:32 CDT.
SENT = datetime(2026, 10, 24, 19, 32, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def zone(monkeypatch: pytest.MonkeyPatch):
    def set_zone(name: str | None) -> None:
        if name is None:
            monkeypatch.delenv("SON_OF_ANTON_TIMEZONE", raising=False)
        else:
            monkeypatch.setenv("SON_OF_ANTON_TIMEZONE", name)
        son_of_anton_time.reset_cache()

    yield set_zone
    son_of_anton_time.reset_cache()


def test_send_time_prefixes_the_message_in_the_configured_zone(zone) -> None:
    zone("America/Chicago")
    assert compose_user_api_content("what time is it?", "", "", SENT) == "[Sat 2026-10-24 14:32 CDT] what time is it?"


def test_without_a_zone_the_host_zone_is_used(zone) -> None:
    zone(None)
    local = datetime.fromtimestamp(SENT).astimezone()
    assert compose_user_api_content("hi", "", "", SENT) == f"[{local.strftime('%a %Y-%m-%d %H:%M %Z')}] hi"


def test_injections_follow_the_prefixed_message(zone) -> None:
    zone("UTC")
    composed = compose_user_api_content("q", "<note>n</note>", "<recalled-notes>r</recalled-notes>", SENT)
    assert composed.startswith("[Sat 2026-10-24 19:32 UTC] q\n\n")
    assert composed.index("<recalled-notes>") < composed.index("<note>")


def test_nothing_to_inject_still_sends_as_is() -> None:
    assert compose_user_api_content("q", "", "", None) is None


def test_send_time_follows_the_setting_and_the_stored_metadata() -> None:
    on, off = SimpleNamespace(_message_timestamps=True), SimpleNamespace(_message_timestamps=False)
    assert message_sent_at(on, {"timestamp": SENT}) == SENT
    assert message_sent_at(off, {"timestamp": SENT}) is None
    assert message_sent_at(on, {}) is None


def test_multimodal_message_gets_a_leading_time_part(zone) -> None:
    zone("UTC")
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    assert prepend_sent_at_to_multimodal_content(content, SENT)
    assert content[0] == {"type": "text", "text": "[Sat 2026-10-24 19:32 UTC]"}
    assert content[1]["type"] == "image_url"


def test_replay_keeps_the_original_time_after_the_zone_changes(zone) -> None:
    zone("America/Chicago")
    sent = compose_user_api_content("remind me at 6", "", "", SENT)
    sid = "20261024_193200_ts"
    db = SessionDB()
    try:
        db.create_session(session_id=sid, source="cli", cwd="/tmp")
        db.append_message(sid, "user", "remind me at 6", api_content=sent)
        db.append_message(sid, "assistant", "ok")
        zone("UTC")
        history = db.get_messages_as_conversation(sid)
    finally:
        db.close()
    user_row = next(m for m in history if m["role"] == "user")
    substitute_api_content(user_row)
    assert user_row["content"] == sent
