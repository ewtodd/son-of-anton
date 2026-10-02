"""Per-surface scoping for ``session_search`` (STATUS.md future work #3).

The state layer already supported source filtering (``search_messages``
``source_filter`` and ``list_sessions_rich`` ``sources``) but the tool never
surfaced it, so a workstation (``cli``) session would recall a messaging
(``signal`` / ``discord`` / ``slack``) session's history and vice versa.

These tests assert the invariant the tool now promises: a ``sources`` filter
restricts BOTH the browse shape (no query) and the discovery shape (query) to
the named session sources, and leaving it unset searches across all sources.
"""

from __future__ import annotations

import json
import time

from son_of_anton_state import SessionDB
from tools.session_search_tool import _list_recent_sessions, session_search


def _make_session(session_id: str, source: str, *, token: str) -> None:
    db = SessionDB()
    db.create_session(session_id=session_id, source=source)
    db.append_message(
        session_id=session_id,
        role="user",
        content=f"unique-token-{token} body",
        timestamp=time.time(),
    )
    db.close()


def test_browse_filters_to_named_sources(tmp_path) -> None:
    _make_session("20261001_000101_aaaaaa", "cli", token="cli-a")
    _make_session("20261001_000102_bbbbbb", "signal", token="sig-b")
    _make_session("20261001_000103_cccccc", "discord", token="dsc-c")

    out = session_search(sources="cli")
    payload = json.loads(out)
    listed = {s["session_id"] for s in payload.get("results", [])}
    # Only the cli session surfaces; the messaging sessions are excluded.
    assert listed == {"20261001_000101_aaaaaa"}


def test_browse_without_sources_returns_all_surfaces(tmp_path) -> None:
    _make_session("20261001_000201_dddddd", "cli", token="cli-d")
    _make_session("20261001_000202_eeeeee", "telegram", token="tgm-e")

    out = session_search()
    payload = json.loads(out)
    listed = {s["session_id"] for s in payload.get("results", [])}
    assert listed == {"20261001_000201_dddddd", "20261001_000202_eeeeee"}


def test_discovery_filters_to_named_sources(tmp_path) -> None:
    # Two sessions carry the same searchable term; only one is in the requested
    # source. The discovery shape must return only the in-source session.
    _make_session("20261001_000301_ffffff", "cli", token="quantum-flux")
    _make_session("20261001_000302_gggggg", "signal", token="quantum-flux")

    out = session_search(query="quantum-flux", sources="cli")
    payload = json.loads(out)
    found = {r["session_id"] for r in payload.get("results", [])}
    assert "20261001_000301_ffffff" in found
    assert "20261001_000302_gggggg" not in found


def test_discovery_multi_source_filter(tmp_path) -> None:
    _make_session("20261001_000401_hhhhhh", "cli", token="waveform-peak")
    _make_session("20261001_000402_iiiiii", "telegram", token="waveform-peak")
    _make_session("20261001_000403_jjjjjj", "slack", token="waveform-peak")

    out = session_search(query="waveform-peak", sources="cli,telegram")
    payload = json.loads(out)
    found = {r["session_id"] for r in payload.get("results", [])}
    assert "20261001_000401_hhhhhh" in found
    assert "20261001_000402_iiiiii" in found
    assert "20261001_000403_jjjjjj" not in found


def test_list_recent_sessions_accepts_sources_kwarg(tmp_path) -> None:
    # Guard the helper signature so the browse path keeps its filter.
    _make_session("20261001_000501_kkkkkk", "cli", token="scope-k")
    _make_session("20261001_000502_llllll", "discord", token="scope-l")

    db = SessionDB()
    try:
        out = _list_recent_sessions(db, limit=5, sources=["cli"])
    finally:
        db.close()
    payload = json.loads(out)
    listed = {s["session_id"] for s in payload.get("results", [])}
    assert listed == {"20261001_000501_kkkkkk"}
