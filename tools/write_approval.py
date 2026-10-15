#!/usr/bin/env python3
"""Write-approval gate + pending store for memory writes and durable exec approvals.

Memory gate
-----------
The agent writes MEMORY.md / USER.md, small (~200 char) declarative entries
that survive across sessions. ``memory.write_approval`` gates those writes:

  * ``false`` (default) — write freely (the pre-gate behaviour)
  * ``true``            — require approval: do not commit the write; either
    prompt inline (interactive CLI only) or **stage** it to a pending store
    and surface it for the user to approve or reject out-of-band
    (``/memory pending|approve|reject``)

Staging is used whenever there is no inline prompt channel — gateway
sessions, scripts, cron. Interactive CLI writes prompt inline via the
dangerous-command approval callback.

Durable exec approvals
----------------------
The same pending store carries dangerous-command approval requests for a
headless process (``exec``): the blocked process writes the request, a chat
relays the decision with :func:`decide_pending`, and the process polls.

Pending records live under ``<SON_OF_ANTON_HOME>/pending/{memory,exec}/<id>.json``
so they survive process restarts and can be reviewed from CLI or gateway.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from son_of_anton_constants import get_son_of_anton_home

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is always present on supported platforms
    fcntl = None

logger = logging.getLogger(__name__)

# Subsystem identifiers
MEMORY = "memory"
# Dangerous-command approvals for a headless process: the pending record is
# the request (written by the blocked process), decide_pending is the answer
# (written by whatever chat is relaying it), and the blocked process polls.
EXEC = "exec"
_SUBSYSTEMS = (MEMORY, EXEC)

# Config key (per subsystem). A single boolean: the approval gate is OFF by
# default (writes flow freely, the pre-gate behaviour), and ON means stage /
# prompt every write for the user's approval. There is intentionally no third
# "block all writes" state — to disable a subsystem entirely use its own
# enable flag (e.g. ``memory.memory_enabled: false``).
CONFIG_KEY = "write_approval"


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def write_approval_enabled(subsystem: str) -> bool:
    """Return whether the approval gate is enabled for ``subsystem``.

    Reads ``<subsystem>.write_approval`` from config.toml. Defaults to
    ``False`` (gate off — writes flow freely) for any unset / invalid value so
    existing installs keep their current behaviour until the user opts in.
    """
    if subsystem not in _SUBSYSTEMS:
        return False
    try:
        from son_of_anton_cli.config import load_config, cfg_get
        cfg = load_config()
        raw = cfg_get(cfg, subsystem, CONFIG_KEY, default=False)
    except Exception:
        return False
    return _normalize_enabled(raw)


def _normalize_enabled(value: Any) -> bool:
    """Coerce a config value to a bool. Default (unknown) is False (gate off).

    Accepts real bools and the usual truthy/falsey strings. TOML 1.1 parses
    bare ``on``/``off``/``yes``/``no`` as bools already, so the string branch
    is mostly for hand-edited configs.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"on", "true", "yes", "1", "approve", "enabled"}
    return False


# ---------------------------------------------------------------------------
# Pending store (file-backed)
# ---------------------------------------------------------------------------

def _pending_dir(subsystem: str) -> Path:
    return get_son_of_anton_home() / "pending" / subsystem


def stage_write(subsystem: str, payload: Dict[str, Any],
                *, summary: str,
                expires_at: Optional[float] = None,
                chat_key: str = "") -> Dict[str, Any]:
    """Persist a pending write and return a short record describing it.

    Args:
        subsystem: ``memory`` or ``exec``.
        payload: the exact kwargs needed to replay the write when approved
            (e.g. ``{"action": "add", "target": "user", "content": "..."}``
            for memory). For ``exec`` it carries the command/pattern data the
            approver needs; the blocked process applies the allowance itself.
        summary: a one-line human-readable description shown in pending lists
            (for memory it can be the entry text itself).
        expires_at: optional epoch deadline. Past it the request is dead and
            the blocked process fails closed.
        chat_key: optional session/chat key that asked for the request, so a
            relay can route the prompt back to the right conversation.

    Returns a dict with ``id`` and metadata. Best-effort: on disk failure it
    logs and still returns a record (the write is simply lost, which is the
    safe failure for an approval gate — nothing is silently committed).
    """
    pid = uuid.uuid4().hex[:8]
    record = {
        "id": pid,
        "subsystem": subsystem,
        "action": payload.get("action", ""),
        "summary": (summary or "").strip(),
        "created_at": time.time(),
        "payload": payload,
    }
    if expires_at is not None:
        record["expires_at"] = float(expires_at)
    if chat_key:
        record["chat_key"] = str(chat_key)
    try:
        d = _pending_dir(subsystem)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{pid}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:  # pragma: no cover - disk failure path
        logger.error("Failed to stage pending %s write: %s", subsystem, e, exc_info=True)
    return record


