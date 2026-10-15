"""The gateway side of a relay run.

A worker is a terminal session, so the process the gateway spawns must not
carry the relay chat's identity or the gateway's own markers — whatever chat
turn spawned it — and it must carry the durable-approval route back to the
chat. A gateway restart must not strand messages queued behind a worker that
died meanwhile.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import types

import pytest

from son_of_anton_state import AsyncSessionDB, SessionDB


def _run_env_through_spawn(tmp_path) -> dict:
    from gateway import relay_workers
    from tools.process_registry import process_registry

    env = relay_workers.worker_env(
        chat_key="agent:main:signal:group:group:x:+1", cwd=str(tmp_path),
        new_session_id="20261015_120000_abc123",
    )
    watcher = {
        "platform": "signal", "chat_id": "group:x", "thread_id": "", "user_id": "+1",
        "user_name": "u", "check_interval": 3, "parent_session_id": "chat-session",
        "relay_session_id": "20261015_120000_abc123",
    }
    proc = relay_workers.spawn_worker("env", str(tmp_path), "chat", env, watcher)
    deadline = time.monotonic() + 15
    while not proc.exited and time.monotonic() < deadline:
        time.sleep(0.05)
    assert proc.exited
    lines = [line for line in process_registry.get(proc.id).output_buffer.splitlines() if "=" in line]
    return dict(line.split("=", 1) for line in lines)


@pytest.mark.linux_only
def test_a_worker_carries_the_approval_route_not_the_chat_identity(tmp_path, monkeypatch) -> None:
    from gateway.session_context import clear_session_vars, set_session_vars

    # Spawned from inside a chat turn of a gateway process.
    monkeypatch.setenv("_SON_OF_ANTON_GATEWAY", "1")
    monkeypatch.setenv("SON_OF_ANTON_EXEC_ASK", "1")
    tokens = set_session_vars(
        platform="signal", source="signal", chat_id="group:x", chat_type="group",
        session_key="agent:main:signal:group:group:x:+1", session_id="chat-session",
    )
    try:
        child = _run_env_through_spawn(tmp_path)
    finally:
        clear_session_vars(tokens)

    for leaked in ("SON_OF_ANTON_SESSION_PLATFORM", "SON_OF_ANTON_SESSION_SOURCE",
                   "SON_OF_ANTON_SESSION_KEY", "SON_OF_ANTON_SESSION_ID",
                   "SON_OF_ANTON_SESSION_CHAT_ID"):
        assert not child.get(leaked), f"{leaked} leaked into the worker"
    assert not child.get("_SON_OF_ANTON_GATEWAY")
    assert not child.get("SON_OF_ANTON_EXEC_ASK")
    assert child["SON_OF_ANTON_DURABLE_APPROVALS"] == "1"
    assert child["SON_OF_ANTON_APPROVAL_CHAT_KEY"] == "agent:main:signal:group:group:x:+1"
    assert child["SON_OF_ANTON_RELAY_RUN"] == "1"
    assert child["TERMINAL_CWD"] == str(tmp_path)


def test_a_restart_runs_what_a_dead_worker_left_queued(son_of_anton_home, monkeypatch) -> None:
    from gateway import relay_workers

    db = SessionDB()
    db.relay_start_or_enqueue("s1", "chat-a", "one")
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    db.set_relay_host_pid("s1", worker.pid)
    assert db.relay_start_or_enqueue("s1", "chat-a", "two")["action"] == "queued"
    worker.kill()  # the worker dies while no gateway is watching it
    worker.wait()

    started = []

    async def _fake_start(runner, root, messages, *, chat_key, cwd_hint=None):
        started.append((root, [m["text"] for m in messages], chat_key))
        return {"started": True}

    monkeypatch.setattr(relay_workers, "start_run", _fake_start)

    async def _go() -> None:
        tasks = []
        runner = types.SimpleNamespace(
            _session_db=AsyncSessionDB(db),
            _spawn_supervised=lambda factory, name, restart=True: tasks.append(
                asyncio.ensure_future(factory())
            ),
        )
        await relay_workers.reconcile_hosts(runner)
        await asyncio.gather(*tasks)

    try:
        asyncio.run(_go())
        assert started == [("s1", ["two"], "chat-a")]
        assert db.list_relay_inbox("s1") == []
    finally:
        db.close()


def test_a_completion_follows_the_chat_past_new(son_of_anton_home) -> None:
    # The user ran /new while the worker was busy: the conversation the
    # worker was started from has ended. The completion must go to the chat's
    # current conversation instead of being dropped with the old one.
    from gateway import relay_workers

    db = SessionDB()
    db.relay_start_or_enqueue("s1", "chat-a", "one")
    db.set_relay_host_pid("s1", 4242)
    entry = types.SimpleNamespace(origin=types.SimpleNamespace(platform=None), session_id="after-new")
    runner = types.SimpleNamespace(
        _session_db=AsyncSessionDB(db),
        session_store=types.SimpleNamespace(_entries={"chat-a": entry}, _ensure_loaded=lambda: None),
    )
    watcher = {
        "relay_session_id": "s1", "relay_host_pid": 4242,
        "session_key": "chat-a", "parent_session_id": "before-new",
    }
    try:
        result = asyncio.run(relay_workers.on_worker_exit(runner, watcher))
        assert result is not None
        assert watcher["parent_session_id"] == "after-new"
    finally:
        db.close()


@pytest.mark.linux_only
def test_a_worker_spawned_without_a_login_shell_carries_no_rc_noise(tmp_path) -> None:
    # spawn_local's default runs a command in the user's interactive login
    # shell, whose rc files can print; a relay worker's output is read back
    # to the user, so it is spawned without one.
    from tools.process_registry import process_registry

    for rc in (".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile"):
        (tmp_path / rc).write_text("echo RC-NOISE\n", encoding="utf-8")
    proc = process_registry.spawn_local(
        "echo worker-output", cwd=str(tmp_path), env_vars={"HOME": str(tmp_path)}, login_shell=False,
    )
    deadline = time.monotonic() + 15
    while not proc.exited and time.monotonic() < deadline:
        time.sleep(0.05)
    output = process_registry.get(proc.id).output_buffer
    assert "worker-output" in output
    assert "RC-NOISE" not in output

