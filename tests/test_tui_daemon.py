"""The Textual TUI as a soa-d client (daemon/phases/02b-tui-client.md).

Everything here runs for real: the Textual app, the daemon client
(``son_of_anton_tui.daemon``) and its backend (``son_of_anton_tui.backend
.DaemonBackend``) against the real ``soa-d`` binary, which talks to a scripted
fake OpenAI-compatible server defined below. The ``son-of-anton`` entry point
is exercised as a subprocess: ``-z`` and ``-q`` go to soa-d, ``chat -Q``
(the Python gateway's relay worker) stays on the Python agent.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from son_of_anton_tui import daemon as _daemon
from son_of_anton_tui import tui as _tui
from son_of_anton_tui.backend import DaemonBackend

REPO = Path(__file__).resolve().parents[1]
INTERRUPTED = "[response interrupted]"


def _textual() -> None:
    if not _tui.is_available():
        pytest.skip("textual not installed")


def _soa_d() -> str:
    path = os.environ.get("SOA_D_BIN") or shutil.which("soa-d")
    if not path:
        pytest.fail("soa-d is not on PATH: the dev shell provides it (daemon/phases/02b-tui-client.md, Nix)")
    return path


# ---------------------------------------------------------------------------
# A scripted fake model server
# ---------------------------------------------------------------------------

class FakeLLM:
    """Each chat request takes the next scripted turn: ``content``,
    ``tool_calls`` ([{id, name, arguments}]) or ``hang`` (answer nothing until
    released). Unscripted requests get a short answer. Bodies are recorded."""

    def __init__(self, turns, models=()):
        self.turns = list(turns)
        self.models = list(models)
        self.bodies: list[dict] = []
        self.lock = threading.Lock()
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if not self.path.rstrip("/").endswith("/models"):
                    self.send_error(404)
                    return
                body = json.dumps({"object": "list", "data": [{"id": m, "object": "model"} for m in outer.models]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                with outer.lock:
                    outer.bodies.append(body)
                    turn = outer.turns.pop(0) if outer.turns else {"content": "(unscripted)"}
                if turn.get("hang"):
                    outer.release.wait(30)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if turn.get("served"):  # maitred naming the model that answered
                    self.send_header("X-Maitred-Model", turn["served"])
                self.end_headers()
                model = body.get("model", "")

                def send(delta, finish=None):
                    chunk = {"id": "c", "object": "chat.completion.chunk", "model": model,
                             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                    self.wfile.flush()

                try:
                    send({"role": "assistant"})
                    content = turn.get("content", "")
                    for i in range(0, len(content), 4):
                        send({"content": content[i:i + 4]})
                    calls = turn.get("tool_calls", [])
                    for i, c in enumerate(calls):
                        send({"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                                              "function": {"name": c["name"], "arguments": c["arguments"]}}]})
                    send({}, "tool_calls" if calls else "stop")
                    usage = {"id": "c", "object": "chat.completion.chunk", "model": model, "choices": [],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
                    self.wfile.write(b"data: " + json.dumps(usage).encode() + b"\n\ndata: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    def last_user(self, i):
        users = [m for m in self.bodies[i]["messages"] if m.get("role") == "user"]
        return users[-1]["content"] if users else None


def terminal(cid, command):
    return {"id": cid, "name": "terminal", "arguments": json.dumps({"command": command})}


# ---------------------------------------------------------------------------
# A temp home with soa-d config pointing at the fake
# ---------------------------------------------------------------------------

def _stop_daemon(home: str) -> None:
    try:
        c = _daemon.connect(home, autostart=False, timeout=2)
    except Exception:
        return
    pid = c.hello["pid"]
    c.close()
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and os.path.exists(_daemon.socket_path(home)):
        time.sleep(0.05)


@pytest.fixture
def daemon_home(monkeypatch):
    """make(turns, models=()) -> (home, fake): a fresh short home (a unix
    socket path must fit in 107 bytes) with config.toml and .env for soa-d."""
    made = []

    def make(turns, models=(), extra=""):
        fake = FakeLLM(turns, models)
        home = tempfile.mkdtemp(prefix="soad", dir="/tmp" if os.path.isdir("/tmp") else None)
        Path(home, "config.toml").write_text(
            '[model]\ndefault = "test-model"\nprovider = "custom"\n\n'
            f'[custom_providers.custom]\nbase_url = "{fake.url}"\nkey_env = "SOA_D_TEST_KEY"\n\n'
            '[custom_providers.custom.models."test-model"]\nreasoning_efforts = ["low", "high"]\n\n'
            '[custom_providers.custom.models."other-model"]\n\n' + extra,
            encoding="utf-8",
        )
        Path(home, ".env").write_text("SOA_D_TEST_KEY=s3cret\n", encoding="utf-8")
        os.chmod(Path(home, ".env"), 0o600)
        monkeypatch.setenv("SON_OF_ANTON_HOME", home)
        monkeypatch.setenv("SOA_D_TEST_KEY", "")
        made.append((home, fake))
        return home, fake

    _soa_d()
    yield make
    for home, fake in made:
        _stop_daemon(home)
        fake.close()
        shutil.rmtree(home, ignore_errors=True)


def start_backend(home, cwd, **kw) -> DaemonBackend:
    b = DaemonBackend(home=home, cwd=str(cwd), **kw)
    assert b.start(), "DaemonBackend.start() failed"
    return b


def viewer_state(home, sid) -> dict:
    """The session as a separate viewer sees it: {session, messages, queue, ...}."""
    c = _daemon.connect(home)
    try:
        return c.call("session.attach", {"session": sid, "as": "viewer"})
    finally:
        c.close()


async def wait_for(pilot, pred, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.1)
        if pred():
            return True
    return False


def feed_text(app) -> str:
    out = []
    for b in app.query_one("#feed")._blocks:
        for attr in ("text", "source"):
            v = getattr(b, attr, None)
            if isinstance(v, str):
                out.append(v)
        out.extend(s.text for s in getattr(b, "lines", []))
    return "\n".join(out)


def kinds(app) -> list[str]:
    return [b.kind for b in app.query_one("#feed")._blocks]


async def type_line(app, pilot, text) -> None:
    prompt = app.query_one("#input")
    prompt.focus()
    prompt.text = text
    await pilot.press("enter")


def run_app(backend, body) -> None:
    async def run():
        app = _tui.SonOfAntonTUIApp(backend=backend)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause(0.3)
            await body(app, pilot)

    try:
        asyncio.run(run())
    finally:
        backend.finish()


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

def test_connect_starts_the_daemon_once(daemon_home) -> None:
    home, _ = daemon_home([])
    with pytest.raises(_daemon.DaemonError):
        _daemon.connect(home, autostart=False, timeout=1)
    a = _daemon.connect(home)
    assert a.hello["protocol"] == 1
    assert Path(home, "logs", "soa-d.log").exists(), "the autostarted daemon logs to <home>/logs/soa-d.log"
    b = _daemon.connect(home)
    assert b.hello["pid"] == a.hello["pid"], "a running daemon is reused, never started twice"
    with pytest.raises(_daemon.DaemonError) as err:
        a.call("session.attach", {"session": "20260101_000000_000000", "as": "driver"})
    assert err.value.code == -32011
    a.close()
    b.close()


# ---------------------------------------------------------------------------
# The app on the daemon backend
# ---------------------------------------------------------------------------

def test_a_message_streams_into_the_feed(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([{"content": "Hello **there**"}])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "hi")
        assert await wait_for(pilot, lambda: "Hello **there**" in app._transcript), app._transcript
        assert await wait_for(pilot, lambda: app._busy is None), "the app is idle once the turn ends"
        assert "user" in kinds(app) and "markdown" in kinds(app)
        assert "hi" in feed_text(app)
        assert fake.last_user(0) == "hi"

    run_app(b, body)


def test_tool_rows_and_the_approval_modal(daemon_home, tmp_path) -> None:
    _textual()
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    f.chmod(0o644)
    home, fake = daemon_home([{"tool_calls": [terminal("c1", "chmod 777 f.txt")]}, {"content": "done"}])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "go")
        assert await wait_for(pilot, lambda: isinstance(app.screen, _tui.ChoiceModal)), "approval modal never opened"
        assert "chmod 777" in app.screen._title + str(getattr(app.screen, "_detail", ""))
        await pilot.press("1")  # once
        assert await wait_for(pilot, lambda: "done" in app._transcript)
        tools = [blk for blk in app.query_one("#feed")._blocks if blk.kind == "tool"]
        assert tools and tools[0].done
        assert f.stat().st_mode & 0o777 == 0o777

    run_app(b, body)


def test_clarify_opens_a_modal(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([
        {"tool_calls": [{"id": "c1", "name": "clarify", "arguments": json.dumps({"question": "Which flavour?", "choices": ["vanilla", "chocolate"]})}]},
        {"content": "picked"},
    ])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "ask me")
        assert await wait_for(pilot, lambda: isinstance(app.screen, _tui.ChoiceModal)), "clarify modal never opened"
        assert "Which flavour?" in app.screen._title + str(getattr(app.screen, "_detail", ""))
        await pilot.press("down", "enter")
        assert await wait_for(pilot, lambda: "picked" in app._transcript)
        tool_rows = [m for m in fake.bodies[1]["messages"] if m.get("role") == "tool"]
        assert '"user_response":"chocolate"' in tool_rows[-1]["content"].replace(" ", "")

    run_app(b, body)


def test_a_message_sent_while_busy_queues_in_the_daemon(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([
        {"tool_calls": [terminal("c1", "sleep 1; echo slow")]},
        {"content": "first done"},
        {"content": "second done"},
    ])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "first")
        assert await wait_for(pilot, lambda: app._busy == "turn")
        await type_line(app, pilot, "second")
        assert await wait_for(pilot, lambda: app._queued_items == ["second"]), app._queued_items
        assert [q["text"] for q in viewer_state(home, b.session_id)["queue"]] == ["second"], \
            "a message typed mid-turn is queued in the daemon, not only in the app"
        assert await wait_for(pilot, lambda: "second done" in app._transcript, timeout=15)
        assert app._queued_items == []
        assert fake.last_user(2) == "second"

    run_app(b, body)


def test_ctrl_c_interrupts_the_turn(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([{"hang": True}, {"content": "after"}])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "slow")
        assert await wait_for(pilot, lambda: len(fake.bodies) == 1 and app._busy == "turn")
        await pilot.press("ctrl+c")
        assert await wait_for(pilot, lambda: app._busy is None), "ctrl+c interrupts the running turn"
        msgs = viewer_state(home, b.session_id)["messages"]
        assert [m["content"] for m in msgs] == ["slow", INTERRUPTED]
        await type_line(app, pilot, "next")
        assert await wait_for(pilot, lambda: "after" in app._transcript)

    run_app(b, body)


def test_resume_renders_the_history(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([{"content": "noted, friend"}])
    c = _daemon.connect(home)
    sid = c.call("session.create", {"cwd": str(tmp_path)})["session"]["id"]
    c.call("session.attach", {"session": sid, "as": "driver"})
    c.call("turn.send", {"session": sid, "text": "remember me"})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and viewer_state(home, sid)["session"]["status"] != "idle":
        time.sleep(0.05)
    c.close()
    b = start_backend(home, tmp_path, resume=sid)
    assert b.session_id == sid

    async def body(app, pilot):
        assert await wait_for(pilot, lambda: "remember me" in feed_text(app) and "noted, friend" in feed_text(app)), feed_text(app)

    run_app(b, body)


def test_continue_resumes_the_newest_session(daemon_home, tmp_path) -> None:
    home, _ = daemon_home([])
    c = _daemon.connect(home)
    c.call("session.create", {"cwd": str(tmp_path)})
    newest = c.call("session.create", {"cwd": str(tmp_path)})["session"]["id"]
    c.close()
    b = start_backend(home, tmp_path, continue_last=True)
    try:
        assert b.session_id == newest
    finally:
        b.finish()


def test_continue_never_crosses_into_another_workspace(daemon_home, tmp_path) -> None:
    """-c continues this workspace's newest session (the git root, or the
    directory itself); --resume latest takes the newest anywhere."""
    home, _ = daemon_home([])
    repo, elsewhere, empty = tmp_path / "repo", tmp_path / "elsewhere", tmp_path / "empty"
    for d in (repo / ".git", repo / "sub", elsewhere, empty):
        d.mkdir(parents=True)
    c = _daemon.connect(home)
    in_repo = c.call("session.create", {"cwd": str(repo)})["session"]["id"]
    newest = c.call("session.create", {"cwd": str(elsewhere)})["session"]["id"]
    c.close()
    for cwd, kw, want in ((repo / "sub", {"continue_last": True}, in_repo),
                          (repo, {"resume": "latest"}, newest)):
        b = start_backend(home, cwd, **kw)
        try:
            assert b.session_id == want, (cwd, kw)
        finally:
            b.finish()
    b = start_backend(home, empty, continue_last=True)
    try:
        assert b.session_id not in (in_repo, newest), "nothing to continue here: a new session"
    finally:
        b.finish()


def test_model_reasoning_and_perm_commands(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([{"content": "a"}], models=["test-model", "live-model"])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "/model other-model")
        assert await wait_for(pilot, lambda: viewer_state(home, b.session_id)["session"]["model"] == "other-model")
        await type_line(app, pilot, "/perm yolo")
        assert await wait_for(pilot, lambda: viewer_state(home, b.session_id)["session"]["mode"] == "yolo")
        assert await wait_for(pilot, lambda: b.permission_mode() == "yolo")
        await type_line(app, pilot, "/model test-model")
        assert await wait_for(pilot, lambda: viewer_state(home, b.session_id)["session"]["model"] == "test-model")
        await type_line(app, pilot, "/reasoning low")
        assert await wait_for(pilot, lambda: viewer_state(home, b.session_id)["session"]["reasoning_effort"] == "low")
        await type_line(app, pilot, "hi")
        assert await wait_for(pilot, lambda: "a" in app._transcript and app._busy is None)
        assert fake.bodies[0]["model"] == "test-model" and fake.bodies[0].get("reasoning_effort") == "low"
        snap = b.status_snapshot()
        assert snap["model_short"] and snap["session_api_calls"] == 1

    run_app(b, body)


def test_shift_tab_cycles_the_mode_in_the_daemon(daemon_home, tmp_path) -> None:
    _textual()
    home, _ = daemon_home([])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        before = b.permission_mode()
        await pilot.press("shift+tab")
        assert await wait_for(pilot, lambda: viewer_state(home, b.session_id)["session"]["mode"] != before)
        assert b.permission_mode() == viewer_state(home, b.session_id)["session"]["mode"]

    run_app(b, body)


def test_new_starts_a_fresh_session(daemon_home, tmp_path) -> None:
    _textual()
    home, _ = daemon_home([{"content": "old answer"}])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        first = b.session_id
        await type_line(app, pilot, "old question")
        assert await wait_for(pilot, lambda: "old answer" in app._transcript and app._busy is None)
        await type_line(app, pilot, "/new")
        assert await wait_for(pilot, lambda: b.session_id not in (None, first))
        assert await wait_for(pilot, lambda: "old question" not in feed_text(app)), "the feed starts over"

    run_app(b, body)


def test_commit_runs_the_template(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([{"content": "committed"}])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "/commit just the docs")
        assert await wait_for(pilot, lambda: "committed" in app._transcript)
        text = fake.last_user(0)
        assert "uncommitted" in text and "just the docs" in text

    run_app(b, body)


def test_unavailable_commands_say_so(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "/goal win")
        assert await wait_for(pilot, lambda: "not available" in feed_text(app)), feed_text(app)
        assert fake.bodies == [], "an unavailable command never becomes a turn"

    run_app(b, body)


# ---------------------------------------------------------------------------
# The son-of-anton entry point
# ---------------------------------------------------------------------------

def _cli(home, *args, timeout=120):
    env = dict(os.environ)
    env["SON_OF_ANTON_HOME"] = home
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env["PATH"] = os.path.dirname(_soa_d()) + os.pathsep + env.get("PATH", "")
    return subprocess.run([sys.executable, "-m", "son_of_anton_cli.main", *args],
                          capture_output=True, text=True, env=env, timeout=timeout, cwd=home)


def _soa_d_sessions(home):
    db = Path(home, "soa-d.db")
    if not db.exists():
        return []
    with sqlite3.connect(db) as con:
        return con.execute("SELECT kind, source FROM sessions").fetchall()


def test_z_and_q_go_to_soa_d(daemon_home, tmp_path) -> None:
    home, fake = daemon_home([{"content": "hello from z"}, {"content": "hello from q"}])
    usage = tmp_path / "usage.json"
    r = _cli(home, "-z", "say hi", "-m", "test-model", "--reasoning", "low", "--usage-file", str(usage))
    assert r.returncode == 0 and r.stdout == "hello from z\n", (r.returncode, r.stdout, r.stderr)
    u = json.loads(usage.read_text(encoding="utf-8"))
    assert u["model"] == "test-model" and u["completed"] is True
    assert fake.bodies[0].get("reasoning_effort") == "low"
    assert ("oneshot", "oneshot") in _soa_d_sessions(home), "-z ran as soa-d oneshot"

    r = _cli(home, "-q", "ask")
    assert r.returncode == 0 and "hello from q" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert any(source == "query" for _, source in _soa_d_sessions(home)), "-q ran as soa-d query"


def test_quiet_chat_stays_on_the_python_agent(daemon_home, tmp_path) -> None:
    home, fake = daemon_home([])
    q = tmp_path / "q.txt"
    q.write_text("python path please", encoding="utf-8")
    r = _cli(home, "chat", "-Q", "--query-file", str(q))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _soa_d_sessions(home) == [], "the relay worker's chat -Q never reaches soa-d"
    assert any("python path please" in json.dumps(body) for body in fake.bodies), "the Python agent made the request"


# ---------------------------------------------------------------------------
# Phase 3a: compaction and titles in the TUI
# ---------------------------------------------------------------------------

def test_compact_renders_and_survives_a_resume(daemon_home, tmp_path) -> None:
    _textual()
    aux = FakeLLM([{"content": "SUMMARY-BODY"}])
    try:
        home, fake = daemon_home(
            [{"content": "A" * 4000}, {"content": "B"}],
            extra=f'[auxiliary.compaction]\nbase_url = "{aux.url}"\nkey_env = "SOA_D_TEST_KEY"\nmodel = "summarizer"\n',
        )
        b = start_backend(home, tmp_path)

        async def body(app, pilot):
            await type_line(app, pilot, "a" * 4000)
            assert await wait_for(pilot, lambda: len(fake.bodies) == 1 and app._busy is None)
            await type_line(app, pilot, "b")
            assert await wait_for(pilot, lambda: len(fake.bodies) == 2 and app._busy is None)
            await type_line(app, pilot, "/compact")
            assert await wait_for(pilot, lambda: "SUMMARY-BODY" in feed_text(app)), feed_text(app)
            assert b.status_snapshot()["compactions"] == 1

        run_app(b, body)
        sid = b.session_id
        b2 = start_backend(home, tmp_path, resume=sid)

        async def history(app, pilot):
            assert await wait_for(pilot, lambda: "SUMMARY-BODY" in feed_text(app)), "the compaction shows in the history"

        run_app(b2, history)
    finally:
        aux.close()


def test_the_title_and_context_use_reach_the_status(daemon_home, tmp_path) -> None:
    _textual()
    titles = FakeLLM([{"content": '{"title": "Fix login button on mobile"}'}])
    try:
        home, fake = daemon_home(
            [{"content": "sure"}],
            extra=(f'[auxiliary.title_generation]\nbase_url = "{titles.url}"\nkey_env = "SOA_D_TEST_KEY"\n'
                   'model = "little-titles"\n\n[custom_providers.custom.models."ctx-model"]\ncontext_length = 1000\n'),
        )
        b = start_backend(home, tmp_path, model="ctx-model")

        async def body(app, pilot):
            await type_line(app, pilot, "the login button is broken on mobile")
            assert await wait_for(pilot, lambda: b.status_snapshot()["session_title"] == "Fix login button on mobile")
            assert await wait_for(pilot, lambda: b.status_snapshot()["context_tokens"] == 12)
            snap = b.status_snapshot()
            assert snap["context_length"] == 1000 and snap["context_percent"] == 1, snap

        run_app(b, body)
    finally:
        titles.close()


def test_the_status_names_the_model_that_served_auto(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([{"content": "hi", "served": "radiance/qwen3.8-27b"}])
    b = start_backend(home, tmp_path, model="auto")

    async def body(app, pilot):
        assert b.status_snapshot()["model_short"] == "auto", "nothing served yet"
        await type_line(app, pilot, "hello")
        assert await wait_for(pilot, lambda: b.status_snapshot()["model_short"] == "auto → qwen3.8-27b"), b.status_snapshot()
        assert b.status_snapshot()["model_name"] == "auto → radiance/qwen3.8-27b"

    run_app(b, body)


# ---------------------------------------------------------------------------
# Phase 3b: a rule change asks in the TUI
# ---------------------------------------------------------------------------

def test_a_rule_change_asks_and_is_saved(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([
        {"tool_calls": [{"id": "c1", "name": "memory", "arguments": json.dumps(
            {"kind": "rule", "action": "add", "text": "Answer in plain English"})}]},
        {"content": "saved it"},
    ])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "remember: plain English")
        assert await wait_for(pilot, lambda: isinstance(app.screen, _tui.ChoiceModal)), "the approval modal never opened"
        title_and_detail = app.screen._title + str(getattr(app.screen, "_detail", ""))
        assert "memory" in title_and_detail.lower() and "Answer in plain English" in title_and_detail, title_and_detail
        await pilot.press("1")  # once
        assert await wait_for(pilot, lambda: "saved it" in app._transcript)
        rules = Path(home, "memories", "rules.toml").read_text(encoding="utf-8")
        assert "Answer in plain English" in rules

    run_app(b, body)


# ---------------------------------------------------------------------------
# Subagents (phase 4b): task rows, the hint, the child view
# (daemon/phases/04b-subagent-view.md)
# ---------------------------------------------------------------------------

def task_call(cid, description, prompt, subagent_type="explore", **extra):
    args = {"description": description, "prompt": prompt, "subagent_type": subagent_type, **extra}
    return {"id": cid, "name": "task", "arguments": json.dumps(args)}


def block_text(b) -> str:
    """A block's text from its source (notes and tool rows render lazily)."""
    out = [v for v in (getattr(b, "text", None), getattr(b, "source", None), getattr(b, "label", None)) if isinstance(v, str)]
    out.extend(t.plain for t in getattr(b, "texts", []))
    out.extend(s.text for s in getattr(b, "lines", []))
    return "\n".join(out)


