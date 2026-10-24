#!/usr/bin/env python3
"""
Session Search Tool - Long-Term Conversation Recall

Single-shape tool with four calling modes (inferred from args, no explicit
mode parameter):

  1. DISCOVERY — pass ``query``. Runs FTS5 and dedupes hits by session lineage.
     Adaptive detail (the default) fully hydrates the top result with a ±5
     message window and bookends, while lower-ranked results keep the exact
     anchor message plus metadata. Pass ``detail="full"`` to fully hydrate
     every result. Zero LLM cost.

  2. SCROLL — pass ``session_id`` + ``around_message_id``. Returns a window
     of ±window messages centered on the anchor, no FTS5, no bookends. To
     scroll forward / backward, re-anchor on the last / first message id of
     the returned window.

  3. READ — pass ``session_id`` without an anchor. Returns the whole session,
     or a bounded head/tail view for large sessions.

  4. BROWSE — no args. Returns recent sessions chronologically (titles,
     previews, timestamps).

All four modes operate on the SQLite session DB via the FTS5 index and
the get_anchored_view / get_messages_around primitives in son_of_anton_state.
No LLM calls anywhere — every shape returns actual messages from the DB.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Union

from son_of_anton_state_common import _RESET_END_REASONS

# Sources excluded from session browsing/searching: delegate subagent runs
# ("subagent"), integrations run with `--source tool`, and "kanban" rows older
# installs wrote — none belongs in the user's session history.
_HIDDEN_SESSION_SOURCES = ("kanban", "subagent", "tool")

# Automation sources that are kept searchable but DEMOTED below interactive
# sessions in discover ranking. Cron jobs run on a schedule and accumulate
# large volumes of repetitive vocabulary (recurring project names, dates,
# "session", summaries); under bare BM25 they dominate the top-N FTS rows and
# starve out the user's own interactive sessions, producing "recall blindness"
# where only cron sessions surface. Demoting — not excluding — keeps
# cron content reachable when it's the only match, while interactive sessions
# always win when both match.
_DEMOTED_SESSION_SOURCES = ("cron",)

# How many FTS rows discover scans before dedup-by-lineage. The interactive
# vs automation split below only helps if enough rows are in hand to find
# interactive matches buried under a wall of cron hits, so this is well above
# the handful of distinct sessions a typical query returns.
_DISCOVER_SCAN_LIMIT = 300

# Raw FTS rows are only a discovery-plan input. The final response hydrates
# its own anchored message window and bookends after lineage deduplication.
_DISCOVER_SEARCH_FIELDS = (
    "id",
    "session_id",
    "role",
    "snippet",
    "source",
    "model",
    "session_started",
)

# Prefixes that identify generated context-compaction handoff summaries.
# These are inserted by compaction as normal user/assistant messages but
# contain machine-generated summary metadata — not user content. They must be
# excluded from discovery bookends to avoid re-introducing huge compaction
# payloads into fresh sessions via session_search.
_COMPACTION_PREFIXES = (
    "[CONTEXT COMPACTION",
    "[CONTEXT SUMMARY]:",
)

# Gateway /new, /reset, idle/daily expiry, and CLI /new end the predecessor
# without carrying its transcript into the child. Those children share a
# parent_session_id lineage with the current session, but the prior content
# is NOT in live context — unlike compaction continuations (summary carried
# forward) and live delegation children (parent still running).
#
# Derived from the canonical gateway reset-reason set so the recovery fence
# and this tool cannot drift (see the comment on _RESET_END_REASONS).
# "new_session" is the CLI /new end reason (cli.py), which the gateway set
# does not include.
_FRESH_RESET_END_REASONS = frozenset(_RESET_END_REASONS) | {"new_session"}


def _format_timestamp(ts: Union[int, float, str, None]) -> str:
    """Convert a Unix timestamp (float/int) or ISO string to a human-readable date.

    Returns "unknown" for None, str(ts) if conversion fails.
    """
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, (int, float)):
            from datetime import datetime
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%B %d, %Y at %I:%M %p")
        if isinstance(ts, str):
            if ts.replace(".", "").replace("-", "").isdigit():
                from datetime import datetime
                dt = datetime.fromtimestamp(float(ts))
                return dt.strftime("%B %d, %Y at %I:%M %p")
            return ts
    except (ValueError, OSError, OverflowError) as e:
        logging.debug("Failed to format timestamp %s: %s", ts, e, exc_info=True)
    except Exception as e:
        logging.debug("Unexpected error formatting timestamp %s: %s", ts, e, exc_info=True)
    return str(ts)


def _is_compaction_summary(content: str) -> bool:
    """Return True if *content* looks like a generated compaction handoff."""
    if not content:
        return False
    stripped = content.lstrip()
    return any(stripped.startswith(p) for p in _COMPACTION_PREFIXES)


def _resolve_to_parent(db, session_id: str) -> tuple[str, bool]:
    """Walk parent_session_id chain to the lineage root.

    Returns ``(root_id, has_compaction_hop)`` where ``has_compaction_hop`` is
    True if any session along the chain ended with ``end_reason = 'compaction'``
    — i.e. at least one parent/ancestor was compaction-rotated into this
    lineage. That flag lets callers distinguish a compaction-split lineage
    (parent content summarised away, no longer in live context) from a
    delegation lineage (child content still visible to the parent agent).

    Falls back to ``(session_id, False)`` on errors.
    """
    if not session_id:
        return session_id, False
    visited: set[str] = set()
    cur = session_id
    has_compaction = False
    while cur and cur not in visited:
        visited.add(cur)
        try:
            s = db.get_session(cur)
            if not s:
                break
            if s.get("end_reason") == "compaction":
                has_compaction = True
            parent = s.get("parent_session_id")
            if not parent:
                break
            cur = parent
        except Exception as e:
            logging.debug("Error resolving parent for %s: %s", cur, e, exc_info=True)
            break
    return cur, has_compaction


def _resolve_lineage(db, session_id: str) -> str:
    """Convenience: return only the lineage root (ignores compaction hop)."""
    return _resolve_to_parent(db, session_id)[0]


def _session_end_reason(db, session_id: str) -> Optional[str]:
    """Return the session's ``end_reason``, or None if missing/unended/error."""
    if not session_id:
        return None
    try:
        s = db.get_session(session_id)
        if not s:
            return None
        return s.get("end_reason") or None
    except Exception:
        return None


