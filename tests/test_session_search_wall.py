"""The session_search wall: a terminal never reads a Signal conversation.

Embeddings are stubbed; sessions live in the isolated SON_OF_ANTON_HOME.
"""

from __future__ import annotations

import json

import pytest

from agent import rag
from son_of_anton_constants import get_son_of_anton_home
from son_of_anton_state import SessionDB
from tools.session_search_tool import session_search

SIG = "20261101_000001_sig"
SIG2 = "20261101_000002_sig2"
TUI = "20261101_000003_tui"
TUI2 = "20261101_000004_tui2"
CLI_CRON = "cron_cc0000000001_20261101_090000"
SIG_CRON = "cron_cc0000000002_20261101_090000"
SECRET = "zebrafruit"  # a token only the Signal sessions say


def _fake_embed(config, texts, **kwargs):
    return [[float(t.lower().count("zebrafruit")), float(t.lower().count("kiwi") + t.lower().count("fruit")), 0.01]
            for t in texts]


def _msg(db, sid, role, text, ts):
    return db.append_message(session_id=sid, role=role, content=text, timestamp=ts)


@pytest.fixture
def world(monkeypatch):
    import cron.jobs as jobs
    import cron.scheduler as scheduler

    monkeypatch.setattr(
        jobs, "get_job",
        lambda job_id: {"id": job_id, "deliver": "signal:chatX" if job_id.endswith("2") else "local"},
    )
    monkeypatch.setattr(
        scheduler, "_resolve_delivery_targets",
        lambda job: [{"platform": "signal", "chat_id": "chatX"}] if job["deliver"] != "local" else [],
    )
    db = SessionDB()
    db.create_session(session_id=SIG, source="signal", chat_id="groupA", cwd="/tmp")
    db.create_session(session_id=SIG2, source="signal", chat_id="groupB", cwd="/tmp")
    db.create_session(session_id=TUI, source="tui", cwd="/tmp")
    db.create_session(session_id=TUI2, source="tui", cwd="/tmp")
    db.create_session(session_id=CLI_CRON, source="cron", cwd="/tmp")
    db.create_session(session_id=SIG_CRON, source="cron", cwd="/tmp")
    ids = {}
    ids["sig"] = _msg(db, SIG, "user", f"{SECRET} health note", 10.0)
    ids["sig2"] = _msg(db, SIG2, "user", f"{SECRET} food note", 11.0)
    ids["tui"] = _msg(db, TUI, "user", "plain terminal work", 12.0)
    ids["cli_cron"] = _msg(db, CLI_CRON, "assistant", f"{SECRET} common cron digest", 13.0)
    ids["sig_cron"] = _msg(db, SIG_CRON, "assistant", f"{SECRET} signal cron digest", 14.0)
    db.set_session_title(SIG, "Dietary secrets") if hasattr(db, "set_session_title") else None
    db.close()
    return ids


def _run(caller, **kw):
    return json.loads(session_search(current_session_id=caller, **kw))


def _found(payload):
    return {r["session_id"] for r in payload.get("results", [])}


def test_terminal_cannot_discover_list_read_or_scroll_signal(world) -> None:
    walled = {SIG, SIG2, SIG_CRON}
    discovered = _found(_run(TUI2, query=SECRET, limit=10))
    assert discovered == {CLI_CRON}
    assert not walled & _found(_run(TUI2, limit=10))
    assert CLI_CRON in _found(_run(TUI2, limit=10))
    # Title match.
    assert not walled & _found(_run(TUI2, query="Dietary secrets"))
    # Even naming the source cannot widen past the wall.
    assert _found(_run(TUI2, query=SECRET, sources="signal,cron", limit=10)) == {CLI_CRON}
    assert _found(_run(TUI2, sources="signal", limit=10)) == set()
    for sid in walled:
        read = _run(TUI2, session_id=sid)
        assert read["success"] is False and "not found" in read["error"]
        assert SECRET not in json.dumps(read)
    for sid, key in ((SIG, "sig"), (SIG_CRON, "sig_cron")):
        scroll = _run(TUI2, session_id=sid, around_message_id=world[key])
        assert scroll["success"] is False and "not found" in scroll["error"]
        # An anchor that lives in a walled session, named under a visible one.
        sneaky = _run(TUI2, session_id=TUI, around_message_id=world[key])
        assert sneaky["success"] is False and SECRET not in json.dumps(sneaky)
    # A walled session reads exactly like a missing one.
    assert _run(TUI2, session_id=SIG)["error"].replace(SIG, "X") == \
        _run(TUI2, session_id="20261101_999999_none")["error"].replace("20261101_999999_none", "X")


