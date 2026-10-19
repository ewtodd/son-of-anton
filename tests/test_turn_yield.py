"""Yielding a turn and continuing it: the agent-loop half of a relay takeover.

A headless relay worker hands its turn to the terminal that takes its session
back (/pickup) by yielding: it stops at the next step boundary — a running
tool finishes, a model call in flight is cancelled — and persists nothing for
the stop. The terminal then continues the same turn without a new user
message. Driven here with a real ``AIAgent``, real ``SessionDB`` and the real
terminal tool against a fake OpenAI-compatible model served in-process.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from son_of_anton_state import SessionDB


class FakeModel:
    """A scripted chat-completions endpoint that records every request.

    Each step answers one request: ``("tool", name, args)``, ``("text", s)``,
    or ``("stall", partial)`` — streams ``partial`` and then holds the
    response open until ``release`` is set (or the client goes away).
    """

    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.requests: list = []
        self.stalled = threading.Event()
        self.release = threading.Event()
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                self._send_json({"object": "list", "data": [{"id": "fake", "object": "model"}]})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not body.get("tools") or not body.get("stream"):
                    # An auxiliary call (the session title): not the agent's turn.
                    self._send_json({
                        "id": "aux", "object": "chat.completion", "created": 1, "model": "fake",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "Fake Title"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    })
                    return
                model.requests.append(body)
                step = model.steps.pop(0) if model.steps else ("text", "(script ended)")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    model.stream(self.wfile, step)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _send_json(self, payload) -> None:
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def stream(self, out, step) -> None:
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "fake"}

        def chunk(delta, finish=None) -> None:
            out.write(f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})}\n\n".encode())
            out.flush()

        kind = step[0]
        if kind == "tool":
            _, name, args = step
            chunk({"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"call_{len(self.requests)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }]})
            chunk({}, "tool_calls")
        elif kind == "text":
            chunk({"role": "assistant", "content": step[1]})
            chunk({}, "stop")
        else:  # stall
            chunk({"role": "assistant", "content": step[1]})
            self.stalled.set()
            deadline = time.monotonic() + 15
            while not self.release.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
                out.write(b": keep-alive\n\n")
                out.flush()
            chunk({"content": " LATE TAIL"})
            chunk({}, "stop")
        usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        out.write(f"data: {json.dumps({**base, 'choices': [], 'usage': usage})}\n\n".encode())
        out.write(b"data: [DONE]\n\n")
        out.flush()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()


SESSION = "20261015_120000_yield1"


@pytest.fixture
def db(son_of_anton_home):
    instance = SessionDB()
    try:
        yield instance
    finally:
        instance.close()


def _agent(model: FakeModel, db: SessionDB, tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    from run_agent import AIAgent

    return AIAgent(
        base_url=model.base_url, api_key="sk-fake", provider="custom", model="fake",
        api_mode="chat_completions", enabled_toolsets=["terminal"], quiet_mode=True,
        skip_context_files=True, skip_memory=True, session_db=db, session_id=SESSION,
        platform="cli", max_iterations=10,
    )


def _rows(db: SessionDB) -> list:
    return [
        (m["role"], (m.get("content") or "")[:60], bool(m.get("tool_calls")))
        for m in db.get_messages(db.get_compaction_tip(SESSION) or SESSION)
        if m["role"] in ("user", "assistant", "tool")
    ]


def _assert_valid_sequence(messages: list) -> None:
    """Role alternation holds and every tool result answers a call before it."""
    convo = [m for m in messages if m.get("role") in ("user", "assistant", "tool")]
    open_calls: set = set()
    previous = None
    for m in convo:
        role = m["role"]
        if role == "tool":
            assert m.get("tool_call_id") in open_calls, "a tool result answers a call"
            open_calls.discard(m["tool_call_id"])
        else:
            assert not open_calls, f"{role} row while tool calls are unanswered"
            assert role != previous, f"two {role} rows in a row"
        if role == "assistant" and m.get("tool_calls"):
            open_calls = {c["id"] for c in m["tool_calls"]}
        previous = role


def _no_interrupt_rows(db: SessionDB) -> None:
    texts = " ".join(content for _, content, _ in _rows(db))
    assert "Operation interrupted" not in texts
    assert "interrupted" not in texts.lower()


def _run_in_thread(fn):
    box: dict = {}

    def _go() -> None:
        box["result"] = fn()

    thread = threading.Thread(target=_go, daemon=True)
    thread.start()
    return thread, box


@pytest.mark.linux_only
def test_a_running_tool_finishes_and_the_turn_stops_after_it(db, tmp_path, monkeypatch) -> None:
    marker = tmp_path / "done.txt"
    model = FakeModel([
        ("tool", "terminal", {"command": f"sleep 2; echo built > {marker}; echo BUILD-OK"}),
        ("text", "never asked for"),
    ])
    try:
        agent = _agent(model, db, tmp_path, monkeypatch)
        thread, box = _run_in_thread(lambda: agent.run_conversation("build it"))
        deadline = time.monotonic() + 30
        while not agent._executing_tools and time.monotonic() < deadline:
            time.sleep(0.02)
        assert agent._executing_tools, "the tool is running"
        agent.request_yield()
        thread.join(60)
        result = box["result"]
    finally:
        model.close()

    assert result["yielded"] is True and result["interrupted"] is False
    assert result["turn_exit_reason"] == "yielded" and result["final_response"] is None
    assert marker.read_text(encoding="utf-8").strip() == "built", "the command was waited for"
    assert len(model.requests) == 1, "no model call after the yield"
    rows = _rows(db)
    assert [r[0] for r in rows] == ["user", "assistant", "tool"]
    assert "BUILD-OK" in rows[-1][1], "the tool's result is persisted as usual"
    _no_interrupt_rows(db)


def test_a_model_call_in_flight_is_cancelled_and_its_partial_reply_dropped(db, tmp_path, monkeypatch) -> None:
    model = FakeModel([("stall", "PARTIAL WORDS")])
    try:
        agent = _agent(model, db, tmp_path, monkeypatch)
        thread, box = _run_in_thread(lambda: agent.run_conversation("explain it"))
        assert model.stalled.wait(30), "the model started answering"
        started = time.monotonic()
        agent.request_yield()
        thread.join(30)
        elapsed = time.monotonic() - started
        result = box["result"]
    finally:
        model.close()

    assert result["yielded"] is True and result["interrupted"] is False
    assert elapsed < 10, "the call was cancelled, not waited out"
    rows = _rows(db)
    assert [r[0] for r in rows] == ["user"], "the tail is the unanswered user message"
    assert all("PARTIAL" not in r[1] for r in rows), "nothing of the partial reply is kept"
    assert all("PARTIAL" not in str(m.get("content")) for m in result["messages"])
    _no_interrupt_rows(db)


def test_a_yield_before_the_turn_starts_stops_before_any_model_call(db, tmp_path, monkeypatch) -> None:
    model = FakeModel([("text", "never asked for")])
    try:
        agent = _agent(model, db, tmp_path, monkeypatch)
        agent.request_yield()
        result = agent.run_conversation("first message")
    finally:
        model.close()
    assert result["yielded"] is True
    assert model.requests == []
    assert [r[0] for r in _rows(db)] == ["user"], "the message itself is kept"


def test_a_stale_interrupt_does_not_abort_a_fresh_turn(db, tmp_path, monkeypatch) -> None:
    # A previous turn finished while an interrupt was still armed — a second
    # Ctrl+C/Escape landed after the turn cleared its own interrupt. That stale
    # flag used to make the next run_conversation abort immediately: the lease
    # pre-check read it as "the user aborted the wait" (surfacing as "Stopped
    # waiting for another Son of Anton process") and the loop read it as
    # "interrupted_by_user". A fresh turn must start clean.
    model = FakeModel([("text", "fresh reply")])
    try:
        agent = _agent(model, db, tmp_path, monkeypatch)
        agent._interrupt_requested = True
        agent._interrupt_message = "stale interrupt"
        result = agent.run_conversation("fresh message")
    finally:
        model.close()

    assert agent._interrupt_requested is False
    assert result["final_response"] == "fresh reply"
    assert result.get("interrupted") is False
    assert model.requests, "the model was actually called (the turn ran)"


@pytest.mark.linux_only
def test_continuing_runs_the_same_turn_without_a_new_message(db, tmp_path, monkeypatch) -> None:
    model = FakeModel([
        ("tool", "terminal", {"command": "sleep 1; echo STEP-ONE"}),
        ("text", "FINAL: step one done"),
    ])
    try:
        worker = _agent(model, db, tmp_path, monkeypatch)
        thread, box = _run_in_thread(lambda: worker.run_conversation("do the step"))
        deadline = time.monotonic() + 30
        while not worker._executing_tools and time.monotonic() < deadline:
            time.sleep(0.02)
        worker.request_yield()
        thread.join(60)
        assert box["result"]["yielded"] is True

        # Another agent — the terminal — picks it up from the stored transcript.
        history = db.get_messages_as_conversation(SESSION)
        terminal = _agent(model, db, tmp_path, monkeypatch)
        result = terminal.run_conversation(None, conversation_history=history, continue_turn=True)
    finally:
        model.close()

    assert result["completed"] is True and result["final_response"] == "FINAL: step one done"
    first, second = model.requests
    sent = [m for m in second["messages"] if m["role"] != "system"]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool"], "no message was added"
    assert sent[0]["content"] == "do the step"
    assert second["messages"][0] == first["messages"][0], "the same stored system prompt"
    assert [t["function"]["name"] for t in second["tools"]] == [
        t["function"]["name"] for t in first["tools"]
    ], "the same tools"
    rows = _rows(db)
    assert [r[0] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert sum(1 for r in rows if r[0] == "user") == 1
    _assert_valid_sequence(db.get_messages_as_conversation(SESSION))
    _no_interrupt_rows(db)


def test_continuing_answers_an_unanswered_user_message(db, tmp_path, monkeypatch) -> None:
    model = FakeModel([("stall", "PARTIAL"), ("text", "FULL ANSWER")])
    try:
        worker = _agent(model, db, tmp_path, monkeypatch)
        thread, box = _run_in_thread(lambda: worker.run_conversation("a question"))
        assert model.stalled.wait(30)
        worker.request_yield()
        thread.join(30)
        assert box["result"]["yielded"] is True
        history = db.get_messages_as_conversation(SESSION)
        terminal = _agent(model, db, tmp_path, monkeypatch)
        result = terminal.run_conversation(None, conversation_history=history, continue_turn=True)
    finally:
        model.close()
    assert result["final_response"] == "FULL ANSWER"
    assert [m["role"] for m in model.requests[-1]["messages"] if m["role"] != "system"] == ["user"]
    assert [r[0] for r in _rows(db)] == ["user", "assistant"]
    _assert_valid_sequence(db.get_messages_as_conversation(SESSION))


def test_there_is_nothing_to_continue_after_a_reply(db, tmp_path, monkeypatch) -> None:
    model = FakeModel([("text", "DONE")])
    try:
        agent = _agent(model, db, tmp_path, monkeypatch)
        agent.run_conversation("hello")
        before = _rows(db)
        history = db.get_messages_as_conversation(SESSION)
        result = agent.run_conversation(None, conversation_history=history, continue_turn=True)
    finally:
        model.close()
    assert result["completed"] is False and "nothing to continue" in result["nothing_to_continue"]
    assert len(model.requests) == 1, "no model call"
    assert _rows(db) == before, "nothing persisted"


def test_the_continue_tail_rules() -> None:
    from agent.turn_context import continue_turn_tail

    call = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "terminal", "arguments": "{}"}}]}
    user = {"role": "user", "content": "go"}
    tool = {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    meta = {"role": "session_meta", "content": "{}"}
    assert continue_turn_tail([user, call, tool, meta]) == (0, "")
    assert continue_turn_tail([user, {"role": "assistant", "content": "hi"}, user]) == (2, "")
    assert continue_turn_tail([user, {"role": "assistant", "content": "hi"}])[0] is None
    assert continue_turn_tail([user, call])[0] is None, "unanswered tool calls"
    assert continue_turn_tail([])[0] is None


# ── the relay worker: watching for a terminal, handing over ──────────────


def _live_taker():
    import subprocess
    import sys

    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def _running_headless(db: SessionDB) -> None:
    """The binding a gateway leaves for a worker it spawned (this process)."""
    import os

    db.create_session(SESSION, source="cli")
    assert db.relay_start_or_enqueue(SESSION, "chat-a", "relayed")["action"] == "start"
    assert db.set_relay_host_pid(SESSION, os.getpid())


def _wait_until(pred, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.linux_only
def test_a_takeover_releases_an_approval_wait_and_leaves_the_command_unrun(
    db, tmp_path, monkeypatch,
) -> None:
    from son_of_anton_cli.relay_worker import YIELD_APPROVAL_REASON, run_relay_turn
    from tools import approval
    from tools import write_approval as wa

    monkeypatch.setenv("SON_OF_ANTON_DURABLE_APPROVALS", "1")
    monkeypatch.setenv("SON_OF_ANTON_APPROVAL_CHAT_KEY", "chat-a")
    monkeypatch.setattr(approval, "_DURABLE_APPROVAL_POLL_SECONDS", 0.05)
    keep = tmp_path / "keep"
    keep.mkdir()
    model = FakeModel([("tool", "terminal", {"command": f"rm -rf {keep} && echo GONE"})])
    taker = _live_taker()
    try:
        _running_headless(db)
        agent = _agent(model, db, tmp_path, monkeypatch)
        thread, box = _run_in_thread(lambda: run_relay_turn(
            agent, db, lambda: agent.run_conversation("clean up"), poll_s=0.1,
        ))
        assert _wait_until(lambda: wa.list_pending(wa.EXEC)), "the approval was staged for the chat"
        assert db.relay_request_yield(SESSION, taker.pid)["requested"]
        thread.join(30)
        result, handed = box["result"]
    finally:
        model.close()
        taker.kill()
        taker.wait()

    assert handed is True and result["yielded"] is True
    assert keep.exists(), "the command never ran"
    assert wa.list_pending(wa.EXEC) == [], "the chat's copy was withdrawn"
    rows = _rows(db)
    assert [r[0] for r in rows] == ["user", "assistant", "tool"]
    tool_text = db.get_messages(SESSION)[-1]["content"]
    assert "BLOCKED" in tool_text and YIELD_APPROVAL_REASON[:40] in tool_text
    assert len(model.requests) == 1
    assert db.get_relay_binding(SESSION)["yielded_at"], "the handover was committed"


@pytest.mark.linux_only
def test_a_worker_whose_terminal_gave_up_continues_its_own_turn(db, tmp_path, monkeypatch) -> None:
    from son_of_anton_cli.relay_worker import run_relay_turn

    model = FakeModel([
        ("tool", "terminal", {"command": "sleep 2; echo STEP"}),
        ("text", "FINAL from the worker"),
    ])
    taker = _live_taker()
    try:
        _running_headless(db)
        agent = _agent(model, db, tmp_path, monkeypatch)
        thread, box = _run_in_thread(lambda: run_relay_turn(
            agent, db, lambda: agent.run_conversation("do it"), poll_s=0.1,
        ))
        assert _wait_until(lambda: agent._executing_tools)
        assert db.relay_request_yield(SESSION, taker.pid)["requested"]
        assert _wait_until(lambda: agent._yield_pending()), "the worker heard it"
        # Ctrl+C at the terminal while the command still runs.
        assert db.relay_cancel_yield(SESSION, taker.pid) is True
        thread.join(60)
        result, handed = box["result"]
    finally:
        model.close()
        taker.kill()
        taker.wait()

    assert handed is False
    assert result["completed"] is True and result["final_response"] == "FINAL from the worker"
    assert [r[0] for r in _rows(db)] == ["user", "assistant", "tool", "assistant"]
    _assert_valid_sequence(db.get_messages_as_conversation(SESSION))
    _no_interrupt_rows(db)


def test_a_turn_that_ends_while_a_terminal_asks_is_handed_over(db, tmp_path, monkeypatch) -> None:
    from son_of_anton_cli.relay_worker import run_relay_turn

    model = FakeModel([("text", "ALL DONE")])
    taker = _live_taker()
    try:
        _running_headless(db)
        agent = _agent(model, db, tmp_path, monkeypatch)

        def _turn():
            result = agent.run_conversation("finish up")
            db.relay_request_yield(SESSION, taker.pid)  # lands as the turn ends
            return result

        result, handed = run_relay_turn(agent, db, _turn, poll_s=0.1)
    finally:
        model.close()
        taker.kill()
        taker.wait()
    assert handed is True and result["final_response"] == "ALL DONE"


def test_a_compaction_in_progress_is_a_step_the_yield_waits_for(db, tmp_path, monkeypatch) -> None:
    model = FakeModel([
        ("tool", "terminal", {"command": "echo STEP"}),
        ("text", "never asked for"),
    ])
    seen: dict = {}
    compacting = threading.Event()
    try:
        agent = _agent(model, db, tmp_path, monkeypatch)

        def slow_compaction(messages, system_message, approx_tokens=None, task_id=None, **_kw):
            compacting.set()
            time.sleep(1.0)  # the yield lands while this runs
            seen["interrupted"] = agent._interrupt_requested or agent._hard_interrupt_requested.is_set()
            seen["finished"] = True
            return list(messages), agent._cached_system_prompt

        monkeypatch.setattr(agent, "_compact_context", slow_compaction)
        monkeypatch.setattr(agent.context_compactor, "should_compact", lambda *_a, **_k: True)
        thread, box = _run_in_thread(lambda: agent.run_conversation("go"))
        assert compacting.wait(30)
        agent.request_yield()
        thread.join(30)
        result = box["result"]
    finally:
        model.close()
    assert seen == {"interrupted": False, "finished": True}, "compaction ran to its end, unsignalled"
    assert result["yielded"] is True and len(model.requests) == 1
    assert [r[0] for r in _rows(db)] == ["user", "assistant", "tool"]
