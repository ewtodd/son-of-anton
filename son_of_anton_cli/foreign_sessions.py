"""Import sessions from foreign coding agents (Claude Code, Codex CLI).

``son-of-anton sessions import`` (and ``--resume @claude`` / ``--resume @codex``)
let a user pull a conversation they started in another agent CLI into
Son of Anton and continue it here.

Sources (read-only — foreign files are never modified):

* **Claude Code** stores one JSONL file per session under
  ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl``.  Each line is a JSON
  object; ``type: "user"`` / ``type: "assistant"`` lines carry an
  Anthropic-format ``message`` payload whose ``content`` is either a string
  or a list of blocks (``text``, ``tool_use``, ``tool_result``, ...).
  ``type: "summary"`` lines carry a human title for the thread.

* **Codex CLI** stores rollout JSONL under
  ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``.  The first line is a
  ``session_meta`` record (cwd, session id); conversation turns are
  ``response_item`` records whose payload is ``{"type": "message",
  "role": user|assistant|developer, "content": [{"type": "input_text"|
  "output_text", "text": ...}]}`` plus ``custom_tool_call`` /
  ``function_call`` payloads for tool activity.  (Schema verified against
  real rollout files, Codex CLI 0.147 and 0.144.)

* **opencode** stores every project's sessions in one SQLite database,
  ``$XDG_DATA_HOME/opencode/opencode.db`` (default
  ``~/.local/share/opencode/opencode.db``).  ``session`` rows carry the
  directory and title; ``message`` rows carry the role; ``part`` rows carry
  text, tool, file, reasoning, patch, and compaction payloads.  The database
  is opened read-only.  Root sessions (``parent_id IS NULL``) are listed —
  child rows are subagent transcripts.  A pending ``session.revert``
  truncates the transcript to what the TUI shows, matching opencode's own
  cleanup on the next prompt; ``compaction`` parts become a
  ``[conversation compacted]`` marker and the summary assistant turn is kept.
  (Schema verified against a live opencode 1.18 database.)

Conversion contract — imported history must satisfy the provider
role-alternation invariant Son of Anton enforces everywhere else:

* only plain ``user`` / ``assistant`` text messages are produced (tool
  calls become short bracketed summaries inside the assistant text; we
  never fabricate ``tool_calls`` structures);
* consecutive same-role turns are merged rather than stubbed;
* system/developer payloads are never imported.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# User-message texts that are really injected context wrappers, not typed
# input. Matched against the stripped start of the text.
_WRAPPER_TAG_RE = re.compile(
    r"^<(?:user_instructions|environment_context|recommended_plugins|"
    r"skills_instructions|permissions[_-]instructions|turn_context|"
    r"command-name|command-message|command-args|local-command-stdout|"
    r"local-command-caveat|ide_opened_file|ide_selection|"
    r"bash-input|bash-stdout|bash-stderr|system-reminder)\b",
    re.IGNORECASE,
)

_TITLE_MAX = 60


@dataclass
class ForeignSession:
    """A discoverable session in another tool's on-disk store."""

    source: str  # "claude" | "codex"
    path: Path
    mtime: float
    cwd: Optional[str] = None
    title_guess: Optional[str] = None
    turn_count: int = 0
    session_id: Optional[str] = None  # the foreign tool's own id

    @property
    def label(self) -> str:
        name = {"claude": "Claude Code", "codex": "Codex CLI"}.get(
            self.source, self.source
        )
        title = (self.title_guess or "").strip() or self.path.stem
        return f"[{name}] {title[:_TITLE_MAX]}"


def _read_json_lines(path: Path):
    """Yield parsed JSON objects, silently skipping unparseable lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _flatten_blocks(content: Any, *, source: str) -> str:
    """Flatten a message ``content`` (string or block list) to plain text.

    Tool activity becomes a short bracketed summary; unknown block types
    are skipped rather than guessed at.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            if isinstance(block, str):
                parts.append(block)
            continue
        btype = block.get("type")
        if btype in ("text", "input_text", "output_text"):
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif btype == "tool_use":  # Claude Code assistant block
            name = block.get("name") or "tool"
            parts.append(f"[ran tool: {name}]")
        elif btype == "tool_result":
            # Tool output echoed into a user message — not typed input.
            continue
        elif btype in ("thinking", "redacted_thinking", "reasoning"):
            continue
        elif btype == "image":
            parts.append("[image]")
    return "\n\n".join(p for p in (s.strip() for s in parts) if p)