def test_terminal_still_reads_terminal_sessions(world) -> None:
    assert _run(TUI2, session_id=TUI)["success"] is True
    assert _run(TUI2, session_id=CLI_CRON)["success"] is True
    assert _run(TUI2, session_id=TUI, around_message_id=world["tui"])["success"] is True
    assert TUI in _found(_run(TUI2, limit=10))


def test_an_unidentifiable_session_is_hidden_from_the_terminal(world) -> None:
    db = SessionDB()
    db.create_session(session_id="20261101_000050_odd", source="telegram", cwd="/tmp")
    _msg(db, "20261101_000050_odd", "user", f"{SECRET} odd one", 20.0)
    db.close()
    assert "20261101_000050_odd" not in _found(_run(TUI2, query=SECRET, limit=10))
    assert _run(TUI2, session_id="20261101_000050_odd")["success"] is False
    assert _run(SIG, session_id="20261101_000050_odd")["success"] is True


def test_oneshot_sessions_count_as_terminal(world) -> None:
    db = SessionDB()
    db.create_session(session_id="20261101_000060_z", source="oneshot", cwd="/tmp")
    _msg(db, "20261101_000060_z", "user", "oneshot pipeline output", 21.0)
    db.close()
    assert _run(TUI2, session_id="20261101_000060_z")["success"] is True


def test_signal_sees_everything_including_terminal(world) -> None:
    found = _found(_run(SIG, query=SECRET, limit=10))
    assert {SIG2, SIG_CRON, CLI_CRON} <= found
    assert _run(SIG, session_id=TUI)["success"] is True
    assert _run(SIG, session_id=SIG2)["success"] is True
    assert _run(SIG, session_id=TUI, around_message_id=world["tui"])["success"] is True


def test_signal_bound_cron_reads_like_signal_and_terminal_cron_like_terminal(world) -> None:
    assert _run(SIG_CRON, session_id=SIG)["success"] is True
    assert _run(CLI_CRON, session_id=SIG)["success"] is False


def test_no_caller_is_treated_as_terminal(world) -> None:
    assert _run(None, session_id=SIG)["success"] is False


@pytest.fixture
def semantic(world, monkeypatch):
    monkeypatch.setattr(rag, "embed_texts", _fake_embed)
    rag._SEARCH_STATE["cooldown_until"] = 0.0
    full = {"memory": {"rag": {"enabled": True, "base_url": "http://e.invalid/v1", "model": "bge-m3"}}}
    original = rag.get_rag_config
    monkeypatch.setattr(rag, "get_rag_config", lambda config=None: original(full if config is None else config))
    journals = get_son_of_anton_home() / "journals"
    journals.mkdir(parents=True, exist_ok=True)
    rows = {
        SIG: "kiwi smoothie plan for the week",
        SIG2: "kiwi dinner idea",
        TUI: "kiwi compiler flags",
        CLI_CRON: "kiwi digest",
    }
    for sid, text in rows.items():
        lines = [{"type": "session", "session_id": sid, "ts": 1.0},
                 {"type": "message", "role": "user", "content": text, "ts": 2.0}]
        (journals / f"{sid}.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\n", encoding="utf-8")
    db = SessionDB()
    for sid, text in rows.items():
        _msg(db, sid, "user", text, 30.0)
    db.close()
    assert rag.sync_index(full)["ok"]
    return full


def test_semantic_hits_merge_into_discovery_and_respect_the_wall(semantic) -> None:
    # "fruit" is no word of any message: only the embedding finds them.
    from_terminal = _found(_run(TUI2, query="fruit", limit=10))
    assert {TUI, CLI_CRON} <= from_terminal
    assert not {SIG, SIG2} & from_terminal
    from_signal = _found(_run(SIG, query="fruit", limit=10))
    assert {SIG2, TUI} <= from_signal


def test_semantic_search_degrades_to_fts_when_the_embedder_is_down(semantic, monkeypatch) -> None:
    def _down(config, texts, **kwargs):
        raise TimeoutError("down")

    monkeypatch.setattr(rag, "embed_texts", _down)
    payload = _run(TUI2, query="compiler flags", limit=10)
    assert payload["success"] is True and TUI in _found(payload)
    assert not {SIG, SIG2} & _found(_run(TUI2, query="fruit", limit=10))
    # The earlier hits came from meaning alone: without the embedder, none.
    assert TUI not in _found(_run(TUI2, query="fruit", limit=10))


def test_walled_rows_cannot_crowd_a_terminal_search_out_of_the_scan(world) -> None:
    db = SessionDB()
    for i in range(350):
        _msg(db, SIG, "user", f"crowdword filler {i}", 100.0 + i)
    _msg(db, TUI, "user", "crowdword the one readable hit", 1.0)
    db.close()
    found = _found(_run(TUI2, query="crowdword", limit=5, sort="newest"))
    assert found == {TUI}
