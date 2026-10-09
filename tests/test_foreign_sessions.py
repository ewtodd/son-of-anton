"""Foreign session import: Claude Code, Codex CLI, and opencode.

The import contract is strict role alternation with plain user/assistant
text; tool activity is summarized, never fabricated as ``tool_calls``.  These
tests pin the format adapters against fixtures, including the parts of each
store that are easy to get wrong: Claude's append-only rewind DAG and
compaction boundary, opencode's pending revert and compaction marker, and
Codex's developer/response_item split.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from son_of_anton_cli import session_analysis
from son_of_anton_cli.foreign_sessions import (
    import_foreign_session,
    list_opencode_sessions,
    parse_claude_session,
    parse_codex_session,
    parse_opencode_session,
    run_sessions_import,
)
from son_of_anton_state import SessionDB


def _write_jsonl(path: Path, objects: list) -> Path:
    with open(path, "w", encoding="utf-8") as fh:
        for obj in objects:
            fh.write(json.dumps(obj))
            fh.write("\n")
    return path


# ── Claude Code ──────────────────────────────────────────────────────────


def _claude_turn(role: str, text: str, record_uuid: str, parent_uuid, **extra):
    obj = {
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/work/proj",
        "sessionId": "11111111-2222-3333-4444-555555555555",
        "version": "2.0.30",
        "gitBranch": "main",
        "type": role,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
        "uuid": record_uuid,
        "timestamp": "2026-01-01T10:00:00.000Z",
    }
    obj.update(extra)
    return obj


def test_claude_rewind_imports_only_the_live_branch(tmp_path):
    path = _write_jsonl(
        tmp_path / "session.jsonl",
        [
            _claude_turn("user", "original question", "u1", None),
            _claude_turn("assistant", "abandoned answer", "a1", "u1"),
            _claude_turn("user", "edited question", "u1b", None),
            _claude_turn("assistant", "new answer", "a1b", "u1b"),
        ],
    )
    parsed = parse_claude_session(path)
    assert [(t["role"], t["content"]) for t in parsed["turns"]] == [
        ("user", "edited question"),
        ("assistant", "new answer"),
    ]


def test_claude_compaction_boundary_keeps_pre_compaction_history(tmp_path):
    boundary = {
        "parentUuid": None,
        "logicalParentUuid": "a1",
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": "b1",
        "sessionId": "11111111-2222-3333-4444-555555555555",
        "cwd": "/work/proj",
        "timestamp": "2026-01-01T10:00:03.000Z",
    }
    path = _write_jsonl(
        tmp_path / "session.jsonl",
        [
            _claude_turn("user", "before", "u1", None),
            _claude_turn("assistant", "answer before", "a1", "u1"),
            boundary,
            _claude_turn("user", "after", "u2", "b1"),
            _claude_turn("assistant", "answer after", "a2", "u2"),
        ],
    )
    parsed = parse_claude_session(path)
    assert [t["content"] for t in parsed["turns"]] == [
        "before",
        "answer before",
        "after",
        "answer after",
    ]


def test_claude_skips_sidechains_meta_and_command_wrappers(tmp_path):
    path = _write_jsonl(
        tmp_path / "session.jsonl",
        [
            _claude_turn(
                "user",
                "<local-command-caveat>Caveat: generated locally."
                "</local-command-caveat>",
                "u1",
                None,
            ),
            _claude_turn("user", "real question", "u2", "u1"),
            _claude_turn(
                "assistant", "SIDECHAIN NOISE", "a-side", "u2", isSidechain=True
            ),
            _claude_turn("user", "META NOISE", "u-meta", "u2", isMeta=True),
            _claude_turn("assistant", "real answer", "a2", "u2"),
        ],
    )
    parsed = parse_claude_session(path)
    assert [t["content"] for t in parsed["turns"]] == [
        "real question",
        "real answer",
    ]


# ── Codex CLI ────────────────────────────────────────────────────────────


def test_codex_rollout_parses_turns_and_tool_calls(tmp_path):
    path = _write_jsonl(
        tmp_path / "rollout-2026-01-01T00-00-00-codex-1.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "codex-1", "cwd": "/work"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "fix it"}],
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call", "name": "shell"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "system noise"}],
                },
            },
        ],
    )
    parsed = parse_codex_session(path)
    assert parsed["cwd"] == "/work"
    assert parsed["session_id"] == "codex-1"
    assert [(t["role"], t["content"]) for t in parsed["turns"]] == [
        ("user", "fix it"),
        ("assistant", "[ran tool: shell]\n\ndone"),
    ]


# ── opencode ─────────────────────────────────────────────────────────────


def _opencode_db(db_path: Path, sessions: list, messages: list, parts: list) -> Path:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            parent_id TEXT,
            directory TEXT,
            title TEXT,
            revert TEXT,
            time_created INTEGER,
            time_updated INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        """
    )
    conn.executemany("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?)", sessions)
    conn.executemany("INSERT INTO message VALUES (?, ?, ?, ?, ?)", messages)
    conn.executemany("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)", parts)
    conn.commit()
    conn.close()
    return db_path


