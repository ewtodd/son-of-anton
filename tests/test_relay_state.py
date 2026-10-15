"""Relay state in SessionDB: which chat drives a terminal session, and its queue.

A relay chat starts a headless run on a terminal session or, while one is in
flight, queues the message; when the run ends the queue becomes the next run.
Both decisions are single transactions, so a message is never lost and never
run twice — under concurrent sends, a stop racing the exit, or a spawn that
fails. State is keyed by the compaction-lineage root, so a worker that
compacts stays bound.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading

import pytest

from son_of_anton_state import SessionDB


@pytest.fixture
def db(son_of_anton_home):
    instance = SessionDB()
    try:
        yield instance
    finally:
        instance.close()


def _live_pid() -> int:
    import os

    return os.getpid()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _texts(rows) -> list[str]:
    return [r["text"] for r in rows]


def test_send_starts_once_then_queues_and_the_queue_runs_next(db) -> None:
    first = db.relay_start_or_enqueue("s1", "chat-a", "one")
    assert first["action"] == "start"
    assert _texts(first["messages"]) == ["one"]
    assert db.set_relay_host_pid("s1", _live_pid())

    assert db.relay_start_or_enqueue("s1", "chat-a", "two") == {
        "action": "queued", "session_id": "s1", "position": 1, "host": "headless",
    }
    assert db.relay_start_or_enqueue("s1", "chat-a", "three")["position"] == 2

    after = db.relay_finish_run("s1", _live_pid())
    assert after["action"] == "start"
    assert after["chat_key"] == "chat-a"
    assert _texts(after["messages"]) == ["two", "three"]
    # The same exit seen twice (watcher retry, startup reconcile) acts once.
    assert db.relay_finish_run("s1", _live_pid())["action"] == "none"

    assert db.set_relay_host_pid("s1", _live_pid())
    assert db.relay_finish_run("s1", _live_pid())["action"] == "idle"
    binding = db.get_relay_binding("s1")
    assert binding["host"] is None and binding["live"] is False
    assert db.list_relay_inbox("s1") == []


def test_concurrent_sends_start_one_run_and_every_message_runs_once(db) -> None:
    texts = [f"m{i}" for i in range(12)]
    results = []
    barrier = threading.Barrier(len(texts))

    def _send(text: str) -> None:
        barrier.wait()
        results.append(db.relay_start_or_enqueue("s1", "chat-a", text))

    threads = [threading.Thread(target=_send, args=(t,)) for t in texts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    starts = [r for r in results if r["action"] == "start"]
    assert len(starts) == 1
    ran = _texts(starts[0]["messages"])
    assert db.set_relay_host_pid("s1", _live_pid())
    after = db.relay_finish_run("s1", _live_pid())
    if after["action"] == "start":
        ran += _texts(after["messages"])
    assert sorted(ran) == sorted(texts)  # all of them, each exactly once


def test_a_stop_fences_the_old_run_and_keeps_the_queue(db) -> None:
    db.relay_start_or_enqueue("s1", "chat-a", "one")
    pid = _live_pid()
    db.set_relay_host_pid("s1", pid)
    db.relay_start_or_enqueue("s1", "chat-a", "two")

    assert db.clear_relay_host("s1", pid)  # stop
    assert db.relay_finish_run("s1", pid)["action"] == "none"  # its exit starts nothing
    assert _texts(db.list_relay_inbox("s1")) == ["two"]

    nxt = db.relay_start_or_enqueue("s1", "chat-a", "three")
    assert nxt["action"] == "start"
    assert _texts(nxt["messages"]) == ["two", "three"]


def test_a_failed_spawn_puts_its_messages_back_in_order(db) -> None:
    start = db.relay_start_or_enqueue("s1", "chat-a", "one")
    db.clear_relay_host("s1")
    db.release_relay_inbox([m["id"] for m in start["messages"]])

    again = db.relay_start_or_enqueue("s1", "chat-a", "two")
    assert again["action"] == "start"
    assert _texts(again["messages"]) == ["one", "two"]


def test_a_stop_during_the_spawn_wins(db) -> None:
    db.relay_start_or_enqueue("s1", "chat-a", "one")
    db.clear_relay_host("s1")
    # The spawner must learn nobody wants its process.
    assert db.set_relay_host_pid("s1", _live_pid()) is False


def test_a_dead_host_does_not_hold_the_queue(db) -> None:
    db.relay_start_or_enqueue("s1", "chat-a", "one")
    db.set_relay_host_pid("s1", _dead_pid())
    assert db.get_relay_binding("s1")["live"] is False
    assert db.relay_start_or_enqueue("s1", "chat-a", "two")["action"] == "start"


def test_a_compacted_worker_stays_bound_under_its_root(db) -> None:
    db.create_session("root", source="cli")
    db.end_session("root", "compaction")
    db.create_session("child", source="cli", parent_session_id="root")

    bound = db.bind_relay_session("child", "chat-a")
    assert bound["session_id"] == "root"
    assert db.get_relay_binding("child")["session_id"] == "root"
    db.relay_start_or_enqueue("child", "chat-a", "one")
    assert db.relay_start_or_enqueue("root", "chat-a", "two")["action"] == "queued"


def test_detach_waits_for_the_run_and_takes_the_queue_with_it(db) -> None:
    db.relay_start_or_enqueue("s1", "chat-a", "one")
    db.set_relay_host_pid("s1", _live_pid())
    db.relay_start_or_enqueue("s1", "chat-a", "two")
    assert db.unbind_relay_session("s1") == {"unbound": False, "running": True}

    db.clear_relay_host("s1")
    assert db.unbind_relay_session("s1") == {"unbound": True, "dropped": 1}
    assert db.get_relay_binding("s1") is None
    assert db.list_relay_inbox("s1") == []


def test_a_chats_bindings_come_back_most_recent_first(db) -> None:
    db.bind_relay_session("old", "chat-a")
    db.bind_relay_session("other", "chat-b")
    db.bind_relay_session("new", "chat-a")
    assert [b["session_id"] for b in db.list_relay_bindings("chat-a")] == ["new", "old"]
    # Binding from another chat moves the session.
    db.bind_relay_session("old", "chat-b")
    assert [b["session_id"] for b in db.list_relay_bindings("chat-a")] == ["new"]


def test_v28_drops_the_handoff_columns_and_adds_the_relay_tables(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    SessionDB(db_path=db_path).close()
    raw = sqlite3.connect(db_path)
    raw.execute("DROP TABLE relay_bindings")
    raw.execute("DROP TABLE relay_inbox")
    for column in ("handoff_state", "handoff_platform", "handoff_error"):
        raw.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")
    raw.execute(
        "CREATE INDEX idx_sessions_handoff_state ON sessions(handoff_state, started_at)"
    )
    raw.execute(
        "INSERT INTO sessions (id, source, started_at, handoff_state) "
        "VALUES ('kept', 'cli', 1, 'completed')"
    )
    raw.execute("UPDATE schema_version SET version = 27")
    raw.commit()
    raw.close()

    SessionDB(db_path=db_path).close()

    raw = sqlite3.connect(db_path)
    columns = {r[1] for r in raw.execute("PRAGMA table_info(sessions)")}
    indexes = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    rows = raw.execute("SELECT id FROM sessions").fetchall()
    raw.close()
    assert not {c for c in columns if c.startswith("handoff_")}
    assert "idx_sessions_handoff_state" not in indexes
    assert {"relay_bindings", "relay_inbox"} <= tables
    assert rows == [("kept",)]
