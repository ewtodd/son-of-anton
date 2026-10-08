"""Session listing/rich rows, export, and import (portability) for SessionDB.

Mixin contract: this is a plain mixin class consumed by
``son_of_anton_state.SessionDB``. It defines no ``__init__`` and no state of its
own; methods access the host's attributes (``self._conn``, ``self.db_path``,
``self._execute_write`` and other SessionDB methods) established by
``SessionDB.__init__``. It must never import son_of_anton_state (cycle) — shared
module-level constants live in son_of_anton_state_common.
"""

import logging
import json
import time
from typing import Any, Dict, List, Optional

from agent.skill_commands import SKILL_SCAFFOLD_SQL_LIKE
from son_of_anton_state_common import (
    SCHEMA_SQL,
    _PREVIEW_RAW_SELECT,
    _shape_preview,
    _sql_session_last_active,
)

# Moved methods logged under the "son_of_anton_state" logger before the split;
# keep that logger identity so log filtering/capture behavior is unchanged.
logger = logging.getLogger("son_of_anton_state")


class SessionPortabilityMixin:
    """See module docstring — mixin for SessionDB (Port cluster)."""

    @classmethod
    def _compact_session_cols(cls) -> str:
        """SELECT list for compact_rows: every ``sessions`` column declared in
        SCHEMA_SQL except prompt storage internals, aliased with the ``s``
        prefix used by list_sessions_rich/_get_session_rich_row queries."""
        if cls._session_compact_cols_sql is None:
            declared = cls._parse_schema_columns(SCHEMA_SQL)["sessions"]
            cls._session_compact_cols_sql = ", ".join(
                f"s.{name}" for name in declared
                if name not in cls._SESSION_COMPACT_EXCLUDED
            )
        return cls._session_compact_cols_sql


    def _get_session_rich_rows_batch(
        self, session_ids, compact_rows: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch multiple sessions with the same enriched columns as
        ``_get_session_rich_row``, in a single query.

        Used by ``list_sessions_rich``'s compaction-tip projection to resolve
        every tip row for a page in one round trip instead of one query per
        compaction-root row. Returns a dict keyed by session id; ids that
        don't exist are simply absent from the result (same as
        ``_get_session_rich_row`` returning ``None`` for them).
        """
        ids = [sid for sid in session_ids if sid]
        if not ids:
            return {}
        # Old SQLite builds cap bound variables at 999
        # (SQLITE_MAX_VARIABLE_NUMBER); large pages (limit=10000 callers
        # exist) could exceed it. Chunk the IN list so the helper is safe at
        # any page size — this is the single choke point for the enriched
        # multi-row fetch, so the bound lives here, not at call sites.
        _CHUNK = 900
        if len(ids) > _CHUNK:
            result: Dict[str, Dict[str, Any]] = {}
            for start in range(0, len(ids), _CHUNK):
                result.update(
                    self._get_session_rich_rows_batch(
                        ids[start:start + _CHUNK], compact_rows=compact_rows
                    )
                )
            return result
        # Same read-your-writes guarantee as list_sessions_rich.
        self.flush_token_counts()
        _sel = self._compact_session_cols() if compact_rows else "s.*"
        placeholders = ",".join("?" for _ in ids)
        prompt_select = (
            "" if compact_rows
            else ", COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved"
        )
        prompt_join = (
            "" if compact_rows
            else "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash"
        )
        query = f"""
            SELECT {_sel}{prompt_select},
                COALESCE(
                    (SELECT {_PREVIEW_RAW_SELECT}
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                {_sql_session_last_active("s")} AS last_active
            FROM sessions s
            {prompt_join}
            WHERE s.id IN ({placeholders})
        """
        with self._lock:
            cursor = self._conn.execute(query, ids)
            rows = cursor.fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            s = self._session_row_dict(row)
            s["preview"] = _shape_preview(s.pop("_preview_raw", ""))
            result[s["id"]] = s
        return result


    def list_skill_scaffolded_sessions(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Titled sessions whose first user turn was a ``/skill`` invocation.

        Those titles were generated from the expanded message, which embeds the
        whole skill body — so they describe the skill rather than the request.
        Returns ``id``, ``title``, and the full first-turn ``content`` so a
        caller can re-derive what the user typed. Newest first.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.id, s.title, m.content
                FROM sessions s
                JOIN messages m ON m.id = (
                    SELECT m2.id FROM messages m2
                    WHERE m2.session_id = s.id AND m2.role = 'user'
                      AND m2.content IS NOT NULL
                    ORDER BY m2.timestamp, m2.id LIMIT 1
                )
                WHERE s.title IS NOT NULL AND m.content LIKE ?
                ORDER BY s.started_at DESC
                LIMIT ?
                """,
                (SKILL_SCAFFOLD_SQL_LIKE, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]


    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a single session with all its messages as a dict."""
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id)
        return {**session, "messages": messages}

    def export_session_lineage(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a compaction lineage as one logical session dict."""
        lineage_ids = self.get_compaction_lineage(session_id)
        if not lineage_ids:
            return None
        segments = []
        for sid in lineage_ids:
            segment = self.export_session(sid)
            if segment:
                segments.append(segment)
        if not segments:
            return None
        base = dict(segments[-1])
        total_messages = sum(len(seg.get("messages") or []) for seg in segments)
        base["segments"] = segments
        base["lineage_session_ids"] = [seg["id"] for seg in segments]
        base["message_count"] = total_messages
        base["messages"] = [msg for seg in segments for msg in (seg.get("messages") or [])]
        return base

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    @staticmethod
    def _import_text_or_none(value: Any, field: str) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        raise ValueError(f"{field} must be a string")

    @staticmethod
    def _import_json_object_or_none(value: Any, field: str) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{field} must be valid JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"{field} must be a JSON object")
            return value
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be a JSON object")
        try:
            return json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be JSON serializable") from exc

    @staticmethod
    def _float_or_none(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _import_int_or_none(value: Any, field: str) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer") from exc

    @staticmethod
    def _int_or_default(value: Any, default: int = 0) -> int:
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _reasoning_json_value(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    @staticmethod
    def _import_error(index: int, session_id: str, error: str) -> Dict[str, Any]:
        item: Dict[str, Any] = {"index": index, "error": error}
        if session_id:
            item["session_id"] = session_id
        return item

