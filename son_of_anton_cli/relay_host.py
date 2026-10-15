"""The TUI side of a relay handoff: the open TUI hosts its session for a chat.

On a ``gateway.single_user`` instance, ``/handoff`` binds the TUI's current
session to the user's messaging chat with this process as the session's host
(``relay_bindings.host = 'tui'``). Nothing about the session changes — same
process, prompt, tools and turns; the handoff is plumbing around it:

* What the relay chat sends lands in ``relay_inbox``. While handed off the TUI
  polls (one small read every ~1.5s; nothing at all otherwise), claims it and
  runs each message as a user turn: queued behind a running turn like a typed
  follow-up, steered into it for ``now``, or taken as the answer when a
  clarify question is open.
* What the chat should hear — the handoff itself, each turn's final reply, a
  question, the pickup — goes to ``relay_outbox``; the gateway delivers it as
  an internal event (gateway/relay_workers.py).
* A dangerous-command approval shows the TUI's modal *and* stages a durable
  approval routed to the chat (``/approve <id>``); a clarify shows the modal
  and asks the chat. Whichever answer comes first wins.
* ``/pickup`` (or ``son-of-anton --pickup``) ends it. Exiting the TUI only
  clears the host, so the chat's next send starts a headless worker as
  before; a crashed or hung TUI is noticed by its stale heartbeat.

``RelayHostMixin`` is mixed into ``son_of_anton_tui.backend.TextualBackend``:
the TUI is the only interactive host there is.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The messaging platforms whose chats carry the relay tool (toolsets.py).
RELAY_CHAT_SOURCES = ("signal", "discord", "slack")
# How often a handed-off TUI refreshes its heartbeat. The gateway treats one
# older than SessionDB.RELAY_TUI_STALE_S as a TUI that is gone.
HEARTBEAT_S = 5.0

_DECISIONS = ("once", "session", "always", "deny")


def single_user_configured(config: Optional[Dict[str, Any]]) -> bool:
    """``single_user`` as the gateway reads it: top level wins over ``gateway.``."""
    from utils import is_truthy_value

    config = config or {}
    if "single_user" in config:
        return is_truthy_value(config.get("single_user"))
    section = config.get("gateway")
    return isinstance(section, dict) and is_truthy_value(section.get("single_user"))


def platform_of(chat_key: str) -> str:
    """``signal`` from ``agent:main:signal:…`` — for messages, never routing."""
    parts = str(chat_key or "").split(":")
    return parts[2] if len(parts) > 2 and parts[0] == "agent" else "messaging"


def _label(platform: str) -> str:
    return {"signal": "Signal", "discord": "Discord", "slack": "Slack"}.get(platform, platform)


def clamp_decision(choice: str, allowed: List[str]) -> str:
    """An answer from the chat, narrowed to what this prompt offered."""
    choice = str(choice or "").strip().lower()
    if choice in allowed:
        return choice
    if choice == "always" and "session" in allowed:
        return "session"
    if choice in ("always", "session") and "once" in allowed:
        return "once"
    return "deny"


def _say(text: str) -> None:
    import cli as _cli

    _cli._cprint(text)


@dataclass
class RelayHost:
    """This TUI's hosting of one handed-off session."""

    root: str  # compaction-lineage root: what relay state is keyed by
    session_id: str  # the TUI's session id when last checked
    chat_key: str
    platform: str
    pid: int
    last_beat: float


