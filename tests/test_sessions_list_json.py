"""``sessions list --json --here`` feeds the relay's continue/new prompt.

The relay needs the workspace's sessions, most-recently-active first, with
enough metadata to say "a session from <date> in <dir> — continue or new?".
"""

from __future__ import annotations

import argparse
import json
import time

from son_of_anton_cli.sessions_cmd import cmd_sessions
from son_of_anton_state import SessionDB


def _args(**overrides):
    values = {
        "sessions_action": "list",
        "source": None,
        "limit": 20,
        "workspace": None,
        "here": False,
        "json": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _make_session(session_id: str, cwd: str, *, title=None, message_at=None) -> None:
    db = SessionDB()
    db.create_session(session_id=session_id, source="cli", cwd=cwd)
    if title:
        db.set_session_title(session_id, title)
    if message_at is not None:
        db.append_message(
            session_id=session_id, role="user", content="hello", timestamp=message_at
        )
    db.close()


def test_json_rows_carry_the_relay_fields(capsys, tmp_path) -> None:
    here = tmp_path / "project"
    here.mkdir()
    _make_session("20261001_000001_aaaaaa", str(here), title="Local work")
    _make_session("20261001_000002_bbbbbb", str(tmp_path / "other"))

    cmd_sessions(_args())
    rows = json.loads(capsys.readouterr().out)
    assert {row["id"] for row in rows} == {
        "20261001_000001_aaaaaa",
        "20261001_000002_bbbbbb",
    }
    row = next(r for r in rows if r["id"] == "20261001_000001_aaaaaa")
    assert row["workspace"] == str(here)
    assert row["cwd"] == str(here)
    assert row["title"] == "Local work"
    assert row["message_count"] == 0
    assert row["last_active_relative"]


def test_here_filters_to_the_current_directory(capsys, tmp_path, monkeypatch) -> None:
    here = tmp_path / "project"
    here.mkdir()
    _make_session("20261001_000003_cccccc", str(here))
    _make_session("20261001_000004_dddddd", str(tmp_path / "elsewhere"))
    monkeypatch.chdir(here)

    cmd_sessions(_args(here=True))
    rows = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in rows] == ["20261001_000003_cccccc"]


def test_json_is_ordered_by_last_activity(capsys, tmp_path) -> None:
    now = time.time()
    _make_session("20261001_000005_eeeeee", str(tmp_path), message_at=now + 100)
    _make_session("20261001_000006_ffffff", str(tmp_path), message_at=now)

    cmd_sessions(_args())
    rows = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in rows] == [
        "20261001_000005_eeeeee",
        "20261001_000006_ffffff",
    ]


def test_empty_result_is_an_empty_json_array(capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cmd_sessions(_args(here=True))
    assert json.loads(capsys.readouterr().out) == []


# ---------------------------------------------------------------------------
# sessions delete --all
# ---------------------------------------------------------------------------

def _delete_args(*, session_id=None, all_sessions=False, yes=True):
    return argparse.Namespace(
        sessions_action="delete",
        session_id=session_id,
        all_sessions=all_sessions,
        yes=yes,
    )


def test_sessions_delete_all_removes_every_row(capsys, tmp_path) -> None:
    _make_session("20261001_000001_aaaaaa", str(tmp_path), title="one")
    _make_session("20261001_000002_bbbbbb", str(tmp_path), title="two")
    _make_session("20261001_000003_cccccc", str(tmp_path))

    cmd_sessions(_delete_args(all_sessions=True))
    assert "Deleted 3 session(s)." in capsys.readouterr().out

    db = SessionDB()
    try:
        assert db.list_sessions_rich(
            limit=10, include_children=True, include_archived=True
        ) == []
    finally:
        db.close()


def test_sessions_delete_all_rejects_an_explicit_id(capsys) -> None:
    rc = cmd_sessions(_delete_args(session_id="20261001_000001_aaaaaa", all_sessions=True))
    assert rc == 1
    assert "not both" in capsys.readouterr().out
