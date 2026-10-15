"""/handoff in the TUI: the open TUI keeps running the session for a relay chat.

The TUI backend is the real ``TextualBackend`` (a real ``cli.SonOfAntonCLI``)
under the temp home, and the app is the real Textual app driven through
``App.run_test``; only ``chat()`` is stubbed so a test decides what a turn
does. The relay chat's side is played with the same SessionDB calls the
gateway's relay tool makes (``relay_start_or_enqueue``), and its approval
answer with the gateway's own ``/approve`` resolver.
"""

from __future__ import annotations

import asyncio
import os
import threading
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

    def text(self) -> str:
        return "\n".join(p.get("text", "") for k, p in self.events if k == "ansi")


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("SON_OF_ANTON_DEFER_AGENT_STARTUP", "1")
    from son_of_anton_tui.backend import TextualBackend

    b = TextualBackend(model="test-model", provider="openai", api_key="sk-test",
                       base_url="https://example.invalid/v1")
    rec = _Recorder()
    b.attach(rec, feed_width=90)
    assert b.prepare_interactive_state()
    rec.events.clear()
    try:
        yield b, rec
    finally:
        b.detach()
        b._release_active_session()
        if b._session_db is not None:
            b._session_db.close()


def _single_user(home) -> None:
    (home / "config.toml").write_text("[gateway]\nsingle_user = true\n", encoding="utf-8")


def _signal_chat(b) -> None:
    b._session_db.create_session("20261015_090000_chat01", source="signal", session_key=CHAT_KEY)


def _handed_off(b, home):
    _single_user(home)
    _signal_chat(b)
    assert b.run_slash("/handoff") is True
    assert b._relay_host is not None
    return b._relay_host.root


def _outbox(b) -> list:
    return [(e["kind"], e["payload"]) for e in b._session_db.claim_relay_outbox(limit=50)]


# ── /handoff, /pickup, exit ──────────────────────────────────────────────


def test_handoff_needs_a_single_user_instance(backend, son_of_anton_home) -> None:
    b, rec = backend
    _signal_chat(b)
    b.run_slash("/handoff")
    assert "needs gateway.single_user" in rec.text()
    assert b._relay_host is None and b._session_db.list_relay_bindings() == []


def test_handoff_needs_a_chat_to_relay_to(backend, son_of_anton_home) -> None:
    b, rec = backend
    _single_user(son_of_anton_home)
    b.run_slash("/handoff")
    assert "send the bot a message on Signal first" in rec.text()
    assert b._relay_host is None and b._session_db.list_relay_bindings() == []


def test_handoff_makes_this_tui_the_host_and_tells_the_chat(backend, son_of_anton_home) -> None:
    b, rec = backend
    root = _handed_off(b, son_of_anton_home)
    binding = b._session_db.get_relay_binding(root)
    assert (binding["chat_key"], binding["host"], binding["host_pid"]) == (CHAT_KEY, "tui", os.getpid())
    assert binding["live"] is True
    assert "Handed off to your Signal chat" in rec.text()
    ((kind, payload),) = _outbox(b)
    assert kind == "handoff" and payload["busy"] is False and payload["cwd"]


def test_pickup_unbinds_and_tells_the_chat(backend, son_of_anton_home) -> None:
    b, rec = backend
    root = _handed_off(b, son_of_anton_home)
    _outbox(b)
    b.run_slash("/pickup")
    assert b._relay_host is None
    assert b._session_db.get_relay_binding(root) is None
    assert [k for k, _ in _outbox(b)] == ["pickup"]
    assert "Picked up" in rec.text()


def test_leaving_the_tui_clears_the_host_and_keeps_the_binding(backend, son_of_anton_home) -> None:
    b, _rec = backend
    root = _handed_off(b, son_of_anton_home)
    b.finish()
    binding = b._session_db.get_relay_binding(root)
    assert binding["chat_key"] == CHAT_KEY and binding["host"] is None
    # The chat's next send falls back to a headless worker.
    assert b._session_db.relay_start_or_enqueue(root, CHAT_KEY, "next")["action"] == "start"


