"""Background delegation results reach the conversation, and are never lost.

``delegate_task(background=true)`` finishes on a daemon thread and leaves a
durable completion row in state.db plus an event on the shared completion
queue. The interactive front-end drains that event into the backend's input
queue and runs it as a turn. Contracts, exercised against the real
``TextualBackend`` and the real ``tools.async_delegation`` store under the temp
``SON_OF_ANTON_HOME``:

  * a result is acknowledged as delivered only once a turn has taken it into
    the transcript — a turn that is refused before it starts (e.g. the session
    turn lease) puts it back on the queue, a bounded number of times, and then
    leaves it pending in state.db for the next start;
  * results still queued at shutdown go back to state.db, replayable at once;
  * an idle app runs backend-queued work without waiting for user input, and
    never runs two dispatch chains at once.
"""

from __future__ import annotations

import asyncio
import queue
import time

import pytest

from son_of_anton_tui import tui as _tui


def _textual() -> None:
    if not _tui.is_available():
        pytest.skip("textual not installed (opt-in tui extra)")


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
    from tools import async_delegation
    from tools.process_registry import process_registry

    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    b = TextualBackend(model="test-model", provider="openai", api_key="sk-test",
                       base_url="https://example.invalid/v1")
    b.attach(_Recorder(), feed_width=90)
    assert b.prepare_interactive_state()
    try:
        yield b
    finally:
        b.detach()
        b._release_active_session()
        db = getattr(b, "_session_db", None)
        if db is not None:
            db.close()
        async_delegation._reset_for_tests()


def _finished_background_batch(session_id: str) -> str:
    """Dispatch a real background batch that finishes at once; return its id."""
    from tools import async_delegation

    dispatch = async_delegation.dispatch_async_delegation_batch(
        goals=["compute 17*23", "compute 19*29"],
        context=None,
        toolsets=None,
        role="leaf",
        model="test-model",
        session_key=session_id,
        parent_session_id=session_id,
        runner=lambda: {
            "results": [
                {"task_index": 0, "status": "completed", "summary": "391"},
                {"task_index": 1, "status": "completed", "summary": "551"},
            ],
            "total_duration_seconds": 0.1,
        },
    )
    assert dispatch["status"] == "dispatched"
    delegation_id = dispatch["delegation_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        records = async_delegation.list_async_delegations()
        if any(r["delegation_id"] == delegation_id and r["status"] == "completed" for r in records):
            return delegation_id
        time.sleep(0.02)
    raise AssertionError("background batch never finished")


def _replayable(delegation_id: str) -> bool:
    """Would the next process replay this result? (state.db still pending)"""
    from tools import async_delegation

    q: queue.Queue = queue.Queue()
    async_delegation.restore_undelivered_completions(q)
    return any(evt.get("delegation_id") == delegation_id for evt in list(q.queue))


def _drained(b) -> list:
    b._drain_process_notifications("cli-idle")
    return b.drain_pending_input()


def _refused_turn(b):
    """A turn that never starts: the transcript does not change."""
    def chat(message, images=None):
        return "Stopped waiting for another process on this session. Your message was not processed."
    return chat


def _taking_turn(b):
    def chat(message, images=None):
        b.conversation_history.append({"role": "user", "content": message})
        b.conversation_history.append({"role": "assistant", "content": "noted"})
        return "noted"
    return chat


def test_result_is_delivered_only_when_a_turn_takes_it(backend) -> None:
    b = backend
    delegation_id = _finished_background_batch(b.session_id)

    pending = _drained(b)
    assert len(pending) == 1 and delegation_id in pending[0]
    # Queued for a turn is not delivered.
    assert _replayable(delegation_id)

    b.chat = _refused_turn(b)
    b.run_turn(pending[0])
    requeued = b.drain_pending_input()
    assert requeued == pending, "a refused turn must put the result back"
    assert _replayable(delegation_id)

    b.chat = _taking_turn(b)
    b.run_turn(requeued[0])
    assert b.drain_pending_input() == []
    assert not _replayable(delegation_id)


def test_refused_turns_are_bounded_and_leave_the_result_for_the_next_start(backend) -> None:
    from tools import async_delegation

    b = backend
    delegation_id = _finished_background_batch(b.session_id)
    pending = _drained(b)
    b.chat = _refused_turn(b)

    attempts = 0
    while pending and attempts < 50:
        b.run_turn(pending[0])
        attempts += 1
        pending = b.drain_pending_input()
    assert pending == [], "re-queuing must stop"
    assert attempts < 50
    assert _replayable(delegation_id)
    # The claim was released, so the next consumer can take it immediately.
    assert async_delegation.claim_completion_delivery(delegation_id, "next-process")


def test_results_still_queued_at_shutdown_go_back_to_state_db(backend) -> None:
    from tools import async_delegation

    b = backend
    delegation_id = _finished_background_batch(b.session_id)
    pending = _drained(b)
    assert pending

    b._release_unsettled_notifications()

    assert _replayable(delegation_id)
    assert async_delegation.claim_completion_delivery(delegation_id, "next-process")


async def _wait_until(pilot, predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


def test_idle_app_runs_backend_queued_work(backend) -> None:
    _textual()
    b = backend
    b.detach()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            dispatched: list = []

            async def fake_dispatch(value, images=None):
                dispatched.append(value)
                await app._after_dispatch()

            app._dispatch = fake_dispatch
            b._pending_input.put("[ASYNC DELEGATION BATCH COMPLETE — deleg_x] ...")
            assert await _wait_until(pilot, lambda: dispatched)
            assert dispatched == ["[ASYNC DELEGATION BATCH COMPLETE — deleg_x] ..."]

    asyncio.run(run())


def test_input_never_starts_a_second_concurrent_chain(backend) -> None:
    _textual()
    b = backend
    b.detach()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            seen: list = []
            active = 0
            peak = 0

            async def fake_dispatch(value, images=None):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                seen.append(value)
                await asyncio.sleep(0.4)
                active -= 1
                await app._after_dispatch()

            app._dispatch = fake_dispatch
            b._pending_input.put("result A")
            b._pending_input.put("result B")
            assert await _wait_until(pilot, lambda: "result A" in seen)
            # The user types while the chain is between (or inside) its turns.
            await app._submit_text("user follow-up")
            assert await _wait_until(pilot, lambda: len(seen) == 3 and active == 0)
            assert sorted(seen) == ["result A", "result B", "user follow-up"]
            assert peak == 1

    asyncio.run(run())
