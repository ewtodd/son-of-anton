"""The worker side of a relay run: a headless terminal turn driven from a chat.

A ``gateway.single_user`` messaging chat hands project work to an ordinary
terminal session (``source=cli``) by spawning ``son-of-anton chat -Q`` on it
(see ``gateway/relay_workers.py``). The worker is the same CLI agent as a TUI
session — same prompt, same tools, so a session's prompt cache carries over
between the two surfaces. Only two things differ, both read from the
environment the gateway sets:

* ``SON_OF_ANTON_NEW_SESSION_ID`` names the session a new worker creates, so
  the chat can bind it and report on it before the run ends. Honored only
  when not resuming.
* ``SON_OF_ANTON_RELAY_RUN`` marks the run. Nobody sits at this terminal, so
  a clarify question gets an immediate answer telling the model to put the
  question in its reply and end the turn; the reply reaches the user in the
  chat, and their answer comes back as the next turn.

Both are taken out of ``os.environ`` when read, so a process the worker
starts — say, another ``son-of-anton`` run in its terminal — does not inherit
them.

A relay run also listens for the user coming back: ``/pickup`` (or
``son-of-anton --pickup``) at a terminal asks the run to yield
(``SessionDB.relay_request_yield``). :func:`run_relay_turn` polls for that
about once a second and asks the agent to yield: it stops at its next step
boundary — a running command finishes, a model call in flight is cancelled —
persisting nothing for the stop, and the run exits with
:data:`YIELDED_EXIT_CODE` so the terminal continues the same turn. A durable
approval the run is waiting on is released (the command stays unrun). If the
terminal gives up before the run committed, the run continues its own turn.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Callable, Dict, MutableMapping, Optional, Tuple

logger = logging.getLogger(__name__)

NEW_SESSION_ID_ENV = "SON_OF_ANTON_NEW_SESSION_ID"
RELAY_RUN_ENV = "SON_OF_ANTON_RELAY_RUN"

# What a session id may look like when it arrives from outside: the CLI's own
# ``YYYYmmdd_HHMMSS_<hex>`` shape and anything else that is a safe file name.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Starts like the other no-user clarify answers ("[oneshot mode:") so
# compaction never quotes it as something the user said. No commas: a
# multi-select answer is split on them.
RELAY_CLARIFY_RESPONSE = (
    "[relay mode: no one is at this terminal — the user reads your replies "
    "in a chat and cannot answer this prompt. Do not choose for them and do "
    "not continue the task: end your turn now with your question in your "
    "reply. Their answer arrives as the next message.]"
)


def take_preassigned_session_id(
    environ: Optional[MutableMapping[str, str]] = None,
) -> Optional[str]:
    """Pop ``SON_OF_ANTON_NEW_SESSION_ID``; return it when well-formed.

    Raises ``ValueError`` for a malformed id: a caller that named the session
    must not silently get a different one.
    """
    env = os.environ if environ is None else environ
    raw = str(env.pop(NEW_SESSION_ID_ENV, "") or "").strip()
    if not raw:
        return None
    if not _SESSION_ID_RE.match(raw):
        raise ValueError(f"{NEW_SESSION_ID_ENV} is not a valid session id: {raw!r}")
    return raw


def take_relay_run_marker(environ: Optional[MutableMapping[str, str]] = None) -> bool:
    """Pop ``SON_OF_ANTON_RELAY_RUN``; True when this process is a relay run."""
    env = os.environ if environ is None else environ
    return str(env.pop(RELAY_RUN_ENV, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def relay_clarify_callback(question, choices, multi_select=False, questions=None):
    """Clarify in a relay run: answer at once, never wait for a terminal user."""
    if questions:
        return {"answers": {q["qid"]: RELAY_CLARIFY_RESPONSE for q in questions}}
    return RELAY_CLARIFY_RESPONSE


# A run that handed its turn to a terminal exits with this (EX_TEMPFAIL) and
# prints YIELDED_LINE: the turn is not over, it goes on at the terminal. The
# gateway knows it from the binding; the status makes it plain to anything
# else that reads the run.
YIELDED_EXIT_CODE = 75
YIELDED_LINE = "[relay: handed the turn to the terminal]"
# How often a relay run checks whether a terminal asks for its session.
YIELD_POLL_S = 1.0
# The reason a durable approval wait ends with when the terminal takes over;
# the tool result reads like a denial with this reason, so the model asks
# again — at the terminal now.
YIELD_APPROVAL_REASON = (
    "the user took this session back at the terminal before answering, so "
    "the command was not run; ask them there if it is still needed"
)


class YieldWatch:
    """A relay run's ear for a terminal taking its session back.

    One small read per poll (``relay_yield_requested``). Each request — a
    terminal pid and when it asked — reaches the agent once, as
    ``request_yield()``; while one is live, a durable approval wait is
    released (``release_reason``).
    """

    def __init__(
        self, db: Any, agent: Any, session_id: Callable[[], str], poll_s: float = YIELD_POLL_S,
    ) -> None:
        self._db = db
        self._agent = agent
        self._session_id = session_id
        self._poll_s = poll_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._acted_on: Any = None
        self.requested = False

    def check(self) -> bool:
        """Poll once; True while a live terminal asks for the session."""
        try:
            request = self._db.relay_yield_requested(self._session_id())
        except Exception:
            logger.debug("relay yield poll failed", exc_info=True)
            return self.requested
        self.requested = request is not None
        if request is not None and request.get("at") != self._acted_on:
            self._acted_on = request.get("at")
            logger.info("A terminal (pid %s) is taking this session back: yielding",
                        request.get("taker_pid"))
            self._agent.request_yield()
        return self.requested

    def release_reason(self) -> Optional[str]:
        return YIELD_APPROVAL_REASON if self.requested else None

    def start(self) -> "YieldWatch":
        self._thread = threading.Thread(target=self._run, name="relay-yield-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        self.check()
        while not self._stop.wait(self._poll_s):
            self.check()


def run_relay_turn(
    agent: Any, db: Any, first_run: Callable[[], Dict[str, Any]], *, poll_s: float = YIELD_POLL_S,
) -> Tuple[Dict[str, Any], bool]:
    """Run a relay worker's turn; ``(result, handed_over)``.

    ``first_run`` runs the turn (the relayed message). While it runs, a
    terminal asking for the session makes the agent yield; at the end the
    run commits the handover (``relay_confirm_yield``) if a live terminal
    still asks — whether the turn yielded or finished just then. A terminal
    that gave up after the turn yielded gets nothing to take: the run
    continues the same turn itself, as many times as that happens.
    """
    from tools.approval import set_durable_wait_release

    def _session() -> str:
        return str(getattr(agent, "session_id", "") or "")

    watch = YieldWatch(db, agent, _session, poll_s=poll_s).start()
    set_durable_wait_release(watch.release_reason)
    try:
        result = first_run()
        while True:
            try:
                handed = bool(db.relay_confirm_yield(_session()))
            except Exception:
                logger.warning("could not settle a terminal takeover", exc_info=True)
                handed = False
            if handed:
                return result, True
            if not (isinstance(result, dict) and result.get("yielded")):
                return result, False
            logger.info("The terminal stopped waiting; this run continues its turn")
            result = agent.run_conversation(
                None, conversation_history=result.get("messages"), continue_turn=True,
            )
    finally:
        set_durable_wait_release(None)
        watch.stop()
