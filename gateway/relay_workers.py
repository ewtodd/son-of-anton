"""A relay chat drives terminal sessions through headless worker runs.

On a ``gateway.single_user`` instance the messaging chat is a relay: it talks
to the user and hands project work to a *worker* — an ordinary terminal
session (``source=cli``) run as ``son-of-anton chat -Q`` in that session's
own directory, with its own stored prompt and tools. The chat agent reaches
this module through the ``relay`` tool (tools/relay_tool.py); ``/resume`` of
a terminal session binds it here too.

State lives in SessionDB (``relay_bindings`` / ``relay_inbox``), so a worker
is visible to every process and survives a gateway restart:

* ``send`` binds the session to the chat and either starts a run or, while
  one is in flight, queues the message. A run is a background process with
  the ordinary completion watcher; when it exits, the next run starts with
  every queued message joined in order, or the session goes idle. Both
  decisions are single SessionDB transactions, so nothing is lost or run
  twice.
* The completion reaches the chat as the usual internal process-completion
  event — between turns, through the adapter — carrying the worker's final
  reply for the chat agent to summarize.
* Dangerous commands in the worker become durable approvals routed to this
  chat (``/approve <id>``), and a clarify question is answered at once with
  "put it in your reply" (son_of_anton_cli/relay_worker.py), so a headless
  run never waits on a terminal nobody is at.

``/handoff`` in the TUI (son_of_anton_cli/relay_host.py) makes the open TUI
the session's host instead: sends queue to it and it runs them, and what the
chat should hear — the handoff, each turn's reply, a question, the pickup —
arrives through ``relay_outbox``, which ``relay_event_loop`` here delivers
into the chat as internal events. A TUI host that goes away (exit, crash, a
heartbeat gone stale) falls back to headless workers.

``/pickup`` at a terminal while a headless worker is mid-turn takes that turn
back: the worker yields and exits, and its exit here starts nothing and
reports nothing — the queue waits for that terminal, which continues the turn
and tells the chat through the outbox (``relay_finish_run`` → ``yielded``).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
import shlex
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from son_of_anton_cli.relay_worker import NEW_SESSION_ID_ENV, RELAY_RUN_ENV
from son_of_anton_constants import get_son_of_anton_home

logger = logging.getLogger(__name__)

# How often the completion watcher polls a worker.
WATCH_INTERVAL_S = 3
# How often the gateway delivers TUI-host events, and every how many of those
# passes it looks for TUI hosts that went away.
EVENT_POLL_S = 2.0
TUI_SWEEP_EVERY = 5
# Characters of a TUI turn's final reply the chat receives.
TURN_REPLY_CHARS = 3000
# Transcript rows ``status`` shows.
STATUS_ROWS = 8
# Sessions ``list`` returns.
LIST_LIMIT = 15

# Gateway markers a worker must not inherit: with them its CLI thinks it is
# the gateway (skips exporting its own cwd, routes approvals to the gateway
# loop). Blanked rather than removed — spawn_local can only add variables.
_GATEWAY_ONLY_ENV = (
    "_SON_OF_ANTON_GATEWAY",
    "_SON_OF_ANTON_GATEWAY_PROC",
    "SON_OF_ANTON_EXEC_ASK",
    "SON_OF_ANTON_GATEWAY_SESSION",
)


class RelayError(Exception):
    """A relay action that cannot proceed; the message is for the chat agent."""


# ── helpers ──────────────────────────────────────────────────────────────


def _db(runner):
    db = getattr(runner, "_session_db", None)
    if db is None:
        raise RelayError("the session database is unavailable")
    return db


def _chat_route(runner, chat_key: str):
    """(origin, current session id) of the relay chat, from the session store."""
    store = runner.session_store
    try:
        store._ensure_loaded()
    except Exception:
        pass
    entry = store._entries.get(chat_key)
    if entry is None or getattr(entry, "origin", None) is None:
        raise RelayError("this chat has no delivery route yet")
    return entry.origin, str(entry.session_id or "")


def worker_argv(session_id: str, query_file: str, *, resume: bool, cwd: str) -> List[str]:
    """The worker command: the gateway's own build, never whatever is on PATH."""
    own_bin = os.environ.get("SON_OF_ANTON_BIN", "").strip()
    if own_bin and os.path.isfile(own_bin) and os.access(own_bin, os.X_OK):
        argv = [own_bin]
    else:
        argv = [sys.executable, "-m", "son_of_anton_cli.main"]
    argv += ["chat", "-Q", "--query-file", query_file]
    argv += ["--resume", session_id] if resume else ["--in", cwd]
    return argv


