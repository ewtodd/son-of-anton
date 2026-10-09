"""Resume must restore what the session row already knows.

A resumed session builds a fresh ``AIAgent``, so every usage counter starts
at zero and the context meter is driven by a provider reading that does not
exist yet in this process.  The result was a fully-restored transcript under
a "0%" / "0 calls" read-out.  These tests pin the restore contract: the
persisted counters and the last prompt reading come from the session row, and
when the row has no reading the meter falls back to the same request estimate
the turn-start compaction gate would use.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from son_of_anton_cli.cli_agent_setup_mixin import (
    CLIAgentSetupMixin,
    resume_recap_limit,
)
from son_of_anton_state import SessionDB


class _FakeCompactor:
    def __init__(self, context_length: int = 352_256) -> None:
        self.last_prompt_tokens = 0
        self.context_length = context_length
        self.awaiting_real_usage_after_compaction = False
        self.compaction_count = 0


class _FakeAgent:
    def __init__(self) -> None:
        for attr in (
            "session_prompt_tokens",
            "session_completion_tokens",
            "session_total_tokens",
            "session_api_calls",
            "session_input_tokens",
            "session_output_tokens",
            "session_cache_read_tokens",
            "session_cache_write_tokens",
            "session_reasoning_tokens",
        ):
            setattr(self, attr, 0)
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.context_compactor = _FakeCompactor()
        self.tools: list = []


class _Harness(CLIAgentSetupMixin):
    """Just enough of ``SonOfAntonCLI`` to exercise the resume restore."""

    def __init__(self, db, agent, *, resumed: bool, session_id: str, history=None) -> None:
        self._session_db = db
        self.agent = agent
        self._resumed = resumed
        self.session_id = session_id
        self.conversation_history = history or []


@pytest.fixture
def db():
    database = SessionDB()
    try:
        yield database
    finally:
        database.close()


def _seed_session(database: SessionDB, session_id: str, **counters) -> None:
    database.create_session(session_id, "cli")
    database.update_token_counts(
        session_id,
        absolute=True,
        input_tokens=counters.get("input_tokens", 0),
        output_tokens=counters.get("output_tokens", 0),
        cache_read_tokens=counters.get("cache_read_tokens", 0),
        cache_write_tokens=counters.get("cache_write_tokens", 0),
        reasoning_tokens=counters.get("reasoning_tokens", 0),
        api_call_count=counters.get("api_call_count", 0),
        estimated_cost_usd=counters.get("estimated_cost_usd"),
        last_prompt_tokens=counters.get("last_prompt_tokens"),
    )


def test_last_prompt_tokens_round_trips_through_the_session_row(db):
    """The write path: a reading is stored, and never summed with the totals."""
    _seed_session(db, "sess-a", input_tokens=100, api_call_count=1, last_prompt_tokens=7_500)
    row = db._conn.execute(
        "SELECT last_prompt_tokens FROM sessions WHERE id = ?", ("sess-a",)
    ).fetchone()
    assert row[0] == 7_500

    # A later call overwrites the reading rather than accumulating it, and a
    # plain counter update (no reading passed) leaves it alone.
    db.update_token_counts("sess-a", input_tokens=5, api_call_count=1, last_prompt_tokens=9_100)
    db.update_token_counts("sess-a", input_tokens=5, api_call_count=1)
    row = db._conn.execute(
        "SELECT last_prompt_tokens, input_tokens FROM sessions WHERE id = ?", ("sess-a",)
    ).fetchone()
    assert row[0] == 9_100
    assert row[1] == 110


def test_existing_install_gains_the_column_on_open(db):
    """A state.db written before the column existed must migrate in place.

    Existing installs are the whole point of the column — the reconciler adds
    declared-but-missing columns on startup, so simulate one by dropping the
    column and reopening the store.
    """
    _seed_session(db, "sess-old", input_tokens=777, api_call_count=4)
    db._conn.execute("ALTER TABLE sessions DROP COLUMN last_prompt_tokens")
    db._conn.commit()
    db.close()

    reopened = SessionDB()
    try:
        columns = [
            row[1]
            for row in reopened._conn.execute("PRAGMA table_info(sessions)").fetchall()
        ]
        assert "last_prompt_tokens" in columns
        # Legacy rows survive and read back with the new column defaulted.
        row = reopened._conn.execute(
            "SELECT input_tokens, api_call_count, last_prompt_tokens "
            "FROM sessions WHERE id = ?",
            ("sess-old",),
        ).fetchone()
        assert (row[0], row[1], row[2]) == (777, 4, 0)
        reopened.update_token_counts("sess-old", last_prompt_tokens=3_210)
        assert reopened.get_session("sess-old")["last_prompt_tokens"] == 3_210
    finally:
        reopened.close()


def test_resume_restores_counters_and_context_meter(db):
    """The bug this exists for: resume showed 0% and 0 calls over real history."""
    _seed_session(
        db,
        "sess-b",
        input_tokens=50_000,
        output_tokens=1_200,
        cache_read_tokens=20_000,
        cache_write_tokens=400,
        reasoning_tokens=300,
        api_call_count=17,
        estimated_cost_usd=0.42,
        last_prompt_tokens=72_000,
    )
    agent = _FakeAgent()
    harness = _Harness(db, agent, resumed=True, session_id="sess-b")

    harness._restore_resumed_usage_state()

    assert agent.session_input_tokens == 50_000
    assert agent.session_output_tokens == 1_200
    assert agent.session_cache_read_tokens == 20_000
    assert agent.session_cache_write_tokens == 400
    assert agent.session_reasoning_tokens == 300
    assert agent.session_api_calls == 17
    assert agent.session_estimated_cost_usd == pytest.approx(0.42)
    # Provider-shaped aggregates are reconstructed from the canonical buckets.
    assert agent.session_prompt_tokens == 50_000 + 20_000
    assert agent.session_completion_tokens == 1_200
    assert agent.session_total_tokens == 70_000 + 1_200
    # The context meter now reflects the last call, not a fresh session.
    assert agent.context_compactor.last_prompt_tokens == 72_000


def test_resume_without_a_stored_reading_leaves_the_meter_unset(db):
    """No reading stored -> the meter stays 0 and the display falls back."""
    _seed_session(db, "sess-c", input_tokens=10, api_call_count=1)
    agent = _FakeAgent()
    harness = _Harness(db, agent, resumed=True, session_id="sess-c")

    harness._restore_resumed_usage_state()

    assert agent.session_input_tokens == 10
    assert agent.context_compactor.last_prompt_tokens == 0


def test_restore_is_a_noop_when_not_resuming(db):
    _seed_session(db, "sess-d", input_tokens=999, api_call_count=3, last_prompt_tokens=5_000)
    agent = _FakeAgent()
    harness = _Harness(db, agent, resumed=False, session_id="sess-d")

    harness._restore_resumed_usage_state()

    assert agent.session_input_tokens == 0
    assert agent.context_compactor.last_prompt_tokens == 0


def test_restore_survives_a_missing_session_row(db):
    agent = _FakeAgent()
    harness = _Harness(db, agent, resumed=True, session_id="sess-does-not-exist")
    harness._restore_resumed_usage_state()  # must not raise
    assert agent.session_api_calls == 0


def test_context_estimate_covers_the_restored_transcript(db):
    """The display fallback for a row with no reading must be non-zero."""
    agent = _FakeAgent()
    history = [
        {"role": "user", "content": "x" * 400},
        {"role": "assistant", "content": "y" * 400},
    ] * 6
    harness = _Harness(db, agent, resumed=True, session_id="sess-e", history=history)

    estimate = harness._resumed_context_estimate()
    assert estimate > 0

    # Cached per history length: the chrome repaints often.
    harness._resumed_context_estimate_cache = (len(history), 1234)
    assert harness._resumed_context_estimate() == 1234


def test_context_estimate_is_zero_without_history(db):
    harness = _Harness(db, _FakeAgent(), resumed=True, session_id="sess-f")
    assert harness._resumed_context_estimate() == 0


@pytest.mark.parametrize(
    "exchanges,total,expected",
    [
        (0, 40, 40),   # 0 = no cap: paint the whole lineage
        (-1, 40, 40),  # nonsense values behave as "no cap" too
        (10, 40, 20),  # positive value keeps the old 2-entries-per-pair recap
        (10, 5, 5),    # never asks for more than there is
        (0, 0, 0),
    ],
)
def test_recap_limit_zero_means_no_cap(exchanges, total, expected):
    assert resume_recap_limit(exchanges, total) == expected
