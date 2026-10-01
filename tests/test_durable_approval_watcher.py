"""The durable-approval sweep: route the prompt, once, to the right chat.

Exercises ``GatewayRunner._deliver_durable_approvals`` against a fake runner,
source store and adapter — the real method, with only the routing seams
stubbed. Covers delivery, cache-miss fallback to the stored origin, the
no-route retry case, decided records, missing chat keys, and expiry pruning.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from gateway.run import GatewayRunner, _format_durable_approval
from tools import write_approval as wa


class _FakeAdapter:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, chat_id, text, metadata=None) -> None:
        self.sent.append((chat_id, text, metadata))


class _FakeAsyncSessionStore:
    async def _ensure_loaded(self) -> None:
        return None


class _FakeRunner:
    def __init__(self, *, cached_source=None, stored_entry=None, adapter=None) -> None:
        self.async_session_store = _FakeAsyncSessionStore()
        self.session_store = SimpleNamespace(_entries={})
        if stored_entry is not None:
            self.session_store._entries["chat-a"] = stored_entry
        self._cached_source = cached_source
        self._adapter = adapter

    def _get_cached_session_source(self, session_key):
        return self._cached_source

    def _adapter_for_source(self, source):
        return self._adapter

    def _thread_metadata_for_target(self, *args, **kwargs):
        return {"routed": True}


def _stage(chat_key="chat-a", *, expires_at=None) -> dict:
    return wa.stage_write(
        wa.EXEC,
        {
            "action": "exec_approval",
            "command": "rm -rf /tmp/x",
            "description": "dangerous rm",
        },
        summary="dangerous rm",
        origin="foreground",
        expires_at=expires_at,
        chat_key=chat_key,
    )


def _sweep(runner) -> int:
    return asyncio.run(GatewayRunner._deliver_durable_approvals(runner))


def test_sweep_sends_the_prompt_and_stamps_notified() -> None:
    record = _stage()
    adapter = _FakeAdapter()
    source = SimpleNamespace(platform="signal", chat_id="chat-1", thread_id=None, chat_type="dm")
    runner = _FakeRunner(cached_source=source, adapter=adapter)

    assert _sweep(runner) == 1
    assert len(adapter.sent) == 1
    chat_id, text, metadata = adapter.sent[0]
    assert chat_id == "chat-1"
    assert metadata == {"routed": True}
    assert "rm -rf /tmp/x" in text
    assert f"/approve {record['id']}" in text
    assert wa.get_pending(wa.EXEC, record["id"])["notified_at"]


def test_sweep_is_idempotent_and_skips_decided_records() -> None:
    record = _stage()
    wa.decide_pending(wa.EXEC, record["id"], "deny", decided_by="chat-a")
    adapter = _FakeAdapter()
    source = SimpleNamespace(platform="signal", chat_id="chat-1", thread_id=None, chat_type="dm")
    runner = _FakeRunner(cached_source=source, adapter=adapter)

    assert _sweep(runner) == 0
    assert adapter.sent == []


def test_source_cache_miss_falls_back_to_the_stored_origin() -> None:
    record = _stage()
    adapter = _FakeAdapter()
    origin = SimpleNamespace(platform="signal", chat_id="chat-9", thread_id=None, chat_type="dm")
    runner = _FakeRunner(
        cached_source=None,
        stored_entry=SimpleNamespace(origin=origin),
        adapter=adapter,
    )

    assert _sweep(runner) == 1
    assert adapter.sent[0][0] == "chat-9"
    assert wa.get_pending(wa.EXEC, record["id"])["notified_at"]


def test_sweep_retries_an_unroutable_request() -> None:
    record = _stage()
    adapter = _FakeAdapter()
    runner = _FakeRunner(cached_source=None, stored_entry=None, adapter=adapter)

    assert _sweep(runner) == 0
    assert adapter.sent == []
    # Still pending and un-notified: the next sweep retries it.
    stored = wa.get_pending(wa.EXEC, record["id"])
    assert stored is not None
    assert not stored.get("notified_at")


def test_sweep_skips_a_record_without_a_chat_key() -> None:
    record = _stage(chat_key="")
    adapter = _FakeAdapter()
    source = SimpleNamespace(platform="signal", chat_id="chat-1", thread_id=None, chat_type="dm")
    runner = _FakeRunner(cached_source=source, adapter=adapter)

    assert _sweep(runner) == 0
    assert adapter.sent == []
    assert not wa.get_pending(wa.EXEC, record["id"]).get("notified_at")


def test_sweep_prunes_expired_records() -> None:
    record = _stage(expires_at=time.time() - 1)
    runner = _FakeRunner()

    assert _sweep(runner) == 0
    assert wa.get_pending(wa.EXEC, record["id"]) is None


def test_prompt_format_is_shared_with_the_relay() -> None:
    record = _stage()
    assert _format_durable_approval(record).startswith("⚠️ Approval needed")
