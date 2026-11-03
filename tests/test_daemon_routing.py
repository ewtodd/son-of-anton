"""Where the ``son-of-anton`` entry point sends an invocation.

``-z``, ``-q`` and the interactive launch exec soa-d (``oneshot``, ``query``
and ``tui``); ``chat -Q`` (the Python gateway's relay worker) stays on the
Python agent. The first two run against the real ``soa-d`` binary and a
scripted fake OpenAI-compatible server; the interactive launch is checked
through a stand-in ``SOA_D_BIN`` that records how it was called, since the
TUI needs a terminal (its own tests are in ``daemon/internal/tui``).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _soa_d() -> str:
    path = os.environ.get("SOA_D_BIN") or shutil.which("soa-d")
    if not path:
        pytest.fail("soa-d is not on PATH: the dev shell provides it")
    return path


class FakeLLM:
    """Each chat request takes the next scripted ``content``; unscripted
    requests get a short answer. Bodies are recorded."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.bodies: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps({"object": "list", "data": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                with outer.lock:
                    outer.bodies.append(body)
                    content = outer.turns.pop(0) if outer.turns else "(unscripted)"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                model = body.get("model", "")

                def chunk(payload):
                    self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
                    self.wfile.flush()

                try:
                    for delta, finish in (({"role": "assistant"}, None), ({"content": content}, None), ({}, "stop")):
                        chunk({"id": "c", "object": "chat.completion.chunk", "model": model,
                               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
                    chunk({"id": "c", "object": "chat.completion.chunk", "model": model, "choices": [],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _stop_daemon(home: str) -> None:
    """Stop a daemon a test started: ask its pid with hello, then SIGTERM it."""
    sock = Path(home, "run", "soa-d.sock")
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2)
            s.connect(str(sock))
            s.sendall(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "hello",
                                  "params": {"protocol": 1, "client": "cli"}}).encode() + b"\n")
            pid = json.loads(s.makefile().readline())["result"]["pid"]
    except (OSError, ValueError, KeyError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and sock.exists():
        time.sleep(0.05)


@pytest.fixture
def daemon_home(monkeypatch):
    """make(turns) -> (home, fake): a fresh short home (a unix socket path
    must fit in 107 bytes) with config.toml and .env for soa-d."""
    made = []

    def make(turns):
        fake = FakeLLM(turns)
        home = tempfile.mkdtemp(prefix="soad", dir="/tmp" if os.path.isdir("/tmp") else None)
        Path(home, "config.toml").write_text(
            '[model]\ndefault = "test-model"\nprovider = "custom"\n\n'
            f'[custom_providers.custom]\nbase_url = "{fake.url}"\nkey_env = "SOA_D_TEST_KEY"\n\n'
            '[custom_providers.custom.models."test-model"]\nreasoning_efforts = ["low", "high"]\n',
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


def _cli(home, *args, env_extra=None, timeout=120):
    env = dict(os.environ)
    env["SON_OF_ANTON_HOME"] = home
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env["PATH"] = os.path.dirname(_soa_d()) + os.pathsep + env.get("PATH", "")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "son_of_anton_cli.main", *args],
                          capture_output=True, text=True, env=env, timeout=timeout, cwd=home)


def _soa_d_sessions(home):
    db = Path(home, "soa-d.db")
    if not db.exists():
        return []
    with sqlite3.connect(db) as con:
        return con.execute("SELECT kind, source FROM sessions").fetchall()


def test_z_and_q_go_to_soa_d(daemon_home, tmp_path) -> None:
    home, fake = daemon_home(["hello from z", "hello from q"])
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


def test_the_interactive_launch_execs_soa_d_tui(daemon_home, tmp_path) -> None:
    home, _ = daemon_home([])
    record = tmp_path / "argv.json"
    stub = tmp_path / "soa-d"
    stub.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"json.dump(sys.argv[1:], open({str(record)!r}, 'w', encoding='utf-8'))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    stand_in = {"SOA_D_BIN": str(stub)}

    def launched(*args):
        if record.exists():
            record.unlink()
        r = _cli(home, *args, env_extra=stand_in)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        return json.loads(record.read_text(encoding="utf-8"))

    assert launched("--in", str(work)) == ["tui", "--home", home, "--in", str(work)]
    assert launched("-c", "-m", "test-model", "--reasoning", "low", "--yolo", "--in", str(work)) == [
        "tui", "--home", home, "--model", "test-model", "--reasoning", "low", "--yolo", "--in", str(work), "-c"]
    assert launched("--resume", "latest", "--in", str(work)) == [
        "tui", "--home", home, "--in", str(work), "--resume", "latest"]
    assert launched("--resume", "20261103_010203_abcdef", "--in", str(work))[-2:] == [
        "--resume", "20261103_010203_abcdef"]

    record.unlink()
    r = _cli(home, "-c", "my session", env_extra=stand_in)  # resuming by name has no meaning on soa-d
    assert r.returncode == 2 and not record.exists(), (r.returncode, r.stderr)
