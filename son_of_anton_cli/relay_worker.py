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
"""

from __future__ import annotations

import os
import re
from typing import MutableMapping, Optional

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