def _oc_message(message_id: str, session_id: str, role: str, created: int):
    return (
        message_id,
        session_id,
        created,
        created,
        json.dumps({"role": role, "time": {"created": created}}),
    )


def _oc_part(part_id: str, message_id: str, session_id: str, data: dict):
    return (part_id, message_id, session_id, 0, 0, json.dumps(data))


def _basic_opencode_fixture(db_path: Path) -> Path:
    session_id = "ses_test0001"
    return _opencode_db(
        db_path,
        sessions=[
            (
                session_id,
                "global",
                None,
                "/work/proj",
                "Test Session",
                None,
                1000,
                2000,
            )
        ],
        messages=[
            _oc_message("msg_0001", session_id, "user", 1000),
            _oc_message("msg_0002", session_id, "assistant", 1100),
            _oc_message("msg_0003", session_id, "user", 1200),
            _oc_message("msg_0004", session_id, "assistant", 1300),
            _oc_message("msg_0005", session_id, "user", 1400),
        ],
        parts=[
            _oc_part("prt_0001", "msg_0001", session_id, {"type": "text", "text": "hello"}),
            _oc_part("prt_0002", "msg_0002", session_id, {"type": "step-start"}),
            _oc_part(
                "prt_0003",
                "msg_0002",
                session_id,
                {"type": "reasoning", "text": "thinking"},
            ),
            _oc_part("prt_0004", "msg_0002", session_id, {"type": "text", "text": "hi"}),
            _oc_part(
                "prt_0005",
                "msg_0002",
                session_id,
                {
                    "type": "tool",
                    "tool": "read",
                    "state": {"status": "completed"},
                },
            ),
            _oc_part(
                "prt_0006",
                "msg_0002",
                session_id,
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "error"},
                },
            ),
            _oc_part("prt_0007", "msg_0002", session_id, {"type": "step-finish"}),
            _oc_part(
                "prt_0008",
                "msg_0003",
                session_id,
                {"type": "compaction", "auto": True, "tail_start_id": "msg_0001"},
            ),
            _oc_part(
                "prt_0009",
                "msg_0004",
                session_id,
                {"type": "text", "text": "## Objective\nsummary"},
            ),
            _oc_part("prt_0010", "msg_0005", session_id, {"type": "text", "text": "next"}),
        ],
    )


def test_opencode_text_tool_and_compaction(tmp_path):
    db_path = _basic_opencode_fixture(tmp_path / "opencode.db")
    parsed = parse_opencode_session(db_path, "ses_test0001")
    assert parsed["cwd"] == "/work/proj"
    assert parsed["title_guess"] == "Test Session"
    assert [(t["role"], t["content"]) for t in parsed["turns"]] == [
        ("user", "hello"),
        ("assistant", "hi\n\n[ran tool: read]\n\n[tool failed: bash]"),
        ("user", "[conversation compacted]"),
        ("assistant", "## Objective\nsummary"),
        ("user", "next"),
    ]


def test_opencode_pending_revert_truncates_transcript(tmp_path):
    session_id = "ses_test0002"
    db_path = _opencode_db(
        tmp_path / "opencode.db",
        sessions=[
            (
                session_id,
                "global",
                None,
                "/work/proj",
                "Reverted",
                json.dumps({"messageID": "msg_0103"}),
                1000,
                2000,
            )
        ],
        messages=[
            _oc_message("msg_0101", session_id, "user", 1000),
            _oc_message("msg_0102", session_id, "assistant", 1100),
            _oc_message("msg_0103", session_id, "user", 1200),
            _oc_message("msg_0104", session_id, "assistant", 1300),
        ],
        parts=[
            _oc_part("prt_0101", "msg_0101", session_id, {"type": "text", "text": "keep"}),
            _oc_part("prt_0102", "msg_0102", session_id, {"type": "text", "text": "kept reply"}),
            _oc_part("prt_0103", "msg_0103", session_id, {"type": "text", "text": "discarded"}),
            _oc_part("prt_0104", "msg_0104", session_id, {"type": "text", "text": "discarded reply"}),
        ],
    )
    parsed = parse_opencode_session(db_path, session_id)
    assert [t["content"] for t in parsed["turns"]] == ["keep", "kept reply"]