# ── the inbox, in the real app ───────────────────────────────────────────


class _FakeAgent:
    _supports_active_turn_redirect = True

    def __init__(self) -> None:
        self.redirected: list = []

    def redirect(self, text):
        self.redirected.append(text)
        return True


async def _wait(pilot, pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.05)
        if pred():
            return True
    return False


def test_relayed_messages_run_like_typed_ones(backend, son_of_anton_home, monkeypatch) -> None:
    _textual()
    monkeypatch.setattr(_tui, "RELAY_TICK_S", 0.1)
    b, _rec = backend
    root = _handed_off(b, son_of_anton_home)
    _outbox(b)
    b.detach()
    db = b._session_db
    agent = _FakeAgent()
    b.agent = agent
    turns: list = []
    release = threading.Event()
    seen: dict = {}

    def fake_chat(message, images=None):
        turns.append(message)
        if message == "long task":
            release.wait(10)
        if message == "ask me":
            seen["clarify"] = b._clarify_callback("Which flavour?", ["vanilla", "chocolate"])
        return f"done: {message}"

    def fake_interrupt() -> bool:
        seen["interrupted"] = True
        release.set()
        return True

    b.chat = fake_chat
    b.interrupt_turn = fake_interrupt

    def send(text: str, mode: str = "queue") -> None:
        db.relay_start_or_enqueue(root, CHAT_KEY, text, mode=mode)

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.2)

            # Idle: a relayed message is a turn, shown as from Signal — and a
            # leading slash is the user's words, never a local command.
            send("/quit is what I typed")
            assert await _wait(pilot, lambda: turns == ["/quit is what I typed"])
            assert await _wait(pilot, lambda: app._busy is None)
            feed = app.query_one("#feed")
            notes = "".join(s.text for blk in feed._blocks if blk.kind == "note" for s in blk.lines)
            assert "sent from Signal" in notes
            assert app.is_running

            # Busy: queued behind the turn like a typed follow-up; ``now``
            # steers the live turn instead; ``stop`` interrupts it.
            send("long task")
            assert await _wait(pilot, lambda: app._busy == "turn")
            send("follow-up")
            assert await _wait(pilot, lambda: app._queued_items == ["follow-up"])
            assert isinstance(app._queued_items[0], _tui.RelayedText)
            send("steer this", mode="now")
            assert await _wait(pilot, lambda: agent.redirected == ["steer this"])
            db.enqueue_relay_message(root, "", mode="stop")
            assert await _wait(pilot, lambda: seen.get("interrupted") is True)
            assert await _wait(pilot, lambda: turns[-1] == "follow-up" and app._busy is None)

            # An open question takes the next relayed message as its answer,
            # and the modal it opened goes away.
            send("ask me")
            assert await _wait(pilot, lambda: isinstance(app.screen, _tui.ChoiceModal))
            send("chocolate, please")
            assert await _wait(pilot, lambda: seen.get("clarify") == "chocolate, please")
            assert await _wait(pilot, lambda: not isinstance(app.screen, _tui.ChoiceModal))
            assert await _wait(pilot, lambda: app._busy is None)
            assert "chocolate, please" not in turns, "an answer is not a new turn"

    asyncio.run(run())

    events = _outbox(b)
    replies = [p["reply"] for k, p in events if k == "turn_end"]
    assert replies == [
        "done: /quit is what I typed", "done: long task", "done: follow-up", "done: ask me",
    ]
    (question,) = [p for k, p in events if k == "question"]
    assert question == {"question": "Which flavour?", "choices": ["vanilla", "chocolate"]}


