"""The compress → compact rename must carry persisted state with it.

Two things outlive a code rename: the user's config.yaml (``compression`` /
``auxiliary.compression`` sections, ``context.engine: compressor``) and the
sessions table (``end_reason = 'compression'`` boundary markers and
``agent.compression*`` provenance stamps). Both migrate exactly once, and a
value already present under the new key wins.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace


def test_config_migration_folds_old_sections_into_new_ones(monkeypatch) -> None:
    from son_of_anton_cli import config_migrations as cm

    on_disk = {
        "compression": {"threshold": 0.85, "enabled": True},
        "compaction": {"threshold": 0.6},  # already-present new key wins
        "auxiliary": {"compression": {"model": "flash", "timeout": 300}},
        "context": {"engine": "compressor"},
    }
    persisted = {}
    fake = SimpleNamespace(
        read_raw_config=lambda: on_disk,
        _persist_migration=lambda cfg: persisted.update(cfg),
    )
    monkeypatch.setattr(cm, "_cfg", lambda: fake)
    cm._migrate_to_39({"warnings": []}, quiet=True)

    assert "compression" not in persisted
    assert persisted["compaction"] == {"threshold": 0.6, "enabled": True}
    assert "compression" not in persisted["auxiliary"]
    assert persisted["auxiliary"]["compaction"] == {"model": "flash", "timeout": 300}
    assert persisted["context"]["engine"] == "compactor"
    assert (39, cm._migrate_to_39) in cm.MIGRATIONS


def test_config_migration_is_a_no_op_without_old_keys(monkeypatch) -> None:
    from son_of_anton_cli import config_migrations as cm

    calls = []
    fake = SimpleNamespace(
        read_raw_config=lambda: {"compaction": {"threshold": 0.5}},
        _persist_migration=lambda cfg: calls.append(cfg),
    )
    monkeypatch.setattr(cm, "_cfg", lambda: fake)
    cm._migrate_to_39({"warnings": []}, quiet=True)
    assert calls == [], "nothing to fold, nothing written"


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