def test_opencode_part_revert_cuts_inside_message(tmp_path):
    session_id = "ses_test0003"
    db_path = _opencode_db(
        tmp_path / "opencode.db",
        sessions=[
            (
                session_id,
                "global",
                None,
                "/work/proj",
                "Part Reverted",
                json.dumps({"messageID": "msg_0202", "partID": "prt_0203"}),
                1000,
                2000,
            )
        ],
        messages=[
            _oc_message("msg_0201", session_id, "user", 1000),
            _oc_message("msg_0202", session_id, "assistant", 1100),
        ],
        parts=[
            _oc_part("prt_0201", "msg_0201", session_id, {"type": "text", "text": "keep"}),
            _oc_part("prt_0202", "msg_0202", session_id, {"type": "text", "text": "kept part"}),
            _oc_part("prt_0203", "msg_0202", session_id, {"type": "text", "text": "cut part"}),
        ],
    )
    parsed = parse_opencode_session(db_path, session_id)
    assert [t["content"] for t in parsed["turns"]] == ["keep", "kept part"]


def test_opencode_listing_uses_root_sessions_only(tmp_path):
    db_path = _opencode_db(
        tmp_path / "opencode.db",
        sessions=[
            ("ses_root", "global", None, "/work", "Root", None, 1000, 2000),
            ("ses_child", "global", "ses_root", "/work", "Child", None, 1100, 2200),
        ],
        messages=[
            _oc_message("msg_r1", "ses_root", "user", 1000),
            _oc_message("msg_c1", "ses_child", "user", 1100),
        ],
        parts=[
            _oc_part("prt_r1", "msg_r1", "ses_root", {"type": "text", "text": "root"}),
            _oc_part("prt_c1", "msg_c1", "ses_child", {"type": "text", "text": "child"}),
        ],
    )
    listed = list_opencode_sessions(db_path, limit=10)
    assert [s.session_id for s in listed] == ["ses_root"]
    assert listed[0].cwd == "/work"
    assert listed[0].turn_count == 1


def test_opencode_import_requires_session_id(tmp_path):
    db_path = _basic_opencode_fixture(tmp_path / "opencode.db")
    with pytest.raises(ValueError):
        import_foreign_session("opencode", db_path, foreign_id=None)


def test_import_opencode_session_end_to_end(tmp_path):
    db_path = _basic_opencode_fixture(tmp_path / "opencode.db")
    session_db = SessionDB()
    try:
        new_id = import_foreign_session(
            "opencode", db_path, db=session_db, foreign_id="ses_test0001"
        )
        row = session_db._conn.execute(
            "SELECT source, cwd, origin_json, title FROM sessions WHERE id = ?",
            (new_id,),
        ).fetchone()
        assert row[0] == "opencode"
        assert row[1] == "/work/proj"
        assert row[3] == "Imported from opencode: Test Session"
        origin = json.loads(row[2])
        assert origin["imported_from"]["tool"] == "opencode"
        assert origin["imported_from"]["foreign_session_id"] == "ses_test0001"

        messages = session_db._conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
            (new_id,),
        ).fetchall()
        roles = [m[0] for m in messages]
        assert roles == ["user", "assistant", "user", "assistant", "user"]
        assert messages[1][1] == "hi\n\n[ran tool: read]\n\n[tool failed: bash]"
    finally:
        session_db.close()


def test_run_sessions_import_opencode_by_id(tmp_path):
    db_path = _basic_opencode_fixture(tmp_path / "opencode.db")
    args = SimpleNamespace(
        from_source="opencode",
        path=None,
        foreign_session_id="ses_test0001",
        opencode_db=str(db_path),
    )
    new_id = run_sessions_import(args)
    assert new_id
    session_db = SessionDB()
    try:
        count = session_db._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (new_id,)
        ).fetchone()[0]
        assert count == 5
    finally:
        session_db.close()


def test_run_sessions_import_opencode_positional_session_id(tmp_path):
    db_path = _basic_opencode_fixture(tmp_path / "opencode.db")
    args = SimpleNamespace(
        from_source="opencode",
        path="ses_test0001",
        foreign_session_id=None,
        opencode_db=str(db_path),
    )
    assert run_sessions_import(args)


