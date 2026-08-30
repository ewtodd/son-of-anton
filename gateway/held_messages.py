"""Save and replay messages that arrive outside the active-hours window.

When ``gateway.active_hours`` is set, inbound messages during the closed
stretch are saved here instead of being dropped.  When the window reopens
the gateway presents a summary and asks the user whether to proceed.

Storage is a single JSON file (``held_messages.json``) in the sessions
directory, keyed by session_key.  The file is small — one short record
per held message — and survives gateway restarts.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _held_path(sessions_dir: Path) -> Path:
    return sessions_dir / "held_messages.json"


def _load(sessions_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    path = _held_path(sessions_dir)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def _save(sessions_dir: Path, data: Dict[str, List[Dict[str, Any]]]) -> None:
    path = _held_path(sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("held_messages: failed to persist: %s", exc)


def save_message(
    sessions_dir: Path,
    session_key: str,
    *,
    sender: str = "",
    text: str = "",
    timestamp: Optional[float] = None,
) -> int:
    """Append one held message.  Returns the new count for this session."""
    entry: Dict[str, Any] = {
        "ts": timestamp or time.time(),
        "sender": sender,
        "text": text[:2000],
    }
    with _lock:
        data = _load(sessions_dir)
        bucket = data.setdefault(session_key, [])
        bucket.append(entry)
        _save(sessions_dir, data)
        return len(bucket)


def get_messages(
    sessions_dir: Path, session_key: str
) -> List[Dict[str, Any]]:
    """Return the held messages for one session (empty list if none)."""
    with _lock:
        return _load(sessions_dir).get(session_key, [])


def clear_messages(sessions_dir: Path, session_key: str) -> None:
    """Remove all held messages for one session."""
    with _lock:
        data = _load(sessions_dir)
        if session_key in data:
            del data[session_key]
            _save(sessions_dir, data)


def clear_all(sessions_dir: Path) -> None:
    """Remove held messages for every session."""
    with _lock:
        path = _held_path(sessions_dir)
        path.unlink(missing_ok=True)


def has_messages(sessions_dir: Path, session_key: str) -> bool:
    """True when this session has at least one held message."""
    with _lock:
        return bool(_load(sessions_dir).get(session_key))


def summarize(
    sessions_dir: Path, session_key: str, *, max_entries: int = 10
) -> Optional[str]:
    """Build a human-readable summary of held messages, or None if empty."""
    msgs = get_messages(sessions_dir, session_key)
    if not msgs:
        return None

    total = len(msgs)
    shown = msgs[:max_entries]
    lines: list[str] = []

    for m in shown:
        sender = m.get("sender") or "someone"
        text = (m.get("text") or "").strip()
        if len(text) > 200:
            text = text[:200] + "…"
        if text:
            lines.append(f"  • {sender}: {text}")
        else:
            lines.append(f"  • {sender}: (empty or media-only)")

    if total > max_entries:
        lines.append(f"  … and {total - max_entries} more")

    header = (
        f"{total} message{'s' if total != 1 else ''} "
        f"came in while I was off duty:"
    )
    footer = "Process these? Reply **yes** to proceed or **no** to discard."
    return f"{header}\n\n" + "\n".join(lines) + f"\n\n{footer}"


def compose_held_turn(
    sessions_dir: Path, session_key: str
) -> Optional[str]:
    """Build the user-role text to inject when the user confirms replay.

    Returns None when there are no held messages.  Clears the store on
    success so the messages are not replayed twice.
    """
    msgs = get_messages(sessions_dir, session_key)
    if not msgs:
        return None

    parts: list[str] = []
    for m in msgs:
        sender = m.get("sender") or "someone"
        text = (m.get("text") or "").strip()
        if text:
            parts.append(f"[{sender}]: {text}")

    clear_messages(sessions_dir, session_key)
    if not parts:
        return None
    return (
        "The following messages were held while I was off duty. "
        "Please address them:\n\n" + "\n\n".join(parts)
    )