def _session_left_live_context(db, session_id: str) -> bool:
    """True when *session_id*'s transcript is no longer in anyone's live context.

    Two shapes qualify:

    - ``compaction``: the transcript was summarised into the continuation
      child, so the original rows left live context.
    - fresh resets (:data:`_FRESH_RESET_END_REASONS`): every
      ``_RESET_END_REASONS`` member plus CLI ``new_session`` — the child
      starts empty and carries nothing forward.

    Everything else stays excluded from same-lineage recall: live delegation
    children (``end_reason is None``) are still visible to the parent agent,
    and ``branched`` parents were verbatim-copied into the branch child, so
    their content IS the current context.
    """
    end_reason = _session_end_reason(db, session_id)
    return end_reason == "compaction" or _is_fresh_reset_session(end_reason)


def _is_fresh_reset_session(end_reason: Optional[str]) -> bool:
    """True when *end_reason* is a /new-style reset (transcript not carried forward)."""
    return end_reason in _FRESH_RESET_END_REASONS


def _get_message_storage_state(db, message_id) -> Optional[Dict[str, Any]]:
    """Return the owning session and visibility flags for *message_id*."""
    if not message_id:
        return None
    try:
        with db._lock:
            cursor = db._conn.execute(
                "SELECT session_id, active, compacted FROM messages WHERE id = ?",
                (message_id,),
            )
            row = cursor.fetchone()
    except Exception:
        logging.debug(
            "message storage-state lookup failed for %s", message_id, exc_info=True
        )
        return None
    return dict(row) if row is not None else None


def _is_compacted_message(db, message_id) -> bool:
    """Return True if *message_id* is a compaction-archived row.

    Compaction archives are ``active=0, compacted=1`` — the content was
    summarised away from live context by :meth:`archive_and_compact`.
    Rewind/undo rows are ``active=0, compacted=0`` and must stay hidden.

    Used by ``_discover`` to distinguish a compaction-archived FTS hit on the
    current session (pre-compaction content no longer in live context — should
    stay discoverable) from an active live hit (already in context — skip).
    Returns False on any error so the caller falls back to the safe default
    (skip the current session).
    """
    state = _get_message_storage_state(db, message_id)
    return state is not None and state["active"] == 0 and state["compacted"] == 1


def _annotate_rebuild_status(db, payload: Dict[str, Any]) -> None:
    """Add a rebuild-progress note when the deferred FTS backfill (schema
    v23) is still running, so the agent can tell the user why older results
    may be incomplete/slower instead of treating a thin result set as
    ground truth. No-op (and never raises) when no rebuild is pending."""
    try:
        status = db.fts_rebuild_status()
    except Exception:
        return
    if status is None:
        return
    payload["index_rebuild"] = {
        "percent": status["percent"],
        "note": (
            f"The search index is rebuilding in the background "
            f"({status['percent']}% done, {status['indexed']:,} of "
            f"{status['total']:,} messages). Results from older messages "
            f"may be incomplete until it finishes."
        ),
    }