def _is_wrapper_text(text: str) -> bool:
    return bool(_WRAPPER_TAG_RE.match(text.lstrip()))


def _merge_turns(raw_turns: List[Tuple[str, str]]) -> List[Dict[str, str]]:
    """Merge consecutive same-role turns; guarantee strict alternation.

    A leading assistant turn (session began before the log window) gets a
    minimal user stub so the first message is always ``user``; this is the
    only place a stub is ever inserted.
    """
    merged: List[Dict[str, str]] = []
    for role, text in raw_turns:
        text = text.strip()
        if not text:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + text
        else:
            merged.append({"role": role, "content": text})
    if merged and merged[0]["role"] == "assistant":
        merged.insert(
            0,
            {
                "role": "user",
                "content": "(imported conversation begins with an assistant reply)",
            },
        )
    return merged


# ── Claude Code ──────────────────────────────────────────────────────────


def _claude_live_chain(objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the active branch of a Claude Code transcript.

    Claude Code appends to the JSONL forever: a user rewind leaves the
    abandoned branch in the file, and a compact boundary starts a new chain
    segment whose ``parentUuid`` is null and which links back through
    ``logicalParentUuid``.  The live conversation is the parent chain ending
    at the last main-chain record.  Files without uuid linkage (very old
    versions) keep their file order.
    """
    by_uuid: Dict[str, Dict[str, Any]] = {}
    for obj in objects:
        record_uuid = obj.get("uuid")
        if isinstance(record_uuid, str) and record_uuid:
            by_uuid.setdefault(record_uuid, obj)
    if not by_uuid:
        return objects

    tip: Optional[Dict[str, Any]] = None
    for obj in reversed(objects):
        if obj.get("isSidechain"):
            continue
        record_uuid = obj.get("uuid")
        if isinstance(record_uuid, str) and record_uuid:
            tip = obj
            break
    if tip is None:
        return objects

    chain: List[Dict[str, Any]] = []
    seen = set()
    current: Optional[Dict[str, Any]] = tip
    while current is not None:
        record_uuid = current.get("uuid")
        if not isinstance(record_uuid, str) or not record_uuid:
            break
        if record_uuid in seen:
            break
        seen.add(record_uuid)
        chain.append(current)
        parent = current.get("parentUuid")
        if not isinstance(parent, str) or not parent:
            parent = current.get("logicalParentUuid")
        current = by_uuid.get(parent) if isinstance(parent, str) and parent else None
    chain.reverse()
    return chain


def parse_claude_session(path: Path) -> Dict[str, Any]:
    """Parse one Claude Code session JSONL into normalized turns + meta."""
    objects = list(_read_json_lines(path))
    turns: List[Tuple[str, str]] = []
    cwd: Optional[str] = None
    summary: Optional[str] = None
    session_id: Optional[str] = None
    for obj in objects:
        otype = obj.get("type")
        if otype == "summary":
            s = obj.get("summary")
            if isinstance(s, str) and s.strip():
                summary = s.strip()
            continue
        if cwd is None and isinstance(obj.get("cwd"), str):
            cwd = obj["cwd"]
        if session_id is None and isinstance(obj.get("sessionId"), str):
            session_id = obj["sessionId"]

    for obj in _claude_live_chain(objects):
        if obj.get("type") not in ("user", "assistant"):
            continue
        if obj.get("isSidechain") or obj.get("isMeta"):
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _flatten_blocks(message.get("content"), source="claude")
        if not text or (role == "user" and _is_wrapper_text(text)):
            continue
        turns.append((role, text))
    return {
        "turns": _merge_turns(turns),
        "cwd": cwd,
        "title_guess": summary or _first_user_line(turns),
        "session_id": session_id,
    }


def list_claude_sessions(root: Optional[Path] = None) -> List[ForeignSession]:
    """Discover Claude Code sessions under ``~/.claude/projects``."""
    root = Path(root) if root else Path.home() / ".claude" / "projects"
    results: List[ForeignSession] = []
    if not root.is_dir():
        return results
    for jsonl in sorted(root.glob("*/*.jsonl")):
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        parsed = parse_claude_session(jsonl)
        if not parsed["turns"]:
            continue
        results.append(
            ForeignSession(
                source="claude",
                path=jsonl,
                mtime=mtime,
                cwd=parsed["cwd"],
                title_guess=parsed["title_guess"],
                turn_count=len(parsed["turns"]),
                session_id=parsed["session_id"],
            )
        )
    results.sort(key=lambda s: s.mtime, reverse=True)
    return results


# ── Codex CLI ────────────────────────────────────────────────────────────


def parse_codex_session(path: Path) -> Dict[str, Any]:
    """Parse one Codex CLI rollout JSONL into normalized turns + meta."""
    turns: List[Tuple[str, str]] = []
    cwd: Optional[str] = None
    session_id: Optional[str] = None
    for obj in _read_json_lines(path):
        otype = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if otype == "session_meta":
            if isinstance(payload.get("cwd"), str):
                cwd = payload["cwd"]
            sid = payload.get("session_id") or payload.get("id")
            if isinstance(sid, str):
                session_id = sid
            continue
        if otype != "response_item":
            continue
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue  # developer/system payloads never imported
            text = _flatten_blocks(payload.get("content"), source="codex")
            if not text or (role == "user" and _is_wrapper_text(text)):
                continue
            turns.append((role, text))
        elif ptype in ("custom_tool_call", "function_call", "local_shell_call"):
            name = payload.get("name") or payload.get("tool") or "tool"
            # Attach as assistant activity; merged into neighbors later.
            turns.append(("assistant", f"[ran tool: {name}]"))
        # tool outputs / reasoning / web_search etc. are skipped
    return {
        "turns": _merge_turns(turns),
        "cwd": cwd,
        "title_guess": _first_user_line(turns),
        "session_id": session_id,
    }


def list_codex_sessions(root: Optional[Path] = None) -> List[ForeignSession]:
    """Discover Codex CLI rollouts under ``~/.codex/sessions``."""
    root = Path(root) if root else Path.home() / ".codex" / "sessions"
    results: List[ForeignSession] = []
    if not root.is_dir():
        return results
    for jsonl in sorted(root.rglob("rollout-*.jsonl")):
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        parsed = parse_codex_session(jsonl)
        if not parsed["turns"]:
            continue
        results.append(
            ForeignSession(
                source="codex",
                path=jsonl,
                mtime=mtime,
                cwd=parsed["cwd"],
                title_guess=parsed["title_guess"],
                turn_count=len(parsed["turns"]),
                session_id=parsed["session_id"],
            )
        )
    results.sort(key=lambda s: s.mtime, reverse=True)
    return results


# ── opencode ─────────────────────────────────────────────────────────────

_OPENCODE_DB_NAME = "opencode.db"
_OPENCODE_ID_PREFIX = "ses_"


def default_opencode_db() -> Optional[Path]:
    """Locate opencode's SQLite database, or None when it does not exist."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(
        Path.home() / ".local" / "share"
    )
    candidate = Path(data_home) / "opencode" / _OPENCODE_DB_NAME
    return candidate if candidate.is_file() else None