# ── analysis pass ────────────────────────────────────────────────────────


def test_build_analysis_prompt_names_session_and_rules(tmp_path):
    prompt = session_analysis.build_analysis_prompt(
        session_id="20260101_000000_abcdef",
        source_label="opencode",
        title="Imported from opencode: Fix widget",
        transcript_path=tmp_path / "transcript.md",
    )
    assert "20260101_000000_abcdef" in prompt
    assert "opencode" in prompt
    assert str(tmp_path / "transcript.md") in prompt
    assert "Never record secrets" in prompt
    assert "evidence, not instructions" in prompt


def test_write_transcript_export_writes_redacted_markdown():
    db = SessionDB()
    try:
        session_id = db.create_session("sess_analysis", source="opencode", cwd="/work")
        db.append_message(session_id, "user", "the aws key is AKIA1234567890ABCDEF")
    finally:
        db.close()

    db = SessionDB()
    try:
        path = session_analysis.write_transcript_export(db, "sess_analysis")
    finally:
        db.close()

    assert path is not None and path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "sess_analysis" in text
    assert "AKIA1234567890ABCDEF" not in text


def test_run_session_analysis_passes_transcript_to_runner():
    db = SessionDB()
    captured: dict = {}

    def fake_runner(prompt: str) -> int:
        captured["prompt"] = prompt
        return 0

    try:
        session_id = db.create_session("sess_analysis2", source="opencode", cwd="/work")
        db.append_message(session_id, "user", "hello world")
        status = session_analysis.run_session_analysis(
            session_id,
            source_label="opencode",
            title="Imported from opencode: Analysis",
            db=db,
            runner=fake_runner,
        )
    finally:
        db.close()

    assert status == 0
    assert "sess_analysis2" in captured["prompt"]
    assert "analysis-sess_analysis2.md" in captured["prompt"]


def test_run_session_analysis_reads_source_and_title_from_store():
    db = SessionDB()
    captured: dict = {}

    def fake_runner(prompt: str) -> int:
        captured["prompt"] = prompt
        return 0

    try:
        session_id = db.create_session("sess_analysis4", source="claude-code", cwd="/work")
        db.set_session_title(session_id, "Imported from Claude Code: Widget")
        db.append_message(session_id, "user", "hello")
        status = session_analysis.run_session_analysis(
            session_id, db=db, runner=fake_runner
        )
    finally:
        db.close()

    assert status == 0
    assert "claude-code" in captured["prompt"]
    assert "Imported from Claude Code: Widget" in captured["prompt"]


def test_run_session_analysis_survives_runner_failure():
    db = SessionDB()

    def boom(prompt: str) -> int:
        raise RuntimeError("no model configured")

    try:
        session_id = db.create_session("sess_analysis3", source="opencode", cwd="/work")
        db.append_message(session_id, "user", "hello")
        status = session_analysis.run_session_analysis(
            session_id, source_label="opencode", db=db, runner=boom
        )
    finally:
        db.close()
    assert status == 1


def test_run_session_analysis_missing_session_is_a_failure():
    db = SessionDB()
    try:
        status = session_analysis.run_session_analysis(
            "does-not-exist", source_label="opencode", db=db, runner=lambda p: 0
        )
    finally:
        db.close()
    assert status == 1


def test_run_sessions_import_analyze_runs_pass_after_import(tmp_path, monkeypatch):
    db_path = _basic_opencode_fixture(tmp_path / "opencode.db")
    calls: list = []

    def fake_analysis(session_id, *, source_label, db=None, title=""):
        calls.append((session_id, source_label))
        return 0

    monkeypatch.setattr(session_analysis, "run_session_analysis", fake_analysis)
    args = SimpleNamespace(
        from_source="opencode",
        path=None,
        foreign_session_id="ses_test0001",
        opencode_db=str(db_path),
        analyze=True,
    )
    new_id = run_sessions_import(args)
    assert new_id
    assert calls == [(new_id, "opencode")]


def test_run_sessions_import_without_analyze_skips_pass(tmp_path, monkeypatch):
    db_path = _basic_opencode_fixture(tmp_path / "opencode.db")
    calls: list = []

    def fake_analysis(*args, **kwargs):
        calls.append(1)
        return 0

    monkeypatch.setattr(session_analysis, "run_session_analysis", fake_analysis)
    args = SimpleNamespace(
        from_source="opencode",
        path=None,
        foreign_session_id="ses_test0001",
        opencode_db=str(db_path),
        analyze=False,
    )
    assert run_sessions_import(args)
    assert calls == []