def view_text(app) -> str:
    """Everything in the feed on screen (the child's, in a child view). The
    app's own queries search its main screen whatever is pushed on it."""
    return "\n".join(block_text(b) for b in app.screen.query_one("#feed")._blocks)


def task_rows(app) -> list:
    return [b for b in app.screen.query_one("#feed")._blocks if b.kind == "tool" and " Task" in b.label]


def static_text(widget) -> str:
    content = widget.content
    return content.plain if hasattr(content, "plain") else str(content)


def footer(app) -> str:
    return static_text(app.screen.query_one("#subagent-footer"))


def in_child_view(app) -> bool:
    return isinstance(app.screen, _tui.SubagentScreen)


ONE_CHILD = [
    {"tool_calls": [task_call("t1", "Find the thing", "look for the thing")]},
    {"tool_calls": [terminal("c1", "echo hi")]},
    {"content": "found it"},
    {"content": "all done"},
]


def test_task_labels_and_durations_are_opencodes() -> None:
    from son_of_anton_tui.backend import subagent_duration, tool_summary

    args = {"description": "Find it", "prompt": "p", "subagent_type": "explore"}
    assert tool_summary("task", json.dumps(args)) == "Explore Task — Find it"
    assert tool_summary("task", json.dumps({**args, "background": True})) == "Explore Task (background) — Find it"
    assert tool_summary("task", json.dumps({"description": "x", "prompt": "p"})) == "General Task — x"
    assert tool_summary("task", json.dumps({**args, "subagent_type": "code-review"})) == "Code-Review Task — Find it"
    assert [subagent_duration(ms) for ms in (450, 12345, 125000, 3720000, 90000000)] == \
        ["450ms", "12.3s", "2m 5s", "1h 2m", "1d 1h"]


