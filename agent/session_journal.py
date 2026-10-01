"""Per-session JSONL journal: the durable detail the transcript clips.

Every message row the session DB persists is mirrored, as it lands, into
``$SON_OF_ANTON_HOME/journals/<session-id>.jsonl``. The DB row keeps what the
model saw (tool results can be previews with a spillover path) and compaction
summarizes old turns; the journal keeps the row record as a plain file that a
human or the model can grep without a query interface. The idea comes from
letsClaw (https://github.com/goluckyryan/letsClaw), whose rollover writes a
per-window journal beside the archived transcript; see the README.

The journal is write-only from the agent's point of view — it is never read
back into a context — so a crash between the DB commit and the append loses
at most one flush, and every failure path here is swallowed.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from son_of_anton_constants import get_son_of_anton_home

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is always present on supported platforms
    fcntl = None

logger = logging.getLogger(__name__)

_JOURNAL_SUBDIR = "journals"

# Row fields worth mirroring. Internal plumbing (display metadata, codex
# reasoning items, api_content) is deliberately excluded.
_ROW_FIELDS = (
    "role",
    "content",
    "tool_name",
    "tool_calls",
    "tool_call_id",
    "finish_reason",
    "reasoning",
    "reasoning_content",
)


def journal_dir() -> Path:
    """Return ``$SON_OF_ANTON_HOME/journals`` (not created)."""
    return get_son_of_anton_home() / _JOURNAL_SUBDIR


def journal_path(session_id: str) -> Path:
    """Return the journal path for a session id, safe for filenames."""
    safe = "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in str(session_id or "")
    )
    return journal_dir() / f"{safe}.jsonl"


def append_session_journal(
    session_id: str,
    rows: Iterable[Dict[str, Any]],
    *,
    session_header: Optional[Dict[str, Any]] = None,
) -> None:
    """Append persisted message rows to the session journal. Never raises.

    ``session_header`` is written once, when the file is created, so the
    journal is self-describing (session id, cwd, model, start time).
    """
    rows = [row for row in rows if isinstance(row, dict)]
    if not session_id or not rows:
        return
    try:
        path = journal_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with open(lock_path, "a+", encoding="utf-8") as lock_handle:
            if fcntl is not None:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass
            try:
                with open(path, "a", encoding="utf-8") as handle:
                    if path.stat().st_size == 0:
                        header = {
                            "type": "session",
                            "session_id": str(session_id),
                            "ts": time.time(),
                        }
                        if session_header:
                            header.update(
                                {key: value for key, value in session_header.items() if value is not None}
                            )
                        handle.write(_dump_line(header))
                    for row in rows:
                        entry = {"type": "message", "ts": row.get("timestamp") or time.time()}
                        for field in _ROW_FIELDS:
                            value = row.get(field)
                            if value is not None:
                                entry[field] = value
                        handle.write(_dump_line(entry))
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
    except Exception as exc:
        logger.debug("Session journal append failed for %s: %s", session_id, exc)


def _dump_line(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"