def list_pending(subsystem: str) -> List[Dict[str, Any]]:
    """Return all pending records for ``subsystem``, oldest first."""
    d = _pending_dir(subsystem)
    if not d.exists():
        return []
    records: List[Dict[str, Any]] = []
    for p in d.glob("*.json"):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("Skipping unreadable pending record: %s", p)
    records.sort(key=lambda r: r.get("created_at", 0))
    return records


def get_pending(subsystem: str, pending_id: str) -> Optional[Dict[str, Any]]:
    """Return a single pending record by id, or None."""
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def discard_pending(subsystem: str, pending_id: str) -> bool:
    """Delete a pending record. Returns True if it existed."""
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception as e:  # pragma: no cover
        logger.error("Failed to discard pending %s/%s: %s", subsystem, pending_id, e)
    return False


def pending_count(subsystem: str) -> int:
    """Cheap count of pending records (for notification badges)."""
    d = _pending_dir(subsystem)
    if not d.exists():
        return 0
    try:
        return sum(1 for _ in d.glob("*.json"))
    except Exception:
        return 0


@contextmanager
def _record_lock(path: Path):
    """Serialize read-modify-write on one pending record across processes.

    A durable exec approval's decision must not be overwritable by a second
    approver: lock the record, re-read, and only write when still undecided.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if handle is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


def _write_record(subsystem: str, record: Dict[str, Any]) -> bool:
    """Atomically rewrite an existing pending record."""
    try:
        path = _pending_dir(subsystem) / f"{record['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error("Failed to update pending %s/%s: %s", subsystem, record.get("id"), e)
        return False


def decide_pending(subsystem: str, pending_id: str, choice: str,
                   *, decided_by: str = "", reason: str = "") -> Optional[Dict[str, Any]]:
    """Record the user's decision on a pending request. First decision wins.

    Returns the updated record, or None when the request no longer exists.
    A second decision is ignored (the existing record is returned unchanged),
    so two chats or a retried tap can never overwrite the first answer.
    """
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    with _record_lock(path):
        record = get_pending(subsystem, pending_id)
        if record is None:
            return None
        if record.get("decision"):
            return record
        record["decision"] = str(choice or "").strip().lower()
        record["decided_at"] = time.time()
        record["decided_by"] = str(decided_by or "")
        if reason:
            record["decision_reason"] = str(reason)[:280]
        _write_record(subsystem, record)
        return record


def mark_pending_notified(subsystem: str, pending_id: str) -> bool:
    """Stamp ``notified_at`` once, so a watcher does not re-send a prompt."""
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    with _record_lock(path):
        record = get_pending(subsystem, pending_id)
        if record is None or record.get("notified_at"):
            return False
        record["notified_at"] = time.time()
        return _write_record(subsystem, record)


def pending_expired(record: Dict[str, Any], *, now: Optional[float] = None) -> bool:
    """True when a durable request has outlived its timeout."""
    expires_at = record.get("expires_at")
    if not isinstance(expires_at, (int, float)) or expires_at <= 0:
        return False
    return (now if now is not None else time.time()) >= float(expires_at)


def prune_expired_pending(subsystem: str) -> int:
    """Delete expired pending records; returns how many were removed."""
    removed = 0
    for record in list_pending(subsystem):
        if pending_expired(record) and discard_pending(subsystem, record.get("id", "")):
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Gate decision
# ---------------------------------------------------------------------------

class GateDecision:
    """Result of evaluating the write gate for a single memory write.

    Exactly one of the boolean flags is True:
      * ``allow``  — proceed with the real write (gate off, or an inline
        approval was granted).
      * ``blocked`` — refuse the write (the user denied an inline approval
        prompt). ``message`` explains why; surface it to the agent.
      * ``stage``  — do not write; the caller should stage the payload via
        ``stage_write`` (gate on, and no inline prompt is available — gateway,
        script, cron). ``message`` is the user-facing "staged for approval"
        note.
    """

    __slots__ = ("allow", "blocked", "stage", "message")

    def __init__(self, *, allow=False, blocked=False, stage=False, message=""):
        self.allow = allow
        self.blocked = blocked
        self.stage = stage
        self.message = message


def evaluate_gate(subsystem: str, *, inline_summary: str = "",
                  inline_detail: str = "") -> GateDecision:
    """Decide what to do with a pending write for ``subsystem`` (``memory``).

    Args:
        subsystem: ``memory``.
        inline_summary: short description used as the inline approval prompt
            header.
        inline_detail: full content shown in the inline prompt (memory entries
            are small enough to show in full).

    Decision matrix:
        gate off (default)              → allow (writes flow freely)
        gate on + interactive CLI       → inline approve/deny prompt
        gate on + gateway/script/cron   → stage

    Note: there is no config-driven "blocked" outcome — the gate only ever
    delays a write for approval, never silently refuses it. ``blocked`` is
    still produced when the user *actively denies* an inline prompt.
    """
    if not write_approval_enabled(subsystem):
        return GateDecision(allow=True)

    # If an interactive approval channel exists (a CLI approval callback
    # registered on this thread), prompt inline — entries are small enough to
    # show in full. Otherwise (gateway, script, batch, no listener) stage
    # instead of forcing a blind deny.
    if _interactive_approval_available():
        granted = _prompt_inline_memory_approval(inline_summary, inline_detail)
        if granted is True:
            return GateDecision(allow=True)
        if granted is False:
            return GateDecision(
                blocked=True,
                message="Memory write denied by user. The change was not saved.",
            )
        # granted is None → prompt failed; fall through to staging.

    return GateDecision(
        stage=True,
        message=(
            f"Staged for approval ({subsystem}.write_approval is on). "
            f"Not yet saved — review with /{subsystem} pending."
        ),
    )


def _interactive_approval_available() -> bool:
    """True when a foreground memory write can be approved inline.

    Inline prompting requires a per-thread approval callback registered by the
    interactive CLI (``tools.terminal_tool.set_approval_callback``). Every
    other surface stages instead:

    * **Gateway/API sessions** — the dangerous-command ``/approve`` round-trip
      lives in the pending-approval queue (``submit_pending`` +
      ``_await_gateway_decision``), which ``prompt_dangerous_approval`` never
      reaches; trying to prompt from a gateway session would hit the
      ``input()`` fallback and silently deny. Staging gives the user a real
      review affordance (``/memory pending``) instead.
    * Scripts, cron, and background threads — no user present.
    """
    try:
        from tools.terminal_tool import _get_approval_callback
        return _get_approval_callback() is not None
    except Exception:
        return False


def _prompt_inline_memory_approval(summary: str, detail: str) -> Optional[bool]:
    """Prompt the user inline to approve a memory write.

    Returns True (approved), False (denied), or None (no interactive prompt
    available / prompt failed → caller should stage instead).

    Reuses the per-thread CLI approval callback registered for dangerous
    commands (``tools.terminal_tool.set_approval_callback``). The callback is
    invoked directly — NOT via ``prompt_dangerous_approval`` — because that
    wrapper falls back to ``input()`` (deadlock-prone under prompt_toolkit,
    see #15216) and converts callback errors into a silent deny; here a
    failed prompt must stage the write instead.
    """
    try:
        from tools.terminal_tool import _get_approval_callback
    except Exception:
        return None

    callback = _get_approval_callback()
    if callback is None:
        # No interactive channel on this thread — stage rather than risk the
        # input() fallback (deadlock under prompt_toolkit, EOF-deny in tests).
        return None

    header = summary.strip() or "Save to memory?"
    body = detail.strip()
    description = f"Save to memory: {header}"
    command = body if body else header
    # Invoke the callback directly instead of via prompt_dangerous_approval:
    # that wrapper swallows callback exceptions into "deny", which would
    # silently refuse the write. Direct invocation lets a crashed prompt fall
    # back to staging (the gate only ever delays a write, never drops it).
    try:
        choice = callback(command, description, allow_permanent=False)
    except Exception as e:
        logger.error("Inline memory approval prompt failed: %s", e)
        return None

    if choice in {"once", "session"}:
        return True
    if choice == "deny":
        return False
    # Any other outcome (e.g. timeout that returns "deny" already handled) →
    # treat unknown as no-decision so we stage rather than silently drop.
    return None
