"""/handoff state and its gateway side: the open TUI hosts a relayed session.

A TUI that handed its session off is that session's host: the relay chat's
sends queue to it while its heartbeat is fresh and its process runs, and fall
back to headless workers once it is gone. What the TUI tells the chat goes
through ``relay_outbox``, which the gateway delivers into the chat as internal
events — each event once, in order, and only marked delivered once the chat
took it.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types

import pytest

from son_of_anton_state import AsyncSessionDB, SessionDB


@pytest.fixture
def db(son_of_anton_home):
    instance = SessionDB()
    try:
        yield instance
    finally:
        instance.close()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _age_heartbeat(db: SessionDB, root: str, seconds: float) -> None:
    db._conn.execute(
        "UPDATE relay_bindings SET host_heartbeat = ? WHERE session_id = ?",
        (time.time() - seconds, root),
    )
    db._conn.commit()


# ── host liveness ────────────────────────────────────────────────────────


def test_a_fresh_tui_host_takes_sends_and_a_stale_or_dead_one_falls_back(db) -> None:
    pid = os.getpid()
    assert db.relay_tui_bind("s1", "chat-a", pid)["bound"]

    queued = db.relay_start_or_enqueue("s1", "chat-a", "one")
    assert queued["action"] == "queued" and queued["host"] == "tui"
    assert [m["text"] for m in db.relay_tui_claim("s1", pid)] == ["one"]

    # A hung TUI: its process runs, its heartbeat stopped.
    _age_heartbeat(db, "s1", db.RELAY_TUI_STALE_S + 1)
    assert db.get_relay_binding("s1")["live"] is False
    started = db.relay_start_or_enqueue("s1", "chat-a", "two")
    assert started["action"] == "start"
    assert [m["text"] for m in started["messages"]] == ["two"]
    # The TUI that comes back has lost the session for good.
    assert db.relay_tui_beat("s1", pid) is False
    assert db.relay_tui_claim("s1", pid) is None

    # A crashed TUI: fresh heartbeat, dead process.
    dead = _dead_pid()
    db.clear_relay_host("s1")
    assert db.relay_tui_bind("s2", "chat-a", dead)["bound"]
    assert db.get_relay_binding("s2")["live"] is False
    assert db.relay_start_or_enqueue("s2", "chat-a", "three")["action"] == "start"


def test_handoff_is_refused_while_a_headless_worker_runs_it(db) -> None:
    db.relay_start_or_enqueue("s1", "chat-a", "one")
    db.set_relay_host_pid("s1", os.getpid())  # a live worker
    refused = db.relay_tui_bind("s1", "chat-a", os.getpid() + 1, event={"title": None})
    assert refused["bound"] is False and refused["host"] == "headless"
    assert db.claim_relay_outbox() == [], "a refused handoff tells the chat nothing"


def test_a_tui_that_exits_keeps_the_binding_and_never_strands_a_message(db) -> None:
    pid = os.getpid()
    db.relay_tui_bind("s1", "chat-a", pid)
    assert db.relay_tui_release("s1", pid) is True
    binding = db.get_relay_binding("s1")
    assert binding["chat_key"] == "chat-a" and binding["host"] is None
    assert db.relay_start_or_enqueue("s1", "chat-a", "next")["action"] == "start"

    # A message that arrived after the last claim: the host stays until the
    # process is gone, and then the queue runs headless.
    db.clear_relay_host("s1")
    db.relay_tui_bind("s1", "chat-a", pid)
    assert db.relay_start_or_enqueue("s1", "chat-a", "late")["action"] == "queued"
    assert db.relay_tui_release("s1", pid) is False
    assert db.get_relay_binding("s1")["host"] == "tui"
    after = db.relay_finish_run("s1", pid)  # what the gateway does once it is gone
    assert after["action"] == "start" and [m["text"] for m in after["messages"]] == ["late"]
    assert db.get_relay_binding("s1")["host"] == "headless"


def test_pickup_unbinds_tells_the_chat_and_hands_back_what_waited(db) -> None:
    pid = os.getpid()
    db.relay_tui_bind("s1", "chat-a", pid, event={"title": "t"})
    db.relay_start_or_enqueue("s1", "chat-a", "unclaimed")
    db.enqueue_relay_message("s1", "", mode="stop")

    picked = db.relay_pickup("s1", pid, event={"title": "t"})
    assert picked["picked_up"] is True and picked["chat_key"] == "chat-a"
    assert [m["text"] for m in picked["messages"]] == ["unclaimed"]
    assert db.get_relay_binding("s1") is None
    assert [e["kind"] for e in db.claim_relay_outbox()] == ["handoff", "pickup"]
    # Not relayed any more: nothing to pick up, nothing more to tell.
    assert db.relay_pickup("s1", pid)["picked_up"] is False
    assert db.claim_relay_outbox() == []


def test_a_tui_event_follows_the_binding_and_stops_with_it(db) -> None:
    pid = os.getpid()
    db.relay_tui_bind("s1", "chat-a", pid)
    db.bind_relay_session("s1", "chat-b")  # the chat moved it
    db.add_relay_event("s1", pid, "turn_end", {"reply": "done"})
    db.unbind_relay_session("s1")  # detached: a TUI host is no run to wait for
    assert db.add_relay_event("s1", pid, "turn_end", {"reply": "late"}) is None
    events = db.claim_relay_outbox()
    assert [(e["chat_key"], e["kind"]) for e in events] == [("chat-b", "turn_end")]


# ── the outbox ───────────────────────────────────────────────────────────


def test_each_event_is_claimed_once_and_delivered_once(db) -> None:
    pid = os.getpid()
    db.relay_tui_bind("s1", "chat-a", pid, event={})
    for i in range(20):
        db.add_relay_event("s1", pid, "turn_end", {"reply": f"r{i}"})

    claimed: list = []
    barrier = threading.Barrier(4)

    def _claim() -> None:
        barrier.wait()
        claimed.extend(e["id"] for e in db.claim_relay_outbox(limit=50))

    threads = [threading.Thread(target=_claim) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(claimed) == sorted(set(claimed)) and len(claimed) == 21

    first, *rest = sorted(claimed)
    db.finish_relay_outbox(first, delivered=True)
    for event_id in rest:
        db.finish_relay_outbox(event_id, delivered=False)
    again = [e["id"] for e in db.claim_relay_outbox(limit=50)]
    assert again == rest, "a released event comes back in order; a delivered one never"


def test_an_event_claimed_by_a_dead_deliverer_comes_back_after_the_lease(db, monkeypatch) -> None:
    pid = os.getpid()
    db.relay_tui_bind("s1", "chat-a", pid, event={})
    assert len(db.claim_relay_outbox()) == 1
    assert db.claim_relay_outbox() == []
    monkeypatch.setattr(SessionDB, "RELAY_OUTBOX_LEASE_S", -1.0)
    assert [e["kind"] for e in db.claim_relay_outbox()] == ["handoff"]


def test_v29_adds_the_outbox(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    SessionDB(db_path=db_path).close()
    raw = sqlite3.connect(db_path)
    raw.execute("DROP TABLE relay_outbox")
    raw.execute("UPDATE schema_version SET version = 28")
    raw.commit()
    raw.close()

    reopened = SessionDB(db_path=db_path)
    try:
        reopened.relay_tui_bind("s1", "chat-a", os.getpid(), event={"title": "t"})
        assert [e["kind"] for e in reopened.claim_relay_outbox()] == ["handoff"]
    finally:
        reopened.close()


# ── the gateway delivers ─────────────────────────────────────────────────


class _RecordingAdapter:
    name = "signal"
    supports_async_delivery = True

    def __init__(self, fail: int = 0) -> None:
        self.events: list = []
        self.fail = fail

    async def handle_message(self, event) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("transport down")
        self.events.append(event)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult

        return SendResult(success=True, message_id="x")


def test_a_real_gateway_delivers_tui_events_in_order_once(son_of_anton_home, tmp_path) -> None:
    from gateway import relay_workers
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = GatewayRunner(GatewayConfig(single_user=True))
    source = SessionSource(platform=Platform.SIGNAL, chat_id="group:e2e", chat_type="group",
                           user_id="+10000000000", user_name="tester")
    adapter = _RecordingAdapter(fail=1)

    async def _go() -> None:
        entry = await runner.async_session_store.get_or_create_session(source)
        chat_key = runner._session_key_for_source(source)
        tui = runner._session_db._db
        tui.create_session("20261015_120000_aaaaaa", source="cli", cwd=str(tmp_path))
        pid = os.getpid()
        tui.relay_tui_bind("20261015_120000_aaaaaa", chat_key, pid,
                           event={"busy": True, "activity": "pytest -q"})
        tui.add_relay_event("20261015_120000_aaaaaa", pid, "turn_end", {"reply": "ALL GREEN"})

        # No adapter yet: nothing is delivered, nothing is lost.
        assert await relay_workers.deliver_relay_events(runner) == 0
        runner.adapters[Platform.SIGNAL] = adapter
        # The transport fails once: the first event waits, and so does the
        # second behind it — never out of order.
        assert await relay_workers.deliver_relay_events(runner) == 0
        assert await relay_workers.deliver_relay_events(runner) == 2
        assert await relay_workers.deliver_relay_events(runner) == 0  # never twice
        texts = [e.text for e in adapter.events]
        assert "handed terminal session 20261015_120000_aaaaaa" in texts[0]
        assert "running a turn right now (current step: pytest -q)" in texts[0]
        assert "finished a turn" in texts[1] and "ALL GREEN" in texts[1]
        for ev in adapter.events:
            assert ev.internal is True
            assert ev.source.chat_id == "group:e2e"
            assert ev.metadata["gateway_session_id"] == entry.session_id

    asyncio.run(_go())


def test_a_shared_gateway_delivers_nothing(son_of_anton_home) -> None:
    from gateway import relay_workers

    db = SessionDB()
    db.relay_tui_bind("s1", "chat-a", os.getpid(), event={})
    runner = types.SimpleNamespace(config=types.SimpleNamespace(single_user=False),
                                   _session_db=AsyncSessionDB(db))
    try:
        assert asyncio.run(relay_workers.deliver_relay_events(runner)) == 0
        assert len(db.claim_relay_outbox()) == 1, "kept for when it can be delivered"
    finally:
        db.close()


def test_a_crashed_tui_hands_its_queue_to_a_headless_worker(son_of_anton_home, monkeypatch) -> None:
    from gateway import relay_workers

    db = SessionDB()
    tui = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    db.relay_tui_bind("s1", "chat-a", tui.pid)
    assert db.relay_start_or_enqueue("s1", "chat-a", "before the crash")["action"] == "queued"
    tui.kill()  # the TUI dies before it claimed the message
    tui.wait()
    db.relay_tui_bind("s2", "chat-a", os.getpid())  # a live TUI is left alone
    started = []

    async def _fake_start(runner, root, messages, *, chat_key, cwd_hint=None):
        started.append((root, [m["text"] for m in messages], chat_key))
        return {"started": True}

    monkeypatch.setattr(relay_workers, "start_run", _fake_start)
    runner = types.SimpleNamespace(config=types.SimpleNamespace(single_user=True),
                                   _session_db=AsyncSessionDB(db))
    try:
        asyncio.run(relay_workers.sweep_tui_hosts(runner))
        assert started == [("s1", ["before the crash"], "chat-a")]
        assert db.get_relay_binding("s2")["host"] == "tui"
    finally:
        db.close()


# ── the relay tool against a TUI host ────────────────────────────────────


def _relay_runner(db: SessionDB):
    origin = types.SimpleNamespace(platform=types.SimpleNamespace(value="signal"),
                                   chat_id="group:abc", chat_type="group")
    entry = types.SimpleNamespace(session_id="chat-session", origin=origin)
    return types.SimpleNamespace(
        config=types.SimpleNamespace(single_user=True),
        _session_db=AsyncSessionDB(db),
        session_store=types.SimpleNamespace(_ensure_loaded=lambda: None,
                                            _entries={"chat-a": entry}),
    )


def test_the_relay_tool_never_kills_a_tui_and_steers_it_on_request(son_of_anton_home, tmp_path) -> None:
    from gateway import relay_workers

    db = SessionDB()
    db.create_session("20261015_120000_aaaaaa", source="cli", cwd=str(tmp_path))
    db.relay_tui_bind("20261015_120000_aaaaaa", "chat-a", os.getpid())
    runner = _relay_runner(db)

    def act(**args):
        return asyncio.run(relay_workers.handle_relay_action(runner, args, chat_key="chat-a"))

    try:
        sent = act(action="send", message="do it now", now=True)
        assert sent["host"] == "tui" and "steers the running turn" in sent["note"]
        assert act(action="list")["sessions"][0]["host"] == "tui"
        assert act(action="status")["host"] == "tui"

        stopped = act(action="stop")
        assert stopped["stopped"] is True and stopped["host"] == "tui"
        rows = db.relay_tui_claim("20261015_120000_aaaaaa", os.getpid())
        assert [(r["mode"], r["text"]) for r in rows] == [("now", "do it now"), ("stop", "")]
        assert db.get_relay_binding("20261015_120000_aaaaaa")["host"] == "tui", "still hosting"

        detached = act(action="detach")
        assert detached["unbound"] is True and "keeps the session" in detached["note"]
    finally:
        db.close()
