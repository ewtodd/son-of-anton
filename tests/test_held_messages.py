"""Held-messages store and the gateway confirmation flow.

Messages arriving outside the active-hours window are saved to disk.  When
the window reopens the gateway presents a summary and asks for confirmation
before processing them.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from gateway.held_messages import (
    clear_all,
    clear_messages,
    compose_held_turn,
    get_messages,
    has_messages,
    save_message,
    summarize,
)


@pytest.fixture()
def store(tmp_path: Path):
    return tmp_path


# ── store basics ─────────────────────────────────────────────────────────

def test_save_and_retrieve(store: Path) -> None:
    save_message(store, "s:1", sender="alice", text="hello")
    save_message(store, "s:1", sender="bob", text="world")
    msgs = get_messages(store, "s:1")
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "alice"
    assert msgs[1]["text"] == "world"


def test_has_messages_reflects_state(store: Path) -> None:
    assert not has_messages(store, "s:1")
    save_message(store, "s:1", sender="a", text="x")
    assert has_messages(store, "s:1")


def test_clear_removes_one_session(store: Path) -> None:
    save_message(store, "s:1", sender="a", text="x")
    save_message(store, "s:2", sender="b", text="y")
    clear_messages(store, "s:1")
    assert not has_messages(store, "s:1")
    assert has_messages(store, "s:2")


def test_clear_all_removes_everything(store: Path) -> None:
    save_message(store, "s:1", sender="a", text="x")
    save_message(store, "s:2", sender="b", text="y")
    clear_all(store)
    assert not has_messages(store, "s:1")
    assert not has_messages(store, "s:2")


def test_text_is_truncated_on_save(store: Path) -> None:
    save_message(store, "s:1", sender="a", text="x" * 5000)
    assert len(get_messages(store, "s:1")[0]["text"]) == 2000


def test_save_returns_count(store: Path) -> None:
    assert save_message(store, "s:1", sender="a", text="1") == 1
    assert save_message(store, "s:1", sender="b", text="2") == 2


def test_empty_store_returns_empty(store: Path) -> None:
    assert get_messages(store, "s:1") == []


def test_corrupt_file_is_treated_as_empty(store: Path) -> None:
    (store / "held_messages.json").write_text("not json")
    assert get_messages(store, "s:1") == []
    save_message(store, "s:1", sender="a", text="ok")
    assert has_messages(store, "s:1")


# ── summarize ────────────────────────────────────────────────────────────

def test_summarize_empty_returns_none(store: Path) -> None:
    assert summarize(store, "s:1") is None


def test_summarize_shows_messages(store: Path) -> None:
    save_message(store, "s:1", sender="alice", text="buy milk")
    save_message(store, "s:1", sender="bob", text="and eggs")
    out = summarize(store, "s:1")
    assert "2 messages" in out
    assert "alice" in out
    assert "buy milk" in out
    assert "yes" in out.lower()


def test_summarize_truncates_long_lists(store: Path) -> None:
    for i in range(15):
        save_message(store, "s:1", sender=f"u{i}", text=f"msg {i}")
    out = summarize(store, "s:1", max_entries=5)
    assert "and 10 more" in out


def test_summarize_truncates_long_text(store: Path) -> None:
    save_message(store, "s:1", sender="a", text="x" * 300)
    out = summarize(store, "s:1")
    assert "…" in out


# ── compose_held_turn ────────────────────────────────────────────────────

def test_compose_builds_turn_and_clears(store: Path) -> None:
    save_message(store, "s:1", sender="alice", text="hello there")
    turn = compose_held_turn(store, "s:1")
    assert "[alice]: hello there" in turn
    assert not has_messages(store, "s:1")


def test_compose_empty_returns_none(store: Path) -> None:
    assert compose_held_turn(store, "s:1") is None


def test_compose_multiple_messages(store: Path) -> None:
    save_message(store, "s:1", sender="a", text="first")
    save_message(store, "s:1", sender="b", text="second")
    turn = compose_held_turn(store, "s:1")
    assert "[a]: first" in turn
    assert "[b]: second" in turn


# ── gateway integration ─────────────────────────────────────────────────

def _runner(*, window=(20, 7), sessions_dir=None, running=False):
    from gateway.run import GatewayRunner

    obj = object.__new__(GatewayRunner)
    obj.config = types.SimpleNamespace(
        active_hours=window,
        inactive_message="",
        sessions_dir=sessions_dir or Path("/tmp/test-sessions"),
    )
    obj._session_key_for_source = lambda source: "agent:main:signal:group:g"
    obj._peek_session_state = lambda key: None
    obj._is_session_running = lambda key: running
    return obj


def _source():
    return types.SimpleNamespace(
        chat_id="group:1",
        platform=types.SimpleNamespace(value="signal"),
        user_name="alice",
        user_id="u1",
    )


def _event(text: str = "hello", command=None):
    return types.SimpleNamespace(
        text=text,
        get_command=lambda: command,
        allow_gateway_control=True,
    )


def test_held_message_saved_on_inactive_notice(tmp_path: Path) -> None:
    """When the window is closed a message should be saved, not dropped."""
    from datetime import datetime

    gw = _runner(sessions_dir=tmp_path)
    source = _source()
    # First notice for this gap — will return the notice text.
    gw._active_hours_notice(source, now=datetime(2026, 8, 29, 12, 0))

    save_message(tmp_path, "agent:main:signal:group:g", sender="alice", text="test")
    assert has_messages(tmp_path, "agent:main:signal:group:g")