def _open_opencode_db(db_path: Path) -> sqlite3.Connection:
    """Open the opencode store read-only so a live writer is unaffected."""
    uri = "file:" + urllib.request.pathname2url(str(db_path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _opencode_part_text(role: str, parts: List[sqlite3.Row]) -> str:
    """Flatten one opencode message's parts to plain text.

    Only conversational payloads survive: text and file attachments, plus a
    one-line summary for tool activity.  Reasoning, step boundaries, patches,
    and snapshots are machinery the imported transcript does not need.
    """
    segments: List[str] = []
    compacted = False
    for row in parts:
        try:
            data = json.loads(row["data"])
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        ptype = data.get("type")
        if ptype == "text":
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                segments.append(text.strip())
        elif ptype == "file":
            name = data.get("filename") or data.get("url") or "file"
            if isinstance(name, str) and name:
                segments.append(f"[attached file: {name}]")
        elif ptype == "tool" and role == "assistant":
            tool_name = data.get("tool") or "tool"
            state = data.get("state")
            status = state.get("status") if isinstance(state, dict) else None
            if status == "error":
                segments.append(f"[tool failed: {tool_name}]")
            else:
                segments.append(f"[ran tool: {tool_name}]")
        elif ptype == "compaction":
            compacted = True
        # reasoning / step-start / step-finish / patch / snapshot /
        # retry / agent / subtask carry no conversational text.
    if compacted and not segments:
        return "[conversation compacted]" if role == "user" else ""
    return "\n\n".join(segments)


def _opencode_revert_bounds(
    messages: List[sqlite3.Row], revert: Any
) -> Tuple[int, Optional[str]]:
    """Transcript bounds implied by a pending session revert.

    opencode keeps reverted messages on disk until the next prompt deletes
    them; what the user sees (and would keep) is everything before the
    reverted message — or before the reverted part inside it.
    """
    if not isinstance(revert, dict):
        return len(messages), None
    message_id = revert.get("messageID")
    if not isinstance(message_id, str) or not message_id:
        return len(messages), None
    index = -1
    for i, row in enumerate(messages):
        if row["id"] == message_id:
            index = i
            break
    if index < 0:
        return len(messages), None
    part_id = revert.get("partID")
    if isinstance(part_id, str) and part_id:
        return index + 1, part_id
    return index, None


def parse_opencode_session(db_path, session_id: str) -> Dict[str, Any]:
    """Parse one opencode SQLite session into normalized turns + meta."""
    db_path = Path(db_path).expanduser()
    if not db_path.is_file():
        raise ValueError(f"opencode database not found: {db_path}")
    conn = _open_opencode_db(db_path)
    try:
        session = conn.execute(
            "SELECT id, parent_id, directory, title, revert FROM session "
            "WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise ValueError(
                f"opencode session not found in {db_path}: {session_id}"
            )
        messages = conn.execute(
            "SELECT id, data FROM message WHERE session_id = ? "
            "ORDER BY time_created, id",
            (session_id,),
        ).fetchall()

        revert = None
        if session["revert"]:
            try:
                revert = json.loads(session["revert"])
            except (json.JSONDecodeError, ValueError, TypeError):
                revert = None
        keep_count, part_cut_id = _opencode_revert_bounds(messages, revert)

        turns: List[Tuple[str, str]] = []
        for position, row in enumerate(messages[:keep_count]):
            try:
                data = json.loads(row["data"])
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            role = data.get("role")
            if role not in ("user", "assistant"):
                continue
            parts = conn.execute(
                "SELECT id, data FROM part WHERE message_id = ? ORDER BY id",
                (row["id"],),
            ).fetchall()
            if part_cut_id is not None and position == keep_count - 1:
                parts = [p for p in parts if p["id"] < part_cut_id]
            text = _opencode_part_text(role, parts)
            if not text or (role == "user" and _is_wrapper_text(text)):
                continue
            turns.append((role, text))

        title = session["title"] if isinstance(session["title"], str) else ""
        return {
            "turns": _merge_turns(turns),
            "cwd": session["directory"],
            "title_guess": title.strip() or _first_user_line(turns),
            "session_id": session_id,
        }
    finally:
        conn.close()


def list_opencode_sessions(
    db_path=None, limit: int = 25
) -> List[ForeignSession]:
    """List root opencode sessions (newest first) from the given database."""
    path = Path(db_path).expanduser() if db_path else default_opencode_db()
    results: List[ForeignSession] = []
    if path is None or not Path(path).is_file():
        return results
    conn = _open_opencode_db(Path(path))
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.directory, s.title, s.time_created, s.time_updated,
                   (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id)
                       AS message_count
            FROM session s
            WHERE s.parent_id IS NULL
            ORDER BY s.time_created DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    for row in rows:
        title = row["title"] if isinstance(row["title"], str) else ""
        updated_ms = row["time_updated"] or row["time_created"] or 0
        results.append(
            ForeignSession(
                source="opencode",
                path=Path(path),
                mtime=float(updated_ms) / 1000.0,
                cwd=row["directory"],
                title_guess=title.strip() or None,
                turn_count=int(row["message_count"] or 0),
                session_id=row["id"],
            )
        )
    return results


def _first_user_line(turns: List[Tuple[str, str]]) -> Optional[str]:
    for role, text in turns:
        if role == "user":
            line = text.strip().splitlines()[0].strip()
            if line:
                return line[:_TITLE_MAX * 2]
    return None


# ── Import ───────────────────────────────────────────────────────────────

_SOURCE_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex CLI",
    "opencode": "opencode",
}
_SOURCE_DB_NAMES = {
    "claude": "claude-code",
    "codex": "codex-cli",
    "opencode": "opencode",
}


def import_foreign_session(
    source: str, path, db=None, foreign_id: Optional[str] = None
) -> str:
    """Import one foreign session into the Son of Anton SessionDB.

    Returns the new Son of Anton session id.  The foreign store is only read.
    For ``opencode``, ``path`` is the SQLite database and ``foreign_id`` is
    the ``ses_...`` row to import; for the JSONL sources ``foreign_id`` is
    unused.  Raises ``ValueError`` on unknown source, missing store, or a
    session with no usable conversation turns.
    """
    source = (source or "").strip().lower().lstrip("@")
    if source not in _SOURCE_LABELS:
        raise ValueError(f"Unknown foreign session source: {source!r}")
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError(f"Session file not found: {path}")

    if source == "claude":
        parsed = parse_claude_session(path)
    elif source == "codex":
        parsed = parse_codex_session(path)
    else:
        if not foreign_id:
            raise ValueError(
                "opencode import requires a session id (ses_...)"
            )
        parsed = parse_opencode_session(path, foreign_id)
    turns = parsed["turns"]
    if not turns:
        raise ValueError(
            f"No user/assistant conversation turns found in {path}"
        )

    label = _SOURCE_LABELS[source]
    first_user = _first_user_line(
        [(t["role"], t["content"]) for t in turns]
    ) or path.stem
    title_seed = (parsed.get("title_guess") or "").strip()
    title_seed = title_seed.splitlines()[0].strip() if title_seed else ""
    if not title_seed:
        title_seed = first_user
    if len(title_seed) > _TITLE_MAX:
        title_seed = title_seed[: _TITLE_MAX - 1] + "…"
    title = f"Imported from {label}: {title_seed}"

    owns_db = db is None
    if owns_db:
        from son_of_anton_state import SessionDB

        db = SessionDB()
    try:
        session_id = (
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        )
        origin = {
            "imported_from": {
                "tool": _SOURCE_DB_NAMES[source],
                "path": str(path),
                "foreign_session_id": foreign_id or parsed.get("session_id"),
            }
        }
        db.create_session(
            session_id,
            source=_SOURCE_DB_NAMES[source],
            cwd=parsed.get("cwd"),
            origin_json=json.dumps(origin),
        )
        for turn in turns:
            db.append_message(session_id, turn["role"], turn["content"])
        try:
            db.set_session_title(session_id, title)
        except Exception:
            pass  # title is cosmetic; the import itself succeeded
        return session_id
    finally:
        if owns_db:
            try:
                db.close()
            except Exception:
                pass


# ── Picker / CLI helpers ─────────────────────────────────────────────────


def gather_foreign_sessions(
    source: Optional[str] = None,
    *,
    claude_root: Optional[Path] = None,
    codex_root: Optional[Path] = None,
    opencode_db: Optional[Path] = None,
    limit: int = 25,
) -> List[ForeignSession]:
    """List foreign sessions across sources, newest first."""
    sessions: List[ForeignSession] = []
    if source in (None, "claude"):
        sessions.extend(list_claude_sessions(claude_root))
    if source in (None, "codex"):
        sessions.extend(list_codex_sessions(codex_root))
    if source in (None, "opencode"):
        sessions.extend(list_opencode_sessions(opencode_db, limit=limit))
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions[:limit] if limit else sessions


def pick_foreign_session(
    source: Optional[str] = None,
    *,
    opencode_db: Optional[Path] = None,
    limit: int = 25,
) -> Optional[ForeignSession]:
    """Interactive numbered picker. Returns None when nothing was chosen."""
    import sys

    sessions = gather_foreign_sessions(
        source, opencode_db=opencode_db, limit=limit
    )
    if not sessions:
        where = _SOURCE_LABELS.get(
            source or "", "Claude Code, Codex CLI, or opencode"
        )
        print(f"No {where} sessions found on this machine.")
        return None
    print("Foreign sessions (newest first):")
    for i, s in enumerate(sessions, 1):
        when = datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M")
        ws = ""
        if s.cwd:
            ws = f"  ({os.path.basename(s.cwd.rstrip('/')) or s.cwd})"
        print(f"  {i:>2}. {when}  {s.label}{ws}  [{s.turn_count} turns]")
    if not sys.stdin.isatty():
        print(
            "Non-interactive terminal — import by id or path instead:\n"
            "  son-of-anton sessions import --from claude|codex <path>\n"
            "  son-of-anton sessions import --from opencode --session ses_..."
        )
        return None
    try:
        raw = input(f"Import which session? [1-{len(sessions)}, empty to cancel] ")
    except (EOFError, KeyboardInterrupt):
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        print(f"Not a number: {raw}")
        return None
    if not 1 <= idx <= len(sessions):
        print(f"Out of range: {idx}")
        return None
    return sessions[idx - 1]


def run_sessions_import(args, db=None) -> Optional[str]:
    """`son-of-anton sessions import` entry point. Returns new session id or None."""
    source = getattr(args, "from_source", None)
    path = getattr(args, "path", None)
    requested_id = getattr(args, "foreign_session_id", None)
    opencode_db = getattr(args, "opencode_db", None)

    chosen_path = None
    chosen_foreign_id = requested_id

    if source == "opencode":
        db_path = (
            Path(opencode_db).expanduser()
            if opencode_db
            else default_opencode_db()
        )
        if db_path is None or not db_path.is_file():
            print(
                "No opencode database found. Pass --db /path/to/opencode.db, "
                "or import a JSONL session with --from claude|codex."
            )
            return None
        if path:
            path = Path(path).expanduser()
            if str(path).startswith(_OPENCODE_ID_PREFIX):
                chosen_foreign_id = chosen_foreign_id or str(path)
            elif path.is_file():
                db_path = path  # positional database override
            else:
                print(
                    f"Error: not an opencode session id or database: {path}"
                )
                return None
        if chosen_foreign_id:
            chosen_path = db_path
        else:
            picked = pick_foreign_session("opencode", opencode_db=db_path)
            if picked is None:
                return None
            chosen_path, chosen_foreign_id = picked.path, picked.session_id
    elif path:
        # Report a missing file distinctly instead of the misleading
        # "cannot infer source" (SES-10).
        if not Path(path).exists():
            print(f"Error: file not found: {path}")
            return None
        if not source:
            # Guess from the path shape.
            p = str(path)
            if "/.claude/" in p or p.endswith(".jsonl") and "claude" in p:
                source = "claude"
            if "/.codex/" in p or Path(p).name.startswith("rollout-"):
                source = "codex"
        if not source:
            print(
                "Cannot infer source from path; "
                "pass --from claude|codex|opencode."
            )
            return None
        chosen_path = Path(path)
    else:
        picked = pick_foreign_session(
            source,
            opencode_db=Path(opencode_db) if opencode_db else None,
        )
        if picked is None:
            return None
        source, chosen_path = picked.source, picked.path
        chosen_foreign_id = picked.session_id

    try:
        session_id = import_foreign_session(
            source, chosen_path, db=db, foreign_id=chosen_foreign_id
        )
    except ValueError as e:
        print(f"Error: {e}")
        return None
    label = _SOURCE_LABELS.get(source, source)
    print(f"✓ Imported {label} session as {session_id}")

    if getattr(args, "analyze", False):
        from son_of_anton_cli import session_analysis

        print("  Analyzing the transcript for memories and skills ...")
        status = session_analysis.run_session_analysis(
            session_id, source_label=label, db=db
        )
        if status == 0:
            print("  Analysis complete.")
        else:
            print("  Analysis did not complete; the import itself is intact.")
            print(f"  Retry with:  son-of-anton sessions analyze {session_id}")

    print(f"  Continue it with:  son-of-anton --resume {session_id}")
    return session_id
