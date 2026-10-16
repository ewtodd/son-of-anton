"""/pickup in the TUI takes a session back from a headless relay worker.

The real ``TextualBackend`` in the real Textual app (``App.run_test``); the
worker is a stand-in process whose binding the test drives the way a real
worker would (append its last tool result, commit the handover, exit). Only
``chat()`` is stubbed, so the test sees which turns the app runs and in what
order: the turn the worker left open first, then what the chat sent while it
was running.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from son_of_anton_tui import tui as _tui

CHAT_KEY = "agent:main:signal:group:group:abc:+1555"


def _textual() -> None:
    if not _tui.is_available():
        pytest.skip("textual not installed")


class _Recorder:
    def __init__(self) -> None:
        self.events: list = []

    def emit(self, kind: str, **payload) -> None:
        self.events.append((kind, payload))


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("SON_OF_ANTON_DEFER_AGENT_STARTUP", "1")
    from son_of_anton_tui.backend import TextualBackend

    b = TextualBackend(model="test-model", provider="openai", api_key="sk-test",
                       base_url="https://example.invalid/v1")
    b.attach(_Recorder(), feed_width=90)
    assert b.prepare_interactive_state()
    try:
        yield b
    finally:
        b.detach()
        b._release_active_session()
        if b._session_db is not None:
            b._session_db.close()


@pytest.fixture
def worker():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc
    proc.kill()
    proc.wait()


def _mid_turn_in_a_worker(b, worker) -> str:
    """A relayed turn the worker is in: its command is running."""
    db = b._session_db
    sid = b.session_id
    if db.get_session(sid) is None:
        db.create_session(sid, source="cli")
    assert db.relay_start_or_enqueue(sid, CHAT_KEY, "build it")["action"] == "start"
    assert db.set_relay_host_pid(sid, worker.pid)
    db.append_message(sid, "user", "build it")
    db.append_message(sid, "assistant", "", tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "terminal", "arguments": '{"command": "make all"}'},
    }])
    db.relay_start_or_enqueue(sid, CHAT_KEY, "and then deploy")  # queued behind it
    return sid


def _turns(b) -> list:
    turns: list = []

    def fake_chat(message, images=None, continue_turn=False):
        turns.append("<continue>" if continue_turn else message)
        return "ok"

    b.chat = fake_chat
    return turns


def _notes(app) -> str:
    feed = app.query_one("#feed")
    return "\n".join("".join(s.text for s in blk.lines) for blk in feed._blocks)


async def _wait(pilot, pred, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.05)
        if pred():
            return True
    return False


def test_pickup_waits_continues_the_turn_then_runs_what_waited(backend, worker, monkeypatch) -> None:
    _textual()
    monkeypatch.setattr(_tui, "RELAY_TICK_S", 0.1)
    b = backend
    sid = _mid_turn_in_a_worker(b, worker)
    db = b._session_db
    turns = _turns(b)

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.2)
            app.query_one("#input").text = "/pickup"
            await pilot.press("enter")
            assert await _wait(pilot, lambda: b.relay_waiting())
            assert await _wait(pilot, lambda: "terminal (make all)" in _notes(app))
            assert db.get_relay_binding(sid)["yielding"] is True
            await pilot.pause(0.5)
            assert turns == [], "nothing runs while the worker finishes its step"

            # The worker's command finishes; it yields and exits.
            db.append_message(sid, "tool", "BUILD OK", tool_call_id="c1")
            assert db.relay_confirm_yield(sid) is True
            worker.kill()
            worker.wait()

            assert await _wait(pilot, lambda: turns == ["<continue>", "and then deploy"])
            assert await _wait(pilot, lambda: app._busy is None)
            assert "continues here" in _notes(app)

    asyncio.run(run())
    assert db.get_relay_binding(sid) is None
    assert [e["kind"] for e in db.claim_relay_outbox()] == ["pickup"]
    roles = [m["role"] for m in b.conversation_history]
    assert roles[-3:] == ["user", "assistant", "tool"], "the worker's rows were loaded"


def test_ctrl_c_while_waiting_leaves_the_worker_running(backend, worker, monkeypatch) -> None:
    _textual()
    monkeypatch.setattr(_tui, "RELAY_TICK_S", 0.1)
    b = backend
    sid = _mid_turn_in_a_worker(b, worker)
    db = b._session_db
    turns = _turns(b)

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.2)
            app.query_one("#input").text = "/pickup"
            await pilot.press("enter")
            assert await _wait(pilot, lambda: b.relay_waiting())
            await pilot.press("ctrl+c")
            assert await _wait(pilot, lambda: not b.relay_waiting() and app._busy is None)
            assert "Stopped waiting" in _notes(app)
            assert app.is_running, "ctrl+c stopped the wait, not the app"

    asyncio.run(run())
    binding = db.get_relay_binding(sid)
    assert binding["live"] and binding["host_pid"] == worker.pid and binding["yield_pid"] is None
    assert turns == []
    assert [m["text"] for m in db.list_relay_inbox(sid)] == ["and then deploy"], "still queued for it"
    assert worker.poll() is None, "never killed"