def worker_env(*, chat_key: str, cwd: str, new_session_id: Optional[str]) -> Dict[str, str]:
    env = {name: "" for name in _GATEWAY_ONLY_ENV}
    env.update({
        "TERMINAL_CWD": cwd,
        "SON_OF_ANTON_DURABLE_APPROVALS": "1",
        "SON_OF_ANTON_APPROVAL_CHAT_KEY": chat_key,
        RELAY_RUN_ENV: "1",
    })
    if new_session_id:
        env[NEW_SESSION_ID_ENV] = new_session_id
    return env


def _new_session_id() -> str:
    # The CLI's own shape (cli.py), so the session sorts and reads like any other.
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _query_dir() -> Path:
    path = get_son_of_anton_home() / "relay"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_query_file(root: str, text: str) -> str:
    path = _query_dir() / f"{root}-{uuid.uuid4().hex[:8]}.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return str(path)


def _remove_quietly(path: Optional[str]) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _find_process(pid: Optional[int]):
    from tools.process_registry import process_registry

    if not pid:
        return None
    with process_registry._lock:
        sessions = list(process_registry._running.values())
    for proc in sessions:
        if proc.pid == pid:
            return proc
    return None


def _when(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _session_brief(db, session_id: str) -> Dict[str, Any]:
    tip = await db.get_compaction_tip(session_id) or session_id
    row = await db.get_session(tip) or {}
    return {
        "session": session_id,
        "title": row.get("title"),
        "cwd": row.get("cwd"),
    }


async def _resolve_session(db, ref: str) -> Dict[str, Any]:
    """A terminal session by id or title; RelayError when it is anything else."""
    ref = str(ref or "").strip()
    row = await db.get_session(ref)
    if row is None:
        sid = await db.resolve_session_by_title(ref)
        row = await db.get_session(sid) if sid else None
    if row is None:
        raise RelayError(f"no session {ref!r}")
    if row.get("source") != "cli":
        raise RelayError(
            f"session {row['id']} is a {row.get('source')} conversation, not a "
            "terminal session"
        )
    return row


async def _target(runner, chat_key: str, ref: Optional[str]) -> str:
    """The lineage root the action is about: ``ref``, else this chat's binding."""
    db = _db(runner)
    if ref:
        row = await _resolve_session(db, ref)
        return await db.relay_session_root(row["id"])
    bound = await db.list_relay_bindings(chat_key)
    if not bound:
        raise RelayError(
            "no terminal session is bound to this chat; name one (list shows "
            "them) or start one in a directory"
        )
    return bound[0]["session_id"]


def _live_host(binding: Dict[str, Any]) -> Optional[str]:
    """'tui' (handed off from the user's open TUI), 'headless', or None (idle)."""
    return binding.get("host") if binding.get("host") and binding.get("live") else None


async def _pending_approvals(db, chat_key: str, root: str) -> List[Dict[str, Any]]:
    from tools import write_approval as wa

    out = []
    for record in await asyncio.to_thread(wa.list_pending, wa.EXEC):
        if record.get("decision") or record.get("chat_key") != chat_key:
            continue
        agent_sid = str((record.get("payload") or {}).get("agent_session_id") or "")
        if not agent_sid or await db.relay_session_root(agent_sid) != root:
            continue
        out.append({"id": record.get("id"), "summary": record.get("summary")})
    return out


# ── runs ─────────────────────────────────────────────────────────────────


def spawn_worker(command: str, cwd: str, chat_key: str, env: Dict[str, str], watcher: Dict[str, Any]):
    """Spawn the worker process (blocking; call it off the event loop).

    Runs in a fresh, empty ``contextvars.Context``: spawn_local copies the
    calling task's session variables onto the child, and the worker must not
    inherit the relay chat's platform, source, chat or session id — it is a
    terminal session.
    """
    return contextvars.Context().run(_spawn_sync, command, cwd, chat_key, env, watcher)


def _spawn_sync(command: str, cwd: str, chat_key: str, env: Dict[str, str], watcher: Dict[str, Any]):
    from tools.process_registry import process_registry

    proc = process_registry.spawn_local(
        command,
        cwd=cwd,
        task_id=f"relay:{watcher['relay_session_id']}",
        session_key=chat_key,
        env_vars=env,
        # The worker is launched by absolute path and inherits this
        # process's environment; an interactive login shell would only add
        # the user's rc output to the completion the relay reads.
        login_shell=False,
    )
    proc.notify_on_complete = True
    proc.watcher_platform = watcher["platform"]
    proc.watcher_chat_id = watcher["chat_id"]
    proc.watcher_thread_id = watcher["thread_id"]
    proc.watcher_user_id = watcher["user_id"]
    proc.watcher_user_name = watcher["user_name"]
    proc.watcher_interval = watcher["check_interval"]
    proc.parent_session_id = watcher["parent_session_id"]
    process_registry._write_checkpoint()
    return proc


async def start_run(
    runner,
    root: str,
    messages: List[Dict[str, Any]],
    *,
    chat_key: str,
    cwd_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Spawn a worker for ``messages`` (already claimed) on session ``root``.

    The caller holds the host claim. On failure the claim is dropped and the
    messages go back to the inbox, so they run with the next send.
    """
    db = _db(runner)
    query_file = None
    try:
        tip = await db.get_compaction_tip(root) or root
        row = await db.get_session(tip)
        resume = row is not None
        cwd = (row or {}).get("cwd") or cwd_hint
        if not cwd or not os.path.isdir(cwd):
            raise RelayError(f"the session's directory is missing: {cwd or '(none)'}")
        origin, chat_session_id = _chat_route(runner, chat_key)
        query_file = await asyncio.to_thread(
            _write_query_file, root, "\n\n".join(m["text"] for m in messages)
        )
        argv = worker_argv(root, query_file, resume=resume, cwd=cwd)
        env = worker_env(
            chat_key=chat_key, cwd=cwd, new_session_id=None if resume else root,
        )
        watcher = {
            "check_interval": WATCH_INTERVAL_S,
            "session_key": chat_key,
            "platform": origin.platform.value if origin.platform else "",
            "chat_type": str(origin.chat_type or ""),
            "chat_id": str(origin.chat_id or ""),
            "thread_id": str(origin.thread_id or ""),
            "user_id": str(origin.user_id or ""),
            "user_name": str(origin.user_name or ""),
            "message_id": None,
            "notify_on_complete": True,
            "parent_session_id": chat_session_id,
            "relay_session_id": root,
            "relay_cwd": cwd,
            "relay_query_file": query_file,
        }
        command = " ".join(shlex.quote(a) for a in argv)
        proc = await asyncio.to_thread(spawn_worker, command, cwd, chat_key, env, watcher)
    except Exception as exc:
        _remove_quietly(query_file)
        await db.clear_relay_host(root)
        await db.release_relay_inbox([m["id"] for m in messages])
        if isinstance(exc, RelayError):
            raise
        logger.warning("Relay worker for %s failed to start: %s", root, exc, exc_info=True)
        raise RelayError(f"the worker could not start: {exc}") from exc

    watcher["session_id"] = proc.id
    watcher["relay_host_pid"] = proc.pid
    if not await db.set_relay_host_pid(root, proc.pid):
        # Stopped while it was starting: nobody wants this run.
        from tools.process_registry import process_registry

        await asyncio.to_thread(process_registry.kill_process, proc.id, source="relay.stop")
        _remove_quietly(query_file)
        return {"started": False, "reason": "stopped while starting"}
    runner._spawn_supervised(
        lambda w=watcher: runner._run_process_watcher(w),
        f"process_watcher:{proc.id}",
        restart=False,
    )
    logger.info("Relay worker %s (pid %s) started for session %s", proc.id, proc.pid, root)
    return {"started": True, "process": proc.id, "messages": len(messages)}


async def _after_exit(runner, root: str, pid: int, *, cwd_hint: Optional[str]) -> Dict[str, Any]:
    """Run ``root``'s queue after run ``pid`` ended, or mark it idle."""
    db = _db(runner)
    outcome = await db.relay_finish_run(root, pid)
    if outcome["action"] != "start":
        return outcome
    try:
        # The chat it is bound to now — a send from another chat moves it.
        await start_run(
            runner, root, outcome["messages"],
            chat_key=outcome["chat_key"], cwd_hint=cwd_hint,
        )
    except RelayError as exc:
        return {"action": "failed", "error": str(exc), "count": len(outcome["messages"])}
    return {"action": "start", "count": len(outcome["messages"])}


_SESSION_ID_LINE = re.compile(r"\n*session_id: \S+\s*$")


async def on_worker_exit(runner, watcher: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The process watcher saw a relay worker exit: queue first, then report.

    Returns what to tell the chat, or None when this run no longer owns the
    session (a stop already handled it). A run the user took back at the
    terminal (``/pickup``) reports nothing here — its queue waits for the
    terminal, and the chat hears the pickup from it (``relay_outbox``).
    """
    _remove_quietly(watcher.get("relay_query_file"))
    root = watcher["relay_session_id"]
    try:
        outcome = await _after_exit(
            runner, root, int(watcher["relay_host_pid"]), cwd_hint=watcher.get("relay_cwd"),
        )
    except Exception as exc:
        logger.warning("Relay exit handling for %s failed: %s", root, exc, exc_info=True)
        outcome = {"action": "failed", "error": str(exc)}
    if outcome.get("action") == "none":
        # Gone from the chat altogether: the terminal that took it over got
        # there first (or the chat detached it after the run died).
        if await _db(runner).get_relay_binding(root) is None:
            return {"session": root, "action": "picked_up"}
        return None
    # The completion belongs to the chat, not to the conversation that was
    # current when the worker started: after /new that session has ended,
    # and a completion addressed to it would be dropped as a closed thread.
    chat_key = watcher.get("session_key")
    if chat_key:
        try:
            _origin, current = _chat_route(runner, chat_key)
            if current:
                watcher["parent_session_id"] = current
        except RelayError:
            pass
    brief = await _session_brief(_db(runner), root)
    return {**brief, **outcome}


def format_completion(evt: Dict[str, Any], relay: Dict[str, Any]) -> str:
    """The completion event a relay chat receives for a finished worker turn.

    Keeps the ``[IMPORTANT: Background process`` opening of every process
    notification: compaction and titling recognize machine-authored rows by it.
    Empty — nothing is delivered — for a run handed to the terminal: the turn
    goes on there, and the pickup event tells the chat.
    """
    if relay.get("action") in ("yielded", "picked_up"):
        return ""
    exit_code = evt.get("exit_code")
    if evt.get("completion_reason") == "killed":
        how = "was stopped"
    elif exit_code == 0:
        how = "finished its turn"
    else:
        how = "exited with an error"
    title = f" ('{relay['title']}')" if relay.get("title") else ""
    output = _SESSION_ID_LINE.sub("", str(evt.get("output") or "")).strip()
    lines = [
        f"[IMPORTANT: Background process {evt.get('session_id')} — the relay "
        f"worker for terminal session {relay['session']}{title} in "
        f"{relay.get('cwd') or '?'} — {how} (exit code {exit_code}).",
        "Its output; the worker's final reply is at the end:",
        output or "(no output)",
    ]
    action = relay.get("action")
    if action == "start":
        lines.append(
            f"Its {relay.get('count')} queued message(s) went to it as the next "
            "turn; it is running again."
        )
    elif action == "failed":
        lines.append(f"Its queued messages could not start: {relay.get('error')}")
    else:
        lines.append("It is idle now.")
    lines.append("Tell the user the result briefly.]")
    return "\n".join(lines)


async def reconcile_hosts(runner) -> None:
    """At startup: settle runs a previous gateway process left behind.

    A worker outlives a gateway restart (it is its own process). Its
    completion still arrives through the recovered process watcher; this
    finishes the relay side — the queue — once its pid is gone.
    """
    db = _db(runner)
    for binding in await db.list_relay_bindings():
        if not binding.get("host") or binding.get("host") == "tui":
            continue  # idle, or the user's TUI: sweep_tui_hosts watches those
        root = binding["session_id"]
        pid = binding.get("host_pid")
        proc = _find_process(pid)
        if proc is not None and not proc.detached:
            continue  # started by this process: its own watcher has it
        if pid:
            runner._spawn_supervised(
                lambda r=root, p=pid: _await_exit(runner, r, p),
                f"relay_reconcile:{root}",
                restart=False,
            )
        elif not binding.get("live"):
            # Claimed for a spawn that never reported a pid: the spawner died.
            await db.clear_relay_host(root)


async def reconcile_at_startup(runner, delay_s: float = 5.0) -> None:
    """``reconcile_hosts`` once the gateway's platforms have had time to connect."""
    await asyncio.sleep(delay_s)
    try:
        await reconcile_hosts(runner)
    except Exception as exc:
        logger.warning("Relay startup reconcile failed: %s", exc)


async def _await_exit(runner, root: str, pid: int) -> None:
    from son_of_anton_state import SessionDB

    while SessionDB._relay_pid_alive(pid):
        await asyncio.sleep(WATCH_INTERVAL_S)
    try:
        await _after_exit(runner, root, pid, cwd_hint=None)
    except Exception as exc:
        logger.warning("Relay reconcile for %s failed: %s", root, exc)


# ── a TUI host ───────────────────────────────────────────────────────────


def format_relay_event(event: Dict[str, Any], brief: Dict[str, Any]) -> str:
    """The internal event a relay chat receives for one TUI-host event."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    title = brief.get("title") or payload.get("title")
    named = f"terminal session {brief.get('session') or event.get('session_id')}" + (
        f" ('{title}')" if title else ""
    )
    subject = named[:1].upper() + named[1:]
    cwd = brief.get("cwd") or payload.get("cwd") or "?"
    kind = event.get("kind")
    if kind == "handoff":
        if payload.get("busy"):
            doing = "it is running a turn right now" + (
                f" (current step: {_clip(payload['activity'], 160)})"
                if payload.get("activity") else ""
            )
        else:
            doing = "it is idle"
        return (
            f"[System note: The user handed {named}, in {cwd}, to this chat from "
            f"their TUI and left the terminal. The TUI keeps running it; {doing}. "
            "Relay the user's messages for it with the relay tool; its turn "
            "results, questions and approval requests arrive here. Tell the user "
            "briefly that you have it.]"
        )
    if kind == "turn_end":
        reply = str(payload.get("reply") or "").strip()
        if len(reply) > TURN_REPLY_CHARS:
            reply = "…" + reply[-(TURN_REPLY_CHARS - 1):]
        how = "was interrupted" if payload.get("interrupted") else "finished a turn"
        return (
            f"[System note: {subject}, handed off from the "
            f"TUI, {how}. Its final reply:\n{reply or '(no reply)'}\n"
            "Tell the user the result briefly.]"
        )
    if kind == "question":
        questions = payload.get("questions") or [
            {"question": payload.get("question"), "choices": payload.get("choices")}
        ]
        lines = []
        for index, entry in enumerate(questions, 1):
            entry = entry if isinstance(entry, dict) else {}
            choices = [str(c) for c in entry.get("choices") or []]
            text = _clip(entry.get("question"), 600)
            prefix = f"{index}. " if len(questions) > 1 else ""
            lines.append(prefix + text + (f" (choices: {' | '.join(choices)})" if choices else ""))
        several = (
            " The user's next message answers the first question; the session "
            "asks again for the rest if it needs them."
            if len(questions) > 1 else ""
        )
        return (
            f"[System note: {subject} is waiting for the "
            "user to answer:\n" + "\n".join(lines) + "\nAsk the user, and send "
            "their answer with the relay tool: the session takes the next message "
            f"as the answer.{several}]"
        )
    if kind == "pickup":
        return (
            f"[System note: The user picked {named} back up at the terminal; this "
            "chat no longer relays it. Acknowledge briefly.]"
        )
    return f"[System note: {named}: {kind}.]"


def _single_user(runner) -> bool:
    return bool(getattr(getattr(runner, "config", None), "single_user", False))


async def deliver_relay_events(runner) -> int:
    """One pass: deliver the TUI-host events waiting for their chats.

    In order per chat: once one event for a chat cannot be delivered, the
    rest for that chat wait for the next pass behind it. An event is marked
    delivered only after the chat took it; otherwise it goes back unclaimed.
    """
    if not _single_user(runner):
        return 0
    db = _db(runner)
    events = await db.claim_relay_outbox()
    held: set = set()
    delivered = 0
    for event in events:
        chat_key = event["chat_key"]
        accepted = False
        if chat_key not in held:
            try:
                _origin, chat_session_id = _chat_route(runner, chat_key)
                text = format_relay_event(event, await _session_brief(db, event["session_id"]))
                accepted = bool(await runner._inject_watch_notification(
                    text, {"session_key": chat_key, "parent_session_id": chat_session_id},
                ))
            except RelayError:
                accepted = False  # no route yet: retry next pass
            except Exception as exc:
                logger.warning("Relay event %s for %s failed: %s", event["id"], chat_key, exc)
                accepted = False
        if accepted:
            await db.finish_relay_outbox(event["id"], delivered=True)
            delivered += 1
        else:
            held.add(chat_key)
            await db.finish_relay_outbox(event["id"], delivered=False)
    return delivered


async def sweep_tui_hosts(runner) -> None:
    """A TUI host that went away hands its session back to headless workers.

    Gone means its process exited without releasing (a crash) or its
    heartbeat went stale (hung). What it left queued runs headless now, as
    after a headless run ends (``relay_finish_run`` is fenced on its pid, so
    a TUI that comes back cannot double it); with nothing queued the session
    goes idle and the next send starts a worker.

    The same goes for a headless worker that yielded to a terminal which then
    died before picking the session up: its exit left the queue for that
    terminal (``relay_finish_run``), and nobody else would run it.
    """
    db = _db(runner)
    for binding in await db.list_relay_bindings():
        if binding.get("live") or not binding.get("host_pid"):
            continue
        host = binding.get("host")
        orphaned = host == "headless" and binding.get("yield_pid") and not binding.get("yielding")
        if host != "tui" and not orphaned:
            continue
        root = binding["session_id"]
        outcome = await _after_exit(runner, root, int(binding["host_pid"]), cwd_hint=None)
        logger.info("Relay: the %s host of %s went away (%s)", host, root, outcome.get("action"))


async def relay_event_loop(runner) -> None:
    """Deliver TUI-host events (~every 2s) and watch for TUI hosts that went away."""
    await asyncio.sleep(3)  # let adapters connect
    passes = 0
    while getattr(runner, "_running", True):
        try:
            await deliver_relay_events(runner)
            if passes % TUI_SWEEP_EVERY == 0 and _single_user(runner):
                await sweep_tui_hosts(runner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Relay event pass failed: %s", exc, exc_info=True)
        passes += 1
        await asyncio.sleep(EVENT_POLL_S)


# ── actions ──────────────────────────────────────────────────────────────


async def _send(runner, chat_key: str, args: Dict[str, Any]) -> Dict[str, Any]:
    db = _db(runner)
    message = str(args.get("message") or "").strip()
    if not message:
        raise RelayError("send needs a message")
    new_in = str(args.get("new_in") or "").strip()
    cwd_hint = None
    if new_in:
        if args.get("session"):
            raise RelayError("pass either session or new_in, not both")
        cwd_hint = os.path.abspath(os.path.expanduser(new_in))
        if not os.path.isdir(cwd_hint):
            raise RelayError(f"no such directory: {cwd_hint}")
        root = _new_session_id()
    else:
        root = await _target(runner, chat_key, args.get("session"))
    _chat_route(runner, chat_key)  # fail before queueing if the chat cannot be answered

    now = bool(args.get("now"))
    decision = await db.relay_start_or_enqueue(
        root, chat_key, message, mode="now" if now else "queue",
    )
    root = decision["session_id"]
    brief = await _session_brief(db, root)
    if new_in:
        brief["cwd"] = brief.get("cwd") or cwd_hint
    if decision["action"] == "queued" and decision.get("yielding"):
        return {
            "status": "queued",
            "host": "terminal",
            "note": (
                "the user is taking this session back at their terminal; it runs "
                "there, after the turn in progress"
            ),
            **brief,
        }
    if decision["action"] == "queued" and decision.get("host") == "tui":
        return {
            "status": "sent to the TUI",
            "host": "tui",
            "note": (
                "the user's TUI runs this session; it steers the running turn "
                "with this, or runs it next when idle" if now else
                "the user's TUI runs this session; it runs this as its next turn, "
                "or takes it as the answer when the session is asking a question"
            ),
            **brief,
        }
    if decision["action"] == "queued":
        return {
            "status": "queued",
            "host": "headless",
            "position": decision["position"],
            "note": "the worker is busy; this runs after its current turn" + (
                " (a headless worker cannot be steered mid-turn)" if now else ""
            ),
            **brief,
        }
    started = await start_run(
        runner, root, decision["messages"], chat_key=chat_key, cwd_hint=cwd_hint,
    )
    if not started.get("started"):
        return {"status": "not started", "reason": started.get("reason"), **brief}
    return {
        "status": "started",
        "messages": started["messages"],
        "new_session": bool(new_in),
        **brief,
    }


async def _status(runner, chat_key: str, args: Dict[str, Any]) -> Dict[str, Any]:
    db = _db(runner)
    root = await _target(runner, chat_key, args.get("session"))
    binding = await db.get_relay_binding(root) or {}
    host = _live_host(binding)
    tip = await db.get_compaction_tip(root) or root
    rows = await db.get_messages(tip, limit=STATUS_ROWS, latest=True)
    recent = []
    for row in rows:
        role = row.get("role")
        if role == "tool":
            continue
        entry: Dict[str, Any] = {"role": role}
        if row.get("content"):
            entry["text"] = _clip(row["content"], 280)
        calls = row.get("tool_calls")
        if isinstance(calls, list) and calls:
            entry["tools"] = [
                ((c.get("function") or {}).get("name") or c.get("name") or "?")
                for c in calls if isinstance(c, dict)
            ]
        recent.append(entry)
    queued = [m for m in await db.list_relay_inbox(root) if m.get("mode") != "stop"]
    if binding.get("yielding"):
        state = (
            "yielding to the terminal: the user is taking it back; the worker "
            "stops after its current step" if host == "headless" else
            "yielding to the terminal: the user is picking it up there"
        )
    else:
        state = {
            "tui": "handed off: the user's open TUI runs it",
            "headless": "running in a headless worker",
        }.get(host, "idle")
    return {
        **(await _session_brief(db, root)),
        "bound_to_this_chat": binding.get("chat_key") == chat_key,
        "host": host,
        "state": state,
        "running_since": _when(binding.get("host_heartbeat")) if host == "headless" else None,
        "recent": recent,
        "queued": [_clip(m["text"], 160) for m in queued],
        "pending_approvals": await _pending_approvals(db, chat_key, root),
    }


async def _stop(runner, chat_key: str, args: Dict[str, Any]) -> Dict[str, Any]:
    from tools import write_approval as wa
    from tools.process_registry import ProcessRegistry, process_registry

    db = _db(runner)
    root = await _target(runner, chat_key, args.get("session"))
    binding = await db.get_relay_binding(root) or {}
    if not binding.get("host"):
        return {"stopped": False, "reason": "not running", **(await _session_brief(db, root))}
    if binding.get("host") == "tui":
        # The host is the user's own TUI process: never kill it. Ask it to
        # interrupt its running turn instead; it stays the host.
        if not binding.get("live"):
            return {"stopped": False, "reason": "not running", **(await _session_brief(db, root))}
        await db.enqueue_relay_message(root, "", mode="stop")
        return {
            "stopped": True,
            "host": "tui",
            "note": "the user's TUI interrupts its running turn, if any; the session stays handed off",
            **(await _session_brief(db, root)),
        }
    pid = binding.get("host_pid")
    # Release the host first, fenced on this run: its exit then starts nothing.
    await db.clear_relay_host(root, pid if pid else None)
    if pid:
        proc = _find_process(pid)
        if proc is not None:
            await asyncio.to_thread(process_registry.kill_process, proc.id, source="relay.stop")
        else:
            await asyncio.to_thread(ProcessRegistry._terminate_host_pid, int(pid))
    # A worker killed before its approval wait unwound leaves the request behind.
    for record in await _pending_approvals(db, chat_key, root):
        await asyncio.to_thread(wa.discard_pending, wa.EXEC, record["id"])
    queued = await db.list_relay_inbox(root)
    return {
        "stopped": True,
        "queued_kept": len(queued),
        "note": "queued messages stay and go with the next send" if queued else None,
        **(await _session_brief(db, root)),
    }


async def _detach(runner, chat_key: str, args: Dict[str, Any]) -> Dict[str, Any]:
    db = _db(runner)
    root = await _target(runner, chat_key, args.get("session"))
    host = _live_host(await db.get_relay_binding(root) or {})
    result = await db.unbind_relay_session(root)
    if result.get("yielding"):
        raise RelayError("the user is taking this session back at the terminal")
    if result.get("running"):
        raise RelayError("the session is running; stop it first or wait for it to finish")
    if result.get("unbound") and host == "tui":
        result["note"] = "the user's TUI keeps the session; it is just no longer relayed here"
    return {**result, **(await _session_brief(db, root))}


async def _list(runner, chat_key: str, args: Dict[str, Any]) -> Dict[str, Any]:
    db = _db(runner)
    workspace = str(args.get("workspace") or "").strip()
    rows = await db.list_sessions_rich(
        source="cli",
        cwd_prefix=os.path.abspath(os.path.expanduser(workspace)) if workspace else None,
        order_by_last_active=True,
        limit=LIST_LIMIT,
        compact_rows=True,
    )
    bindings = {b["session_id"]: b for b in await db.list_relay_bindings()}
    sessions = []
    for row in rows:
        binding = bindings.get(await db.relay_session_root(row["id"])) or {}
        sessions.append({
            "id": row["id"],
            "title": row.get("title"),
            "cwd": row.get("cwd"),
            "last_active": _when(row.get("last_active")),
            "messages": row.get("message_count"),
            "bound_here": binding.get("chat_key") == chat_key,
            "host": _live_host(binding),
        })
    return {"sessions": sessions}


_ACTIONS = {
    "send": _send,
    "status": _status,
    "stop": _stop,
    "detach": _detach,
    "list": _list,
}


async def handle_relay_action(runner, args: Dict[str, Any], *, chat_key: str) -> Dict[str, Any]:
    """Entry point for the relay tool; runs on the gateway loop."""
    action = str(args.get("action") or "").strip().lower()
    handler = _ACTIONS.get(action)
    if handler is None:
        return {"error": f"unknown action {action!r}; use one of {sorted(_ACTIONS)}"}
    try:
        return await handler(runner, chat_key, args)
    except RelayError as exc:
        return {"error": str(exc)}


async def attach_terminal_session(runner, chat_key: str, session_row: Dict[str, Any]) -> None:
    """``/resume`` of a terminal session: bind it and tell the chat agent."""
    db = _db(runner)
    binding = await db.bind_relay_session(session_row["id"], chat_key)
    brief = await _session_brief(db, binding["session_id"])
    _origin, chat_session_id = _chat_route(runner, chat_key)
    title = f" ('{brief['title']}')" if brief.get("title") else ""
    text = (
        f"[System note: The user attached terminal session {brief['session']}"
        f"{title}, in {brief.get('cwd') or '?'}, to this chat with /resume. "
        "It is now this chat's session for project work: that work goes to it "
        "through the relay tool, not into this conversation. Acknowledge "
        "briefly and wait for the user's request.]"
    )
    await runner._inject_watch_notification(
        text, {"session_key": chat_key, "parent_session_id": chat_session_id},
    )


__all__ = [
    "RelayError",
    "attach_terminal_session",
    "deliver_relay_events",
    "format_completion",
    "format_relay_event",
    "handle_relay_action",
    "on_worker_exit",
    "reconcile_at_startup",
    "reconcile_hosts",
    "relay_event_loop",
    "spawn_worker",
    "start_run",
    "sweep_tui_hosts",
    "worker_argv",
    "worker_env",
]