def test_quitting_gives_unrun_relayed_messages_back(backend, son_of_anton_home, monkeypatch) -> None:
    _textual()
    monkeypatch.setattr(_tui, "RELAY_TICK_S", 0.1)
    b, _rec = backend
    root = _handed_off(b, son_of_anton_home)
    b.detach()
    db = b._session_db
    release = threading.Event()

    def fake_chat(message, images=None):
        release.wait(10)
        return "done"

    def fake_interrupt() -> bool:
        release.set()
        return True

    b.chat = fake_chat
    b.interrupt_turn = fake_interrupt

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.2)
            db.relay_start_or_enqueue(root, CHAT_KEY, "long task")
            assert await _wait(pilot, lambda: app._busy == "turn")
            db.relay_start_or_enqueue(root, CHAT_KEY, "never ran here")
            assert await _wait(pilot, lambda: app._queued_items == ["never ran here"])
            app.exit()

    asyncio.run(run())
    b.finish()
    assert [m["text"] for m in db.list_relay_inbox(root)] == ["never ran here"]
    # Still waiting, so the host stays until the gateway sees this process gone
    # and runs it headless (relay_finish_run on our pid).
    assert db.get_relay_binding(root)["host"] == "tui"
    after = db.relay_finish_run(root, os.getpid())
    assert [m["text"] for m in after["messages"]] == ["never ran here"]


# ── approvals: answerable in either place ────────────────────────────────


def _approve_from_chat(pending_id: str, choice: str) -> str:
    from gateway.slash_commands import GatewaySlashCommandsMixin

    return GatewaySlashCommandsMixin._decide_durable_approval(CHAT_KEY, [pending_id, choice], deny=False)


def test_an_approval_answered_from_the_chat_takes_the_modal_down(backend, son_of_anton_home) -> None:
    _textual()
    from tools import write_approval as wa

    b, _rec = backend
    _handed_off(b, son_of_anton_home)
    b.detach()
    seen: dict = {}

    def fake_chat(message, images=None):
        seen["approval"] = b._approval_callback("rm -rf /tmp/x", "Recursive delete")
        return "ok"

    b.chat = fake_chat

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.2)
            prompt = app.query_one("#input")
            prompt.text = "clean up"
            await pilot.press("enter")
            assert await _wait(pilot, lambda: isinstance(app.screen, _tui.ChoiceModal))
            assert await _wait(pilot, lambda: len(wa.list_pending(wa.EXEC)) == 1)
            (record,) = wa.list_pending(wa.EXEC)
            assert record["chat_key"] == CHAT_KEY
            assert "Approved for this session" in _approve_from_chat(record["id"], "session")
            assert await _wait(pilot, lambda: seen.get("approval") == "session")
            assert await _wait(pilot, lambda: not isinstance(app.screen, _tui.ChoiceModal))
            assert wa.list_pending(wa.EXEC) == []

    asyncio.run(run())


def test_an_approval_answered_in_the_tui_withdraws_the_chats_copy(backend, son_of_anton_home) -> None:
    from tools import write_approval as wa

    b, _rec = backend
    result: dict = {}

    def ask() -> None:
        result["answer"] = b._approval_callback("rm -rf /tmp/x", "Recursive delete")

    def answer_in_tui() -> None:
        deadline = time.monotonic() + 10
        while not b._approval_state and time.monotonic() < deadline:
            time.sleep(0.02)
        b._approval_state["response_queue"].put("once")

    # Not handed off: exactly as before — the modal only, nothing staged.
    threading.Thread(target=answer_in_tui, daemon=True).start()
    ask()
    assert result["answer"] == "once" and wa.list_pending(wa.EXEC) == []

    _handed_off(b, son_of_anton_home)
    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while not wa.list_pending(wa.EXEC) and time.monotonic() < deadline:
        time.sleep(0.05)
    (record,) = wa.list_pending(wa.EXEC)
    answer_in_tui()
    worker.join(10)
    assert result["answer"] == "once"
    assert wa.list_pending(wa.EXEC) == []
    # A late answer from the chat addresses nothing any more.
    assert _approve_from_chat(record["id"], "always") is None
