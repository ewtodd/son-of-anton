"""The compress → compact rename must carry the sessions table with it.

``end_reason = 'compression'`` boundary markers and ``agent.compression*``
provenance stamps in an existing state.db migrate exactly once on open.
"""

from __future__ import annotations

import sqlite3


def test_db_migration_rewrites_persisted_compaction_markers(tmp_path) -> None:
    from son_of_anton_state import SessionDB

    db_path = tmp_path / "state.db"
    SessionDB(db_path=db_path).close()

    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO sessions (id, source, started_at, end_reason, last_activity_provenance) "
        "VALUES ('old', 'cli', 1, 'compression', 'agent.compression_timeout')"
    )
    raw.execute(
        "INSERT INTO sessions (id, source, started_at, end_reason) VALUES ('other', 'cli', 1, 'idle')"
    )
    raw.execute("UPDATE schema_version SET version = 26")
    raw.commit()
    raw.close()

    SessionDB(db_path=db_path).close()  # reopening runs the version-gated chain

    raw = sqlite3.connect(db_path)
    rows = dict(raw.execute("SELECT id, end_reason FROM sessions").fetchall())
    prov = raw.execute("SELECT last_activity_provenance FROM sessions WHERE id = 'old'").fetchone()[0]
    raw.close()
    assert rows == {"old": "compaction", "other": "idle"}
    assert prov == "agent.compaction_timeout"