def test_a_task_row_follows_its_child_and_the_hint_follows_it(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home(ONE_CHILD)
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "find it")
        assert await wait_for(pilot, lambda: "all done" in app._transcript)
        rows = task_rows(app)
        assert len(rows) == 1 and rows[0].label == "Explore Task — Find the thing", [b.label for b in rows]
        assert await wait_for(pilot, lambda: re.fullmatch(r"↳ 1 toolcall · \d+(ms|\.\ds)", rows[0].detail or "")), rows[0].detail
        blocks = app.query_one("#feed")._blocks
        after = [block_text(x) for x in blocks[blocks.index(rows[0]) + 1:]]
        assert any("ctrl+x down view subagents" in t for t in after), "the hint follows the task row"
        assert not any(x.kind == "tool" and "echo hi" in x.label for x in blocks), "the child's own tools stay in the child"

    run_app(b, body)


def test_ctrl_x_down_opens_the_child_and_up_comes_back(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home(ONE_CHILD)
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "find it")
        assert await wait_for(pilot, lambda: "all done" in app._transcript)
        main = app.screen
        await pilot.press("ctrl+x", "down")
        assert await wait_for(pilot, lambda: in_child_view(app)), "ctrl+x down opens the child"
        assert await wait_for(pilot, lambda: "found it" in view_text(app)), view_text(app)
        text = view_text(app)
        assert "look for the thing" in text and "echo hi" in text, "the child's transcript: its prompt, its tools, its answer"
        assert "all done" not in text
        assert not app.screen.query("#input"), "a child view has no prompt"
        assert b.view_id is not None and b.view_id != b.session_id
        assert await wait_for(pilot, lambda: re.search(r"\b12\b", footer(app))), footer(app)
        f = footer(app)
        assert "Explore" in f and "(1 of 1)" in f and "Parent" in f and "Prev" in f and "Next" in f, f
        await pilot.press("up")
        assert await wait_for(pilot, lambda: app.screen is main), "up returns to the parent"
        assert b.view_id is None
        assert "all done" in view_text(app)

    run_app(b, body)