class RelayHostMixin:
    """Handoff, pickup and hosting for the interactive TUI backend."""

    def _relay_init(self) -> None:
        self._relay_host: Optional[RelayHost] = None
        # Messages taken from the relay that the app has not dispatched yet
        # (a pickup's leftovers): delivered by the next app tick.
        self._relay_local: List[Dict[str, Any]] = []
        self._relay_lock = threading.Lock()
        self._relay_pickup_on_start = False

    # ------------------------------------------------------------------
    # State the app reads
    # ------------------------------------------------------------------
    def relay_active(self) -> bool:
        """Whether the app's relay tick has anything to do (no I/O)."""
        return getattr(self, "_relay_host", None) is not None or bool(
            getattr(self, "_relay_local", None)
        )

    def _relay_session_payload(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        db = getattr(self, "_session_db", None)
        if db is not None:
            try:
                row = db.get_session(self.session_id) or {}
            except Exception:
                row = {}
        return {
            "title": row.get("title"),
            "cwd": row.get("cwd") or os.environ.get("TERMINAL_CWD") or os.getcwd(),
        }

    # ------------------------------------------------------------------
    # /handoff and /pickup
    # ------------------------------------------------------------------
    def _handle_handoff_command(self, cmd_original: str = "") -> None:
        host = self._relay_host
        if host is not None:
            _say(f"  Already handed off to your {_label(host.platform)} chat — /pickup ends it.")
            return
        try:
            from son_of_anton_cli.config import load_config_readonly

            single_user = single_user_configured(load_config_readonly())
        except Exception:
            single_user = False
        if not single_user:
            _say(
                "  ✗ /handoff needs gateway.single_user = true: only a single-user "
                "instance's chat can relay terminal sessions."
            )
            return
        db = getattr(self, "_session_db", None)
        if db is None:
            _say("  ✗ The session database is unavailable.")
            return
        chat = db.latest_relay_chat(RELAY_CHAT_SOURCES)
        if chat is None:
            _say(
                "  ✗ No messaging chat on this instance yet — send the bot a message "
                "on Signal first, then /handoff again."
            )
            return
        event = self._relay_session_payload()
        event["busy"] = bool(getattr(self, "_agent_running", False))
        if event["busy"]:
            event["activity"] = str(getattr(self, "_spinner_text", "") or "").strip()
        pid = os.getpid()
        try:
            result = db.relay_tui_bind(self.session_id, chat["session_key"], pid, event=event)
        except Exception as exc:
            _say(f"  ✗ Handoff failed: {exc}")
            return
        if not result.get("bound"):
            if result.get("host") == "headless":
                _say(
                    f"  ✗ A headless relay worker is running this session (pid "
                    f"{result.get('host_pid')}); hand off once it has finished."
                )
            else:
                _say(
                    f"  ✗ Another TUI (pid {result.get('host_pid')}) hosts this "
                    "session for the relay."
                )
            return
        platform = str(chat.get("source") or platform_of(chat["session_key"]))
        self._relay_host = RelayHost(
            root=result["session_id"],
            session_id=self.session_id,
            chat_key=chat["session_key"],
            platform=platform,
            pid=pid,
            last_beat=time.monotonic(),
        )
        _say(
            f"  ↗ Handed off to your {_label(platform)} chat: this TUI keeps running "
            f"the session and {_label(platform)} relays it — messages, results, "
            "questions, approvals. /pickup when you are back."
        )

    def _handle_pickup_command(self, cmd_original: str = "") -> None:
        db = getattr(self, "_session_db", None)
        if db is None:
            _say("  ✗ The session database is unavailable.")
            return
        host = self._relay_host
        try:
            result = db.relay_pickup(
                host.root if host is not None else self.session_id,
                os.getpid(),
                event=self._relay_session_payload(),
            )
        except Exception as exc:
            _say(f"  ✗ Pickup failed: {exc}")
            return
        if not result.get("picked_up"):
            if result.get("host") == "headless":
                _say(
                    f"  ✗ A headless relay worker is mid-run on this session (pid "
                    f"{result.get('host_pid')}); /pickup once it has finished."
                )
            elif result.get("host"):
                _say(
                    f"  ✗ Another TUI (pid {result.get('host_pid')}) hosts this "
                    "session for the relay; /pickup there."
                )
            else:
                self._relay_host = None
                _say("  This session is not handed off.")
            return
        self._relay_host = None
        label = _label(platform_of(result.get("chat_key", "")))
        # Sent before the pickup but not taken yet: they run here, not lost.
        # Nothing steers any more, so each is an ordinary next turn.
        leftovers = [
            dict(m, mode="queue", platform=label)
            for m in result.get("messages") or [] if str(m.get("text") or "").strip()
        ]
        if leftovers:
            with self._relay_lock:
                self._relay_local.extend(leftovers)
        _say(
            f"  ↙ Picked up: your {label} chat no longer relays this session and "
            "has been told."
            + (f" {len(leftovers)} message(s) it sent meanwhile run here next." if leftovers else "")
        )

    def relay_pickup_at_start(self) -> None:
        """``son-of-anton --pickup``: the resumed session is picked up on launch."""
        if getattr(self, "_relay_pickup_on_start", False):
            self._relay_pickup_on_start = False
            self._handle_pickup_command("/pickup")

    # ------------------------------------------------------------------
    # Hosting (the app's relay tick runs this on a worker thread)
    # ------------------------------------------------------------------
    def relay_poll(self) -> List[Dict[str, Any]]:
        """Keep hosting alive and take what the chat sent, oldest first.

        One read when nothing waits; a claim (a write) only when something
        does; a heartbeat write every ``HEARTBEAT_S``.
        """
        with self._relay_lock:
            rows, self._relay_local = self._relay_local, []
        host = self._relay_host
        db = getattr(self, "_session_db", None)
        if host is None or db is None:
            return rows
        try:
            if self.session_id != host.session_id:
                # Compaction keeps the lineage root (still ours); /new and
                # /resume leave it, and this TUI stops hosting it.
                if db.relay_session_root(self.session_id) != host.root:
                    self._relay_leave(host)
                    return rows
                host.session_id = self.session_id
            state = db.relay_tui_poll(host.root, host.pid)
            if state is None:
                self._relay_lost(host)
                return rows
            host.chat_key = state["chat_key"]
            now = time.monotonic()
            if state["waiting"]:
                claimed = db.relay_tui_claim(host.root, host.pid)
                if claimed is None:
                    self._relay_lost(host)
                    return rows
                rows.extend(dict(r, platform=_label(host.platform)) for r in claimed)
                host.last_beat = now
            elif now - host.last_beat >= HEARTBEAT_S:
                if not db.relay_tui_beat(host.root, host.pid):
                    self._relay_lost(host)
                    return rows
                host.last_beat = now
        except Exception:
            logger.debug("relay poll failed", exc_info=True)
        return rows

    def _relay_lost(self, host: RelayHost) -> None:
        """The binding is gone or moved: stop hosting (if still the same one)."""
        if self._relay_host is not host:
            return
        self._relay_host = None
        _say(
            f"  Your {_label(host.platform)} chat no longer relays this session "
            "(detached or taken over); it is not handed off any more."
        )

    def _relay_leave(self, host: RelayHost) -> None:
        """This TUI moved to another session: release the one it hosted."""
        if self._relay_host is not host:
            return
        self._relay_host = None
        self._relay_release(host)
        _say(
            f"  The handed-off session is no longer open here; your "
            f"{_label(host.platform)} chat's next message for it starts a headless worker."
        )

    def _relay_release(self, host: RelayHost) -> None:
        db = getattr(self, "_session_db", None)
        if db is None:
            return
        try:
            db.relay_tui_release(host.root, host.pid)
        except Exception:
            logger.debug("relay release failed", exc_info=True)

    def relay_return_unsent(self, texts: List[str]) -> None:
        """The TUI is closing with relayed messages it never ran: put them back.

        They return to the session's inbox, where ``relay_tui_release`` sees
        them and leaves the host for the gateway to hand them to a headless
        run once this process is gone.
        """
        host = getattr(self, "_relay_host", None)
        db = getattr(self, "_session_db", None)
        if host is None or db is None:
            return
        for text in texts:
            try:
                db.enqueue_relay_message(host.root, text)
            except Exception:
                logger.warning("could not return a relayed message to the inbox", exc_info=True)

    def relay_release_on_exit(self) -> None:
        """The TUI is exiting: clear the host, keep the binding."""
        host = getattr(self, "_relay_host", None)
        if host is None:
            return
        self._relay_host = None
        self._relay_release(host)

    # ------------------------------------------------------------------
    # Events for the chat
    # ------------------------------------------------------------------
    def _relay_event(self, kind: str, payload: Dict[str, Any]) -> None:
        host = self._relay_host
        db = getattr(self, "_session_db", None)
        if host is None or db is None:
            return
        try:
            if db.add_relay_event(host.root, host.pid, kind, payload) is None:
                self._relay_lost(host)
        except Exception:
            logger.debug("relay event %s failed", kind, exc_info=True)

    def relay_after_turn(self, reply: Any, *, interrupted: bool = False) -> None:
        """A turn ended while handed off: its final reply goes to the chat."""
        if getattr(self, "_relay_host", None) is None:
            return
        self._relay_event(
            "turn_end", {"reply": str(reply or "").strip(), "interrupted": bool(interrupted)},
        )

    # ------------------------------------------------------------------
    # Clarify: the modal and the chat; the first answer wins
    # ------------------------------------------------------------------
    def _clarify_callback(self, question, choices, multi_select=False, questions=None):
        if getattr(self, "_relay_host", None) is not None:
            if questions:
                payload = {"questions": [
                    {"question": q.get("question"), "choices": list(q.get("choices") or [])}
                    for q in questions if isinstance(q, dict)
                ]}
            else:
                payload = {"question": question, "choices": list(choices or [])}
            self._relay_event("question", payload)
        return super()._clarify_callback(  # type: ignore[misc]
            question, choices, multi_select=multi_select, questions=questions,
        )

    def relay_answer_clarify(self, text: str) -> bool:
        """Answer the open clarify with a relayed message; False when none is open.

        A batch is resolved at once: the text answers its first unanswered
        question and the rest stay blank — the agent can ask again.
        """
        state = getattr(self, "_clarify_state", None)
        if not state:
            return False
        if state.get("questions"):
            answers = dict(state.get("answers") or {})
            for entry in state["questions"]:
                if entry.get("qid") not in answers:
                    answers[entry.get("qid")] = text
                    break
            reply: Any = answers
        else:
            reply = text
        self._clarify_state = None
        self._clarify_freetext = False
        self._clarify_multi_base = None
        state["response_queue"].put(reply)
        return True

    # ------------------------------------------------------------------
    # Approvals: the modal and a durable record for the chat; first wins
    # ------------------------------------------------------------------
    def _relay_approval_open(self, command: str, description: str, choices: list):
        host = getattr(self, "_relay_host", None)
        if host is None:
            return None
        from tools import write_approval as wa
        from tools.approval import get_current_session_key

        allowed = [c for c in choices if c in _DECISIONS]
        try:
            record = wa.stage_write(
                wa.EXEC,
                {
                    "action": "exec_approval",
                    "session_id": get_current_session_key(),
                    "agent_session_id": self.session_id,
                    "command": command,
                    "description": description,
                    "allow_session": "session" in allowed,
                    "allow_permanent": "always" in allowed,
                },
                summary=f"{description}: {str(command)[:120]}",
                chat_key=host.chat_key,
            )
        except Exception:
            logger.debug("relay approval staging failed", exc_info=True)
            return None
        return {"id": record["id"], "allowed": allowed, "host": host, "withdrawn": False}

    def _relay_approval_poll(self, ticket):
        """The chat's decision, if it answered first; None otherwise."""
        if not ticket or ticket.get("withdrawn"):
            return None
        from tools import write_approval as wa

        if self._relay_host is not ticket["host"]:
            # Picked up meanwhile: the chat can no longer answer this one.
            ticket["withdrawn"] = True
            wa.discard_pending(wa.EXEC, ticket["id"])
            return None
        record = wa.get_pending(wa.EXEC, ticket["id"])
        if record and record.get("decision"):
            return clamp_decision(record["decision"], ticket["allowed"])
        return None

    def _relay_approval_settle(self, ticket, result):
        """Record the answer on the durable request (first wins), then drop it."""
        if not ticket:
            return result
        from tools import write_approval as wa

        try:
            if not ticket.get("withdrawn") and result in _DECISIONS:
                record = wa.decide_pending(wa.EXEC, ticket["id"], result, decided_by="tui")
                if record and record.get("decision"):
                    result = clamp_decision(record["decision"], ticket["allowed"])
        finally:
            wa.discard_pending(wa.EXEC, ticket["id"])
        return result


def pickup_target(db) -> Optional[Dict[str, Any]]:
    """The terminal session most recently handed off or relayed here, if any."""
    for binding in db.list_relay_bindings():
        root = binding["session_id"]
        row = db.get_session(root)
        if row is not None and row.get("source") != "cli":
            continue
        tip = db.get_compaction_tip(root) or root
        return {**binding, "tip": tip, "title": (db.get_session(tip) or row or {}).get("title")}
    return None


def resolve_pickup(*, interactive: bool, poll_s: float = 2.0) -> Optional[str]:
    """``son-of-anton --pickup``: the session id to resume, or None to stop.

    A headless worker mid-run on it is never killed: the user may wait for it
    (interactive) or come back later.
    """
    from son_of_anton_state import SessionDB

    db = SessionDB()
    try:
        target = pickup_target(db)
        if target is None:
            print("Nothing is handed off or relayed on this instance.")
            return None
        name = f"{target['session_id']}" + (f" ('{target['title']}')" if target.get("title") else "")
        if target.get("live") and target.get("host") == "tui":
            print(
                f"Session {name} is handed off from a TUI that is still open "
                f"(pid {target.get('host_pid')}); type /pickup there."
            )
            return None
        if target.get("live") and target.get("host") == "headless":
            print(
                f"Session {name} is mid-run in a headless relay worker "
                f"(pid {target.get('host_pid')})."
            )
            if not interactive:
                print("Run --pickup again once it has finished.")
                return None
            try:
                answer = input("Wait for it to finish? [Y/n] ").strip().lower()
            except EOFError:
                answer = "n"
            if answer not in ("", "y", "yes"):
                return None
            print("Waiting… (Ctrl+C to give up)")
            try:
                while True:
                    binding = db.get_relay_binding(target["session_id"]) or {}
                    if not (binding.get("live") and binding.get("host") == "headless"):
                        break
                    time.sleep(poll_s)
            except KeyboardInterrupt:
                print()
                return None
        return db.get_compaction_tip(target["session_id"]) or target["session_id"]
    finally:
        db.close()


__all__ = [
    "HEARTBEAT_S",
    "RELAY_CHAT_SOURCES",
    "RelayHost",
    "RelayHostMixin",
    "clamp_decision",
    "pickup_target",
    "platform_of",
    "resolve_pickup",
    "single_user_configured",
]