def _order_for_recall(raw_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stable-sort FTS rows so interactive sessions rank above automation.

    Within each class (interactive vs demoted) the original BM25 ``rank``
    order is preserved — Python's sort is stable, and rows arrive already
    ranked by relevance. This only changes cross-class ordering: a cron hit
    never displaces an interactive hit during lineage dedup, so the user's
    own conversations surface first even when cron rows out-rank them under
    bare BM25. Demoted rows still appear when they're the only matches.
    """
    return sorted(
        raw_results,
        key=lambda r: 1 if (r.get("source") or "") in _DEMOTED_SESSION_SOURCES else 0,
    )


def _shape_message(
    m: Dict[str, Any],
    anchor_id: Optional[int] = None,
    max_content_len: Optional[int] = None,
) -> Dict[str, Any]:
    """Slim a message row for the tool response. Keeps content even if empty.

    When *max_content_len* is set, ``content`` is truncated to that many
    characters and ``content_truncated`` / ``original_content_chars`` metadata
    is added so callers know the payload was bounded.
    """
    raw_content = m.get("content")
    if isinstance(raw_content, str) and "\x1b" in raw_content:
        # Recalled messages can carry ANSI escape sequences (e.g. archived
        # terminal output). Strip them before returning content to the model.
        from tools.ansi_strip import strip_ansi

        raw_content = strip_ansi(raw_content)
    if max_content_len and raw_content and len(raw_content) > max_content_len:
        content = raw_content[:max_content_len] + "…"
        truncated = True
        original_chars = len(raw_content)
    else:
        content = raw_content
        truncated = False
        original_chars = None
    entry = {
        "id": m.get("id"),
        "role": m.get("role"),
        "content": content,
        "timestamp": m.get("timestamp"),
    }
    if m.get("tool_name"):
        entry["tool_name"] = m.get("tool_name")
    if m.get("tool_calls"):
        entry["tool_calls"] = m.get("tool_calls")
    if m.get("tool_call_id"):
        entry["tool_call_id"] = m.get("tool_call_id")
    if anchor_id is not None and m.get("id") == anchor_id:
        entry["anchor"] = True
    if truncated:
        entry["content_truncated"] = True
        entry["original_content_chars"] = original_chars
    # Strip None values to keep payload tight, but always keep content
    # (absent content is meaningful — tool-call-only assistant turns).
    return {k: v for k, v in entry.items() if v is not None or k in ("content",)}


def _session_link(session_id: str, profile: str = None) -> str:
    """The ``@session:[<profile>/]<id>`` reference the agent writes to point at a session.

    The profile segment is only a label: every session lives in this home's
    ``state.db``, so a link resolves against the caller's db either way.
    """
    name = (profile or "").strip()
    return f"@session:{name}/{session_id}" if name else f"@session:{session_id}"


def _read_session(db, session_id: str, head: int = 20, tail: int = 10, link_profile: str = None) -> str:
    """Read shape: dump a whole session by id (head + tail when large).

    Serves the linked-session case — the user dropped an @session reference and
    the agent wants the transcript. Bounded payload: small sessions return in
    full, large ones return the first ``head`` and last ``tail`` messages with a
    pointer to scroll the middle.
    """
    try:
        meta = db.get_session(session_id) or {}
    except Exception as e:
        logging.debug("get_session failed for %s: %s", session_id, e, exc_info=True)
        meta = {}
    if not meta:
        return tool_error(f"session_id not found: {session_id}", success=False)

    try:
        rows = db.get_messages(session_id)
    except Exception as e:
        logging.error("get_messages failed for %s: %s", session_id, e, exc_info=True)
        return tool_error(f"failed to load session: {e}", success=False)

    shaped = [_shape_message(m) for m in rows]
    total = len(shaped)
    truncated = total > head + tail
    window = shaped[:head] + shaped[-tail:] if truncated else shaped

    response = {
        "success": True,
        "mode": "read",
        "session_id": session_id,
        "link": _session_link(session_id, link_profile),
        "session_meta": {
            "when": _format_timestamp(meta.get("started_at")),
            "source": meta.get("source"),
            "model": meta.get("model"),
            "title": meta.get("title"),
        },
        "message_count": total,
        "truncated": truncated,
        "messages": window,
    }
    if truncated:
        response["message"] = (
            f"Session has {total} messages; showing first {head} + last {tail}. "
            "Pass around_message_id (any id above) to scroll the middle."
        )
    return json.dumps(response, ensure_ascii=False)


def _list_recent_sessions(
    db, limit: int, current_session_id: str = None, link_profile: str = None,
    sources: Optional[List[str]] = None,
) -> str:
    """Return metadata for the most recent sessions (no LLM calls, no FTS5)."""
    try:
        # list_sessions_rich (include_children=False) already applies the
        # canonical child classifier (_LISTABLE_CHILD_SQL): roots, /branch
        # children, and /new-reset children are admitted, while
        # delegation/compaction children are hidden — trust the query.
        sessions = db.list_sessions_rich(
            limit=limit + 15,
            exclude_sources=list(_HIDDEN_SESSION_SOURCES),
            sources=list(sources) if sources else None,
            order_by_last_active=True,
        )  # fetch extra so we can skip current / compaction roots

        current_root, has_compaction_hop = (
            _resolve_to_parent(db, current_session_id)
            if current_session_id else (None, False)
        )

        results = []
        for s in sessions:
            sid = s.get("id", "")
            if sid == current_session_id:
                continue
            # Compaction continuation: the root's original turns were
            # summarised into the live child, so hide the root. /new-reset
            # children share a lineage root but carry no transcript — keep
            # that root browsable.
            if has_compaction_hop and current_root and sid == current_root:
                continue
            results.append({
                "session_id": sid,
                "link": _session_link(sid, link_profile),
                "title": s.get("title") or None,
                "source": s.get("source", ""),
                "started_at": s.get("started_at", ""),
                "last_active": s.get("last_active", ""),
                "message_count": s.get("message_count", 0),
                "preview": s.get("preview", ""),
            })
            if len(results) >= limit:
                break

        return json.dumps({
            "success": True,
            "mode": "browse",
            "results": results,
            "count": len(results),
            "message": f"Showing {len(results)} most recent sessions. Pass a query= to search, or session_id+around_message_id to scroll.",
        }, ensure_ascii=False)
    except Exception as e:
        logging.error("Error listing recent sessions: %s", e, exc_info=True)
        return tool_error(f"Failed to list recent sessions: {e}", success=False)


def _scroll(
    db,
    session_id: str,
    around_message_id: int,
    window: int = 5,
    current_session_id: str = None,
) -> str:
    """Scroll shape: return a window of messages centered on an anchor.

    No FTS5, no bookends — just the slice. The discovery shape's lineage
    fixup is preserved: if the anchor doesn't live in the named session
    but does live in a child session in the same lineage, rebind silently.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        return tool_error("scroll requires session_id", success=False)
    session_id = session_id.strip()

    try:
        around_message_id = int(around_message_id)
    except (TypeError, ValueError):
        return tool_error("scroll requires integer around_message_id", success=False)

    # Window clamp [1, 20]
    if not isinstance(window, int):
        try:
            window = int(window)
        except (TypeError, ValueError):
            window = 5
    window = max(1, min(window, 20))

    # Locate the anchor before applying the current-lineage guard. Discovery
    # intentionally surfaces same-lineage history that is no longer in live
    # context: in-place compacted rows, compaction-ended parents, and
    # /new-reset predecessors. Scroll must preserve that distinction instead
    # of rejecting the discovery result it just returned.
    anchor_state = _get_message_storage_state(db, around_message_id)
    owning_session_id = (
        anchor_state.get("session_id") if anchor_state is not None else None
    )

    if current_session_id:
        anchor_session_id = owning_session_id or session_id
        a_root = _resolve_lineage(db, anchor_session_id)
        c_root = _resolve_lineage(db, current_session_id)
        if a_root and c_root and a_root == c_root:
            is_compacted_anchor = (
                anchor_state is not None
                and anchor_state["active"] == 0
                and anchor_state["compacted"] == 1
            )
            is_inactive_non_compacted_anchor = (
                anchor_state is not None
                and anchor_state["active"] == 0
                and anchor_state["compacted"] != 1
            )
            is_out_of_context_history = (
                not is_inactive_non_compacted_anchor
                and _session_left_live_context(db, anchor_session_id)
            )
            if not (is_compacted_anchor or is_out_of_context_history):
                return tool_error(
                    "scroll rejected: anchor lives in the current session lineage (already in your active context)",
                    success=False,
                )

    # Session existence check
    try:
        session_meta = db.get_session(session_id) or {}
    except Exception as e:
        logging.debug("get_session failed for %s: %s", session_id, e, exc_info=True)
        session_meta = {}
    if not session_meta:
        return tool_error(f"session_id not found: {session_id}", success=False)

    # Fetch the window
    try:
        view = db.get_messages_around(session_id, around_message_id, window=window)
    except Exception as e:
        logging.error("get_messages_around failed: %s", e, exc_info=True)
        return tool_error(f"failed to load messages: {e}", success=False)

    messages = view.get("window") or []

    # Lineage rebind: caller may have paired a parent session_id with a
    # message id that lives in a descendant (compaction / delegation creates
    # child sessions). Locate the real owning session and refetch.
    rebind_warning = None
    if not messages:
        owning = owning_session_id
        if owning and owning != session_id:
            a_root = _resolve_lineage(db, session_id)
            o_root = _resolve_lineage(db, owning)
            if a_root and o_root and a_root == o_root:
                try:
                    rebind_view = db.get_messages_around(owning, around_message_id, window=window)
                    messages = rebind_view.get("window") or []
                    if messages:
                        view = rebind_view
                        rebind_warning = (
                            f"around_message_id {around_message_id} lives in {owning} "
                            f"(child of {session_id}); rebound transparently"
                        )
                        try:
                            session_meta = db.get_session(owning) or session_meta
                        except Exception:
                            pass
                        session_id = owning
                except Exception as e:
                    logging.debug("rebind get_messages_around failed: %s", e, exc_info=True)

    if not messages:
        return tool_error(
            f"around_message_id {around_message_id} not in session_id {session_id}",
            success=False,
        )

    response = {
        "success": True,
        "mode": "scroll",
        "session_id": session_id,
        "around_message_id": around_message_id,
        "session_meta": {
            "when": _format_timestamp(session_meta.get("started_at")),
            "source": session_meta.get("source"),
            "model": session_meta.get("model"),
            "title": session_meta.get("title"),
        },
        "window": window,
        "messages": [_shape_message(m, anchor_id=around_message_id) for m in messages],
        "messages_before": view.get("messages_before", 0),
        "messages_after": view.get("messages_after", 0),
    }
    if rebind_warning:
        response["warning"] = rebind_warning
    return json.dumps(response, ensure_ascii=False)


def _normalize_title_query(query: str) -> str:
    """Strip common quoting the model may include around a remembered title."""
    return query.strip().strip("`'\"")


def _title_match_result(
    db,
    query: str,
    current_lineage_root: Optional[str],
    sources: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Return a discovery-shaped result when the query matches a session title."""
    title_query = _normalize_title_query(query)
    if not title_query:
        return None

    try:
        session_id = db.resolve_session_by_title(title_query)
    except Exception:
        logging.debug("resolve_session_by_title failed for %r", title_query, exc_info=True)
        return None
    if not session_id:
        return None

    lineage_root = _resolve_lineage(db, session_id)
    if current_lineage_root and lineage_root == current_lineage_root:
        # Same-lineage title hits are in-context only when the session is
        # still live. /new-reset and compaction-ended parents are not.
        if not _session_left_live_context(db, session_id):
            return None

    try:
        session_meta = db.get_session(lineage_root) or db.get_session(session_id) or {}
    except Exception:
        logging.debug("get_session failed for title match %s", session_id, exc_info=True)
        session_meta = {}
    if session_meta.get("source") in _HIDDEN_SESSION_SOURCES:
        return None
    if sources and session_meta.get("source") not in sources:
        return None

    try:
        messages = db.get_messages(session_id)
    except Exception:
        logging.debug("get_messages failed for title match %s", session_id, exc_info=True)
        messages = []

    anchor_id = messages[0].get("id") if messages else None
    if anchor_id is not None:
        try:
            view = db.get_anchored_view(session_id, anchor_id, window=5, bookend=3)
        except Exception:
            logging.debug("get_anchored_view failed for title match %s/%s", session_id, anchor_id, exc_info=True)
            view = {}
    else:
        view = {}

    entry = {
        "session_id": session_id,
        "when": _format_timestamp(session_meta.get("started_at")),
        "source": session_meta.get("source", "unknown"),
        "model": session_meta.get("model") or "unknown",
        "title": session_meta.get("title") or title_query,
        "matched_role": "session_title",
        "match_message_id": anchor_id,
        "snippet": f"Session title matched: {session_meta.get('title') or title_query}",
        "bookend_start": [_shape_message(m) for m in (view.get("bookend_start") or messages[:3])],
        "messages": [_shape_message(m, anchor_id=anchor_id) for m in (view.get("window") or messages[:5])],
        "bookend_end": [_shape_message(m) for m in (view.get("bookend_end") or messages[-3:])],
        "messages_before": view.get("messages_before", 0),
        "messages_after": view.get("messages_after", max(len(messages) - 5, 0)),
        "detail": "full",
        "_lineage_root": lineage_root,
    }
    if lineage_root and lineage_root != session_id:
        entry["parent_session_id"] = lineage_root
    return entry


def _discover(
    db,
    query: str,
    role_filter: Optional[List[str]],
    limit: int,
    sort: Optional[str],
    detail: str,
    current_session_id: str = None,
    link_profile: str = None,
    sources: Optional[List[str]] = None,
) -> str:
    """Discovery shape: FTS5 plus adaptive or full result hydration."""
    role_list = role_filter if role_filter else ["user", "assistant"]
    source_filter = list(sources) if sources else None
    current_lineage_root = _resolve_lineage(db, current_session_id) if current_session_id else None
    title_result = _title_match_result(db, query, current_lineage_root, sources=source_filter)

    try:
        raw_results = db.search_messages(
            query=query,
            role_filter=role_list,
            exclude_sources=list(_HIDDEN_SESSION_SOURCES),
            source_filter=source_filter,
            limit=_DISCOVER_SCAN_LIMIT,  # widen so dedup-by-lineage can find
            # distinct sessions AND so interactive matches buried under a wall
            # of cron rows are still in hand for the demotion pass below.
            offset=0,
            sort=sort,
            fields=_DISCOVER_SEARCH_FIELDS,
        )
    except Exception as e:
        logging.error("FTS5 search failed: %s", e, exc_info=True)
        return tool_error(f"Search failed: {e}", success=False)

    # Demote automation (cron) rows below interactive ones before dedup, so a
    # high-volume cron corpus can't starve the user's own sessions out of the
    # top `limit` results. Stable — preserves BM25/recency order
    # within each class.
    raw_results = _order_for_recall(raw_results)

    if not raw_results and not title_result:
        _empty_payload = {
            "success": True,
            "mode": "discover",
            "query": query,
            "detail": detail,
            "results": [],
            "count": 0,
            "message": "No matching sessions found.",
        }
        _annotate_rebuild_status(db, _empty_payload)
        return json.dumps(_empty_payload, ensure_ascii=False)

    # Dedupe by lineage. Keep the raw owning session_id on the surviving
    # row — only that pairs validly with the FTS5 match id for the anchored
    # window. parent_session_id is exposed separately when different.
    seen_sessions = {}
    results = []

    if title_result:
        title_lineage = title_result.pop("_lineage_root", None)
        if title_lineage:
            seen_sessions[title_lineage] = {"_title_only": True}
        results.append(title_result)

    for r in raw_results:
        if len(seen_sessions) >= limit:
            break
        raw_sid = r["session_id"]
        resolved_sid, _ = _resolve_to_parent(db, raw_sid)
        # Skip the current session lineage — UNLESS the hit's transcript has
        # left live context. Three sub-cases:
        #
        # Legacy compaction rotation: the FTS hit lives in a session that
        # itself ended with end_reason='compaction'. That session's content
        # has been replaced by a summary in the continuation child, so it
        # must stay discoverable.
        #
        # /new-reset (and idle/daily/CLI new_session): the predecessor was
        # ended without carrying any transcript into the child. Same lineage
        # root, but the prior conversation is NOT in the active context —
        # hiding it made gateway recall go blind after every /new.
        # A live delegation child has end_reason=None, so it stays excluded.
        #
        # In-place compaction: the FTS hit lives on the SAME session_id as the
        # current session, but the matched message row is an archived
        # (active=0, compacted=1) row. The live-context load filters active=1,
        # so that content is no longer in context — let it through.
        is_compacted_hit = _is_compacted_message(db, r.get("id"))
        is_ended_session = _session_left_live_context(db, raw_sid)
        if current_lineage_root and resolved_sid == current_lineage_root:
            if not (is_ended_session or is_compacted_hit):
                continue
        if current_session_id and raw_sid == current_session_id:
            # Same-session hit: only skip if the matched message is still live
            # (active=1). Archived/compacted rows are pre-compaction content
            # that's been summarised away — let them through.
            if not is_compacted_hit:
                continue
        if resolved_sid not in seen_sessions:
            row = dict(r)
            row["_lineage_root"] = resolved_sid
            seen_sessions[resolved_sid] = row
        if len(seen_sessions) >= limit:
            break

    for lineage_root, match_info in seen_sessions.items():
        if match_info.get("_title_only"):
            continue
        hit_sid = match_info.get("session_id") or lineage_root
        msg_id = match_info.get("id")
        try:
            view = db.get_anchored_view(hit_sid, msg_id, window=5, bookend=3)
        except Exception as e:
            logging.warning("get_anchored_view failed for %s/%s: %s", hit_sid, msg_id, e, exc_info=True)
            continue

        try:
            session_meta = db.get_session(lineage_root) or {}
        except Exception:
            session_meta = {}

        result_detail = "full" if detail == "full" or not results else "compact"
        window_messages = view.get("window") or []
        if result_detail == "compact":
            window_messages = [m for m in window_messages if m.get("id") == msg_id]

        entry = {
            "session_id": hit_sid,
            "when": _format_timestamp(
                session_meta.get("started_at") or match_info.get("session_started")
            ),
            "source": session_meta.get("source") or match_info.get("source", "unknown"),
            "model": session_meta.get("model") or match_info.get("model") or "unknown",
            "title": session_meta.get("title") or None,
            "matched_role": match_info.get("role"),
            "match_message_id": msg_id,
            "snippet": match_info.get("snippet") or "",
            "bookend_start": (
                [
                    _shape_message(m, max_content_len=1200)
                    for m in (view.get("bookend_start") or [])
                    if not _is_compaction_summary(m.get("content", ""))
                ]
                if result_detail == "full"
                else []
            ),
            "messages": [
                _shape_message(m, anchor_id=msg_id, max_content_len=4000)
                for m in window_messages
            ],
            "bookend_end": (
                [
                    _shape_message(m, max_content_len=1200)
                    for m in (view.get("bookend_end") or [])
                    if not _is_compaction_summary(m.get("content", ""))
                ]
                if result_detail == "full"
                else []
            ),
            "messages_before": view.get("messages_before", 0),
            "messages_after": view.get("messages_after", 0),
            "detail": result_detail,
        }
        if lineage_root and lineage_root != hit_sid:
            entry["parent_session_id"] = lineage_root
        results.append(entry)

    for entry in results:
        entry["link"] = _session_link(entry["session_id"], link_profile)

    _final_payload = {
        "success": True,
        "mode": "discover",
        "query": query,
        "detail": detail,
        "results": results,
        "count": len(results),
        "sessions_searched": len(seen_sessions),
    }
    _annotate_rebuild_status(db, _final_payload)
    return json.dumps(_final_payload, ensure_ascii=False)


def _session_search_impl(
    query: str = "",
    role_filter: str = None,
    limit: int = 3,
    db=None,
    current_session_id: str = None,
    # Scroll shape
    session_id: str = None,
    around_message_id: int = None,
    window: int = 5,
    # Discovery shape
    sort: str = None,
    # Link label (any shape)
    profile: str = None,
    # Discovery result shaping (appended to preserve positional compatibility)
    detail: str = "adaptive",
    # Source filter (discovery + browse): restrict to named session sources
    sources: str = None,
) -> str:
    """Single-shape tool. Mode inferred from which args are set.

    Discovery: pass ``query``; ``detail="full"`` hydrates every result.
    Scroll:    pass ``session_id`` + ``around_message_id``.
    Read:      pass ``session_id`` (no anchor) — dumps the whole session.
    Browse:    pass nothing.

    Scroll wins over read/discovery when an anchor is set — the agent has
    asked for a specific slice. ``profile`` only labels the returned links.
    """
    # Normalise a raw `@session:<profile>/<id>` link value passed as session_id.
    # Session ids never contain "/", so a slash unambiguously means profile/id —
    # always strip the prefix off the id, and adopt the embedded profile only
    # when one wasn't passed explicitly.
    if isinstance(session_id, str) and "/" in session_id:
        emb_profile, _, emb_id = session_id.partition("/")
        if emb_id:
            session_id = emb_id
            if emb_profile and (profile is None or not str(profile).strip()):
                profile = emb_profile

    # Scroll shape takes precedence — explicit anchor beats any query.
    if (isinstance(session_id, str) and session_id.strip()) and around_message_id is not None:
        return _scroll(
            db=db,
            session_id=session_id,
            around_message_id=around_message_id,
            window=window,
            current_session_id=current_session_id,
        )

    # Read shape: a session_id with no anchor → dump the whole session.
    if isinstance(session_id, str) and session_id.strip():
        return _read_session(db, session_id.strip(), link_profile=profile)

    # Limit clamp [1, 10]
    if not isinstance(limit, int):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 3
    limit = max(1, min(limit, 10))

    # Parse the source filter (comma-separated), applied to both the browse
    # and discovery shapes so a workstation session can stop a gateway
    # session's history from surfacing in recall.
    source_list: Optional[List[str]] = None
    if isinstance(sources, str) and sources.strip():
        source_list = [s.strip() for s in sources.split(",") if s.strip()]

    # Browse shape: no query → recent sessions.
    if not query or not isinstance(query, str) or not query.strip():
        return _list_recent_sessions(
            db, limit, current_session_id, link_profile=profile, sources=source_list
        )

    # Parse role_filter
    role_list: Optional[List[str]] = None
    if isinstance(role_filter, str) and role_filter.strip():
        role_list = [r.strip() for r in role_filter.split(",") if r.strip()]

    # Normalise sort
    sort_norm: Optional[str] = None
    if isinstance(sort, str):
        candidate = sort.strip().lower()
        if candidate in ("newest", "oldest"):
            sort_norm = candidate

    detail_norm = (
        "full"
        if isinstance(detail, str) and detail.strip().lower() == "full"
        else "adaptive"
    )

    return _discover(
        db=db,
        query=query.strip(),
        role_filter=role_list,
        limit=limit,
        sort=sort_norm,
        detail=detail_norm,
        current_session_id=current_session_id,
        link_profile=profile,
        sources=source_list,
    )


def session_search(
    query: str = "",
    role_filter: str = None,
    limit: int = 3,
    db=None,
    current_session_id: str = None,
    # Scroll shape
    session_id: str = None,
    around_message_id: int = None,
    window: int = 5,
    # Discovery shape
    sort: str = None,
    # Link label (any shape)
    profile: str = None,
    # Discovery result shaping (appended to preserve positional compatibility)
    detail: str = "adaptive",
    # Source filter (discovery + browse): restrict to named session sources
    sources: str = None,
) -> str:
    """Run session search, opening (and closing) the default SessionDB when none is given."""
    owned_db = None
    if db is None:
        try:
            from son_of_anton_state import SessionDB

            db = owned_db = SessionDB()
        except Exception:
            logging.debug("SessionDB unavailable for session_search", exc_info=True)
            from son_of_anton_state import format_session_db_unavailable

            return tool_error(format_session_db_unavailable(), success=False)

    try:
        return _session_search_impl(
            query=query,
            role_filter=role_filter,
            limit=limit,
            db=db,
            current_session_id=current_session_id,
            session_id=session_id,
            around_message_id=around_message_id,
            window=window,
            sort=sort,
            profile=profile,
            detail=detail,
            sources=sources,
        )
    finally:
        if owned_db is not None:
            try:
                owned_db.close()
            except Exception:
                logging.debug("Failed to close session_search SessionDB", exc_info=True)


def check_session_search_requirements() -> bool:
    """Requires the SQLite state database's directory."""
    from son_of_anton_state import _default_db_path
    return _default_db_path().parent.exists()


SESSION_SEARCH_SCHEMA = {
    "name": "session_search",
    "description": (
        "Search or read past son-of-anton conversations (full-text search over the session "
        "store; returns the real messages). It only knows what was said here before: when the "
        "user names a live source (a URL, file, contact, account or system), check that source "
        "first, and don't conclude something doesn't exist from this search alone.\n\n"
        "Shapes:\n"
        "- query: find sessions. The best match comes with its opening and closing messages and "
        "the messages around the hit; the others with just the matching message.\n"
        "- session_id + around_message_id: read a window around one message. To scroll, pass the "
        "window's last (forward) or first (backward) message id.\n"
        "- session_id alone: read a whole session (the first 20 and last 10 messages if long). "
        "Read an @session:<profile>/<id> link this way, with profile set.\n"
        "- no arguments: list recent sessions.\n\n"
        "Query words are ANDed; use OR, \"quoted phrases\", NOT and prefix* as needed. To point "
        "the user at a session, write its link value (e.g. @session:default/20260722_204335_d62c16) "
        "inline, unchanged, as a noun; it shows as the session's title, so don't also spell out "
        "the title, id or date."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Words or an FTS5 expression to search for. Omit to list recent sessions.",
            },
            "limit": {
                "type": "integer",
                "description": "Sessions to return when searching (default 3, max 10).",
                "default": 3,
            },
            "sort": {
                "type": "string",
                "enum": ["newest", "oldest"],
                "description": "'newest' for where-did-we-leave-off questions, 'oldest' for how-did-it-start ones; omit to rank by relevance.",
            },
            "detail": {
                "type": "string",
                "enum": ["adaptive", "full"],
                "description": "'full' gives every result its context; the default 'adaptive' only the top one.",
                "default": "adaptive",
            },
            "session_id": {
                "type": "string",
                "description": "Session to read, from a search result.",
            },
            "around_message_id": {
                "type": "integer",
                "description": "Message id to center the window on, e.g. a result's match_message_id.",
            },
            "window": {
                "type": "integer",
                "description": "Messages on each side of the anchor (1-20, default 5).",
                "default": 5,
            },
            "role_filter": {
                "type": "string",
                "description": "Comma-separated roles to search; default 'user,assistant'. Add 'tool' to include tool output.",
            },
            "profile": {
                "type": "string",
                "description": "Another profile whose sessions to read (read-only), for @session:<profile>/<id> links.",
            },
            "sources": {
                "type": "string",
                "description": "Comma-separated sources to limit to, e.g. 'signal', 'cli', 'cron'. Omit for all.",
            },
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="session_search",
    toolset="session_search",
    schema=SESSION_SEARCH_SCHEMA,
    handler=lambda args, **kw: session_search(
        query=args.get("query") or "",
        role_filter=args.get("role_filter"),
        limit=args.get("limit", 3),
        session_id=args.get("session_id"),
        around_message_id=args.get("around_message_id"),
        window=args.get("window", 5),
        sort=args.get("sort"),
        detail=args.get("detail", "adaptive"),
        profile=args.get("profile"),
        sources=args.get("sources"),
        db=kw.get("db"),
        current_session_id=kw.get("current_session_id"),
    ),
    check_fn=check_session_search_requirements,
    emoji="🔍",
)