def test_left_and_right_step_through_siblings(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([
        {"tool_calls": [task_call("t1", "Job A", "do job alpha"), task_call("t2", "Job B", "do job beta")]},
        {"content": "finished"},
        {"content": "finished"},
        {"content": "both done"},
    ])
    b = start_backend(home, tmp_path)

    def prompt_shown(app) -> str:
        text = view_text(app)
        return "alpha" if "do job alpha" in text else "beta" if "do job beta" in text else ""

    async def body(app, pilot):
        await type_line(app, pilot, "do both")
        assert await wait_for(pilot, lambda: "both done" in app._transcript)
        await pilot.press("ctrl+x", "down")
        assert await wait_for(pilot, lambda: in_child_view(app) and "(2 of 2)" in footer(app)), "the newest child first"
        assert await wait_for(pilot, lambda: prompt_shown(app))
        newest = prompt_shown(app)
        await pilot.press("right")
        assert await wait_for(pilot, lambda: "(1 of 2)" in footer(app) and prompt_shown(app) not in ("", newest)), "right wraps to the first"
        await pilot.press("right")
        assert await wait_for(pilot, lambda: "(2 of 2)" in footer(app) and prompt_shown(app) == newest)
        await pilot.press("left")
        assert await wait_for(pilot, lambda: "(1 of 2)" in footer(app))
        await pilot.press("up")
        assert await wait_for(pilot, lambda: not in_child_view(app))

    run_app(b, body)


def test_a_running_child_streams_live_into_its_view(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home([
        {"tool_calls": [task_call("t1", "Wait a bit", "sleep then answer")]},
        {"tool_calls": [terminal("c1", "sleep 2; echo slept")]},
        {"content": "woke up"},
        {"content": "all done"},
    ])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "go")
        assert await wait_for(pilot, lambda: any("sleep 2" in (r.detail or "") for r in task_rows(app))), \
            [r.detail for r in task_rows(app)]
        assert "view subagents" in static_text(app.query_one("#status-right")), "the status row names the shortcut while a child runs"
        main = app.screen
        await pilot.press("ctrl+x", "down")
        assert await wait_for(pilot, lambda: in_child_view(app))
        assert await wait_for(pilot, lambda: "sleep then answer" in view_text(app))
        assert await wait_for(pilot, lambda: "woke up" in view_text(app), timeout=15), "the child's turn streams into its view"
        await pilot.press("up")
        assert await wait_for(pilot, lambda: app.screen is main)
        assert await wait_for(pilot, lambda: "all done" in view_text(app)), "the parent's feed kept up underneath"
        assert "view subagents" not in static_text(app.query_one("#status-right"))

    run_app(b, body)


def test_a_childs_approval_names_the_subagent(daemon_home, tmp_path) -> None:
    _textual()
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    f.chmod(0o644)
    home, fake = daemon_home([
        {"tool_calls": [task_call("t1", "Open it up", "make f.txt world-writable", "general")]},
        {"tool_calls": [terminal("c1", "chmod 777 f.txt")]},
        {"content": "changed"},
        {"content": "all done"},
    ])
    b = start_backend(home, tmp_path)

    async def body(app, pilot):
        await type_line(app, pilot, "go")
        assert await wait_for(pilot, lambda: isinstance(app.screen, _tui.ChoiceModal)), "the approval modal never opened"
        title = app.screen._title + str(getattr(app.screen, "_detail", ""))
        assert "chmod 777" in title and app.screen._title.startswith("General Task — Open it up: "), title
        await pilot.press("1")  # once
        assert await wait_for(pilot, lambda: "all done" in app._transcript)
        assert f.stat().st_mode & 0o777 == 0o777

    run_app(b, body)


def test_a_resumed_session_shows_its_task_rows_and_children(daemon_home, tmp_path) -> None:
    _textual()
    home, fake = daemon_home(ONE_CHILD)
    b = start_backend(home, tmp_path)
    sid = []

    async def first(app, pilot):
        await type_line(app, pilot, "find it")
        assert await wait_for(pilot, lambda: "all done" in app._transcript)
        sid.append(b.session_id)

    run_app(b, first)
    b2 = start_backend(home, tmp_path, resume=sid[0])

    async def again(app, pilot):
        assert await wait_for(pilot, lambda: task_rows(app))
        row = task_rows(app)[0]
        assert row.label == "Explore Task — Find the thing"
        assert await wait_for(pilot, lambda: re.fullmatch(r"↳ 1 toolcall · \d+(ms|\.\ds)", row.detail or "")), row.detail
        assert await wait_for(pilot, lambda: "ctrl+x down view subagents" in view_text(app)), view_text(app)
        await pilot.press("ctrl+x", "down")
        assert await wait_for(pilot, lambda: in_child_view(app) and "found it" in view_text(app))

    run_app(b2, again)
