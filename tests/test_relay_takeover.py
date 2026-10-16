"""/pickup takes a session back from a headless relay worker mid-turn.

The terminal asks the worker to yield (``relay_request_yield``) and waits for
it to exit; the gateway, seeing the exit, leaves the session and its queue to
that terminal instead of starting the next run or reporting a finished turn;
the terminal picks it up, continues the turn the worker left open and then
runs what the chat sent meanwhile. These tests drive SessionDB, the gateway's
exit handling and the terminal side with real processes standing in for the
worker and the terminal.
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

SID = "20261015_130000_take01"


@pytest.fixture
def db(son_of_anton_home):
    instance = SessionDB()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def procs():
    started: list = []

    def _spawn() -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        started.append(proc)
        return proc

    yield _spawn
    for proc in started:
        proc.kill()
        proc.wait()


def _end(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait()


def _worker_running(db: SessionDB, worker: subprocess.Popen, *queued: str) -> None:
    db.create_session(SID, source="cli", cwd="/tmp")
    assert db.relay_start_or_enqueue(SID, "chat-a", "first")["action"] == "start"
    assert db.set_relay_host_pid(SID, worker.pid)
    for text in queued:
        db.relay_start_or_enqueue(SID, "chat-a", text)


# ── the state ────────────────────────────────────────────────────────────


def test_while_a_terminal_takes_it_back_nothing_else_starts_a_turn(db, procs) -> None:
    worker, other = procs(), procs()
    _worker_running(db, worker)
    taker = os.getpid()
    assert db.relay_request_yield(SID, taker) == {
        "requested": True, "session_id": SID, "host_pid": worker.pid,
    }
    assert db.get_relay_binding(SID)["yielding"] is True

    # A send waits for the terminal — even once the worker has exited.
    assert db.relay_start_or_enqueue(SID, "chat-a", "meanwhile")["yielding"] is True
    _end(worker)
    late = db.relay_start_or_enqueue(SID, "chat-a", "later")
    assert late["action"] == "queued" and late["yielding"] is True
    # No other terminal takes it, hands it off, or detaches it meanwhile.
    assert db.relay_request_yield(SID, other.pid)["taker_pid"] == taker
    assert db.relay_pickup(SID, other.pid)["picked_up"] is False
    assert db.relay_tui_bind(SID, "chat-a", other.pid)["bound"] is False
    assert db.unbind_relay_session(SID) == {"unbound": False, "yielding": True}

    picked = db.relay_pickup(SID, taker, event={})
    assert picked["picked_up"] is True and picked["took_over"] is True
    assert [m["text"] for m in picked["messages"]] == ["meanwhile", "later"]
    assert db.get_relay_binding(SID) is None


def test_a_dead_terminals_request_is_void(db, procs) -> None:
    worker, gone, second = procs(), procs(), procs()
    _worker_running(db, worker, "queued")
    assert db.relay_request_yield(SID, gone.pid)["requested"]
    _end(gone)
    assert db.get_relay_binding(SID)["yielding"] is False
    assert db.relay_request_yield(SID, second.pid)["requested"], "another terminal may ask"
    _end(second)
    assert db.relay_confirm_yield(SID) is False, "the worker carries on"
    assert db.get_relay_binding(SID)["yield_pid"] is None
    # ...and its exit runs the queue as if nobody had asked.
    _end(worker)
    after = db.relay_finish_run(SID, worker.pid)
    assert after["action"] == "start" and [m["text"] for m in after["messages"]] == ["queued"]


def test_giving_up_and_handing_over_are_decided_once(db, procs) -> None:
    worker = procs()
    _worker_running(db, worker)
    me = os.getpid()

    # Ctrl+C before the worker committed: withdrawn; the worker carries on.
    db.relay_request_yield(SID, me)
    assert db.relay_cancel_yield(SID, me) is True
    assert db.relay_yield_requested(SID) is None
    assert db.relay_confirm_yield(SID) is False

    # The worker committed first: too late to withdraw.
    db.relay_request_yield(SID, me)
    first = db.relay_yield_requested(SID)
    assert first["taker_pid"] == me
    assert db.relay_confirm_yield(SID) is True
    assert db.relay_cancel_yield(SID, me) is False

    # A worker that already exited cannot be left running either.
    db._conn.execute("UPDATE relay_bindings SET yielded_at = NULL WHERE session_id = ?", (SID,))
    db._conn.commit()
    _end(worker)
    assert db.relay_cancel_yield(SID, me) is False


def test_a_new_request_is_a_new_one_for_the_worker(db, procs) -> None:
    worker = procs()
    _worker_running(db, worker)
    me = os.getpid()
    db.relay_request_yield(SID, me)
    first = db.relay_yield_requested(SID)
    db.relay_cancel_yield(SID, me)
    time.sleep(0.01)
    db.relay_request_yield(SID, me)
    assert db.relay_yield_requested(SID)["at"] != first["at"]


def test_nothing_to_wait_for_without_a_live_headless_worker(db, procs) -> None:
    assert db.relay_request_yield(SID, os.getpid())["requested"] is False  # not relayed
    db.relay_tui_bind(SID, "chat-a", os.getpid())
    assert db.relay_request_yield(SID, os.getpid() + 1)["requested"] is False  # a TUI host
    worker = procs()
    db.clear_relay_host(SID)
    db.relay_start_or_enqueue(SID, "chat-a", "x")
    db.set_relay_host_pid(SID, worker.pid)
    _end(worker)
    assert db.relay_request_yield(SID, os.getpid())["requested"] is False  # already gone


def test_v30_adds_the_takeover_columns(tmp_path) -> None:
    path = tmp_path / "state.db"
    SessionDB(db_path=path).close()
    raw = sqlite3.connect(path)
    for column in ("yield_pid", "yield_at", "yielded_at"):
        raw.execute(f"ALTER TABLE relay_bindings DROP COLUMN {column}")
    raw.execute("UPDATE schema_version SET version = 29")
    raw.commit()
    raw.close()

    reopened = SessionDB(db_path=path)
    try:
        reopened.relay_start_or_enqueue("s1", "chat-a", "one")
        reopened.set_relay_host_pid("s1", os.getpid())
        assert reopened.relay_request_yield("s1", os.getpid())["requested"] is True
    finally:
        reopened.close()


# ── the gateway sees the worker exit ─────────────────────────────────────


def _runner(db: SessionDB):
    origin = types.SimpleNamespace(platform=types.SimpleNamespace(value="signal"),
                                   chat_id="group:abc", chat_type="group")
    entry = types.SimpleNamespace(session_id="chat-session", origin=origin)
    return types.SimpleNamespace(
        config=types.SimpleNamespace(single_user=True),
        _session_db=AsyncSessionDB(db),
        session_store=types.SimpleNamespace(_ensure_loaded=lambda: None, _entries={"chat-a": entry}),
    )


def _watcher(worker: subprocess.Popen) -> dict:
    return {"relay_session_id": SID, "relay_host_pid": worker.pid, "session_key": "chat-a",
            "parent_session_id": "chat-session"}


def test_a_yielded_exit_is_a_pickup_not_a_finished_turn(db, procs, monkeypatch) -> None:
    from gateway import relay_workers

    worker = procs()
    _worker_running(db, worker, "queued for later")
    runner = _runner(db)
    started: list = []

    async def _no_start(*args, **kwargs):
        started.append(args)
        return {"started": True}

    monkeypatch.setattr(relay_workers, "start_run", _no_start)
    db.relay_request_yield(SID, os.getpid())
    db.relay_confirm_yield(SID)
    _end(worker)

    # The gateway sees it first: nothing starts, nothing is reported.
    relay = asyncio.run(relay_workers.on_worker_exit(runner, _watcher(worker)))
    assert relay["action"] == "yielded" and started == []
    evt = {"session_id": "proc_x", "exit_code": 75, "output": "[relay: handed the turn to the terminal]"}
    assert relay_workers.format_completion(evt, relay) == ""
    assert [m["text"] for m in db.list_relay_inbox(SID)] == ["queued for later"]
    status = asyncio.run(relay_workers.handle_relay_action(runner, {"action": "status"}, chat_key="chat-a"))
    assert status["state"].startswith("yielding to the terminal")

    picked = db.relay_pickup(SID, os.getpid(), event={})
    assert picked["took_over"] and [m["text"] for m in picked["messages"]] == ["queued for later"]
    assert [e["kind"] for e in db.claim_relay_outbox()] == ["pickup"]


def test_the_terminal_can_get_there_before_the_gateway(db, procs) -> None:
    from gateway import relay_workers

    worker = procs()
    _worker_running(db, worker)
    db.relay_request_yield(SID, os.getpid())
    _end(worker)
    assert db.relay_pickup(SID, os.getpid(), event={})["picked_up"]
    relay = asyncio.run(relay_workers.on_worker_exit(_runner(db), _watcher(worker)))
    assert relay["action"] == "picked_up"
    assert relay_workers.format_completion({"exit_code": 75}, relay) == ""


def test_status_says_a_worker_is_yielding(db, procs) -> None:
    from gateway import relay_workers

    worker = procs()
    _worker_running(db, worker)
    runner = _runner(db)

    def act(**args):
        return asyncio.run(relay_workers.handle_relay_action(runner, args, chat_key="chat-a"))

    db.relay_request_yield(SID, os.getpid())
    status = act(action="status")
    assert status["host"] == "headless" and "stops after its current step" in status["state"]
    sent = act(action="send", message="one more thing")
    assert sent["host"] == "terminal" and "taking this session back" in sent["note"]
    assert "taking this session back" in act(action="detach")["error"]


def test_an_orphaned_handover_goes_back_to_the_chat(db, procs, monkeypatch) -> None:
    # The terminal dies after the worker yielded to it but before picking up.
    from gateway import relay_workers

    worker, taker = procs(), procs()
    _worker_running(db, worker, "waiting")
    db.relay_request_yield(SID, taker.pid)
    _end(worker)
    assert db.relay_finish_run(SID, worker.pid)["action"] == "yielded"
    _end(taker)
    started: list = []

    async def _fake_start(runner, root, messages, *, chat_key, cwd_hint=None):
        started.append((root, [m["text"] for m in messages]))
        return {"started": True}

    monkeypatch.setattr(relay_workers, "start_run", _fake_start)
    asyncio.run(relay_workers.sweep_tui_hosts(_runner(db)))
    assert started == [(SID, ["waiting"])]


# ── the terminal waits ───────────────────────────────────────────────────


def test_the_terminal_waits_for_the_worker_and_notices_a_crash(db, procs) -> None:
    from son_of_anton_cli.relay_host import take_back

    worker = procs()
    _worker_running(db, worker)
    db.append_message(SID, "user", "go")
    db.append_message(SID, "assistant", "", tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "terminal", "arguments": '{"command": "make test"}'},
    }])
    said: list = []
    box: dict = {}
    thread = threading.Thread(
        target=lambda: box.setdefault("outcome", take_back(db, SID, os.getpid(), say=said.append,
                                                            poll_s=0.05)),
        daemon=True,
    )
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive(), "it waits while the worker runs"
    assert any("terminal (make test)" in line for line in said), "and says what is running"
    _end(worker)  # it dies without ever yielding
    thread.join(10)
    assert box["outcome"] == "taken"


def test_giving_up_the_wait_leaves_the_worker_running(db, procs) -> None:
    from son_of_anton_cli.relay_host import take_back

    worker = procs()
    _worker_running(db, worker)
    stop = threading.Event()
    box: dict = {}
    thread = threading.Thread(
        target=lambda: box.setdefault("outcome", take_back(db, SID, os.getpid(), say=lambda _t: None,
                                                            stop=stop, poll_s=0.05)),
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(10)
    assert box["outcome"] == "cancelled"
    binding = db.get_relay_binding(SID)
    assert binding["live"] and binding["host_pid"] == worker.pid and binding["yield_pid"] is None
