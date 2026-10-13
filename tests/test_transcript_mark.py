"""A turn another process runs between two of this agent's turns is not lost.

An agent keeps the conversation in memory between turns. When the same
session is driven from a second surface (the TUI open on a session the gateway
resumes from Signal), the second surface's turn lands in state.db while the
first agent's history still ends where it left off — its next turn would run
without that exchange and append after it. The agent records
``SessionDB.get_transcript_mark()`` at the end of each turn (and the CLI at
each load), and reloads at the next turn when ``transcript_moved_since()``
says another writer touched the session. These tests pin when it must, and
must not, report movement: a false positive costs the prompt-cache prefix on
every turn.
"""

from __future__ import annotations

import pytest

from son_of_anton_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
from son_of_anton_state import SessionDB


@pytest.fixture
def stores():
    """Two SessionDB handles on one state.db: this process and another one."""
    mine = SessionDB()
    theirs = SessionDB()
    try:
        yield mine, theirs
    finally:
        theirs.close()
        mine.close()


def _turn(db: SessionDB, session_id: str, text: str) -> None:
    db.append_message(session_id, "user", text)
    db.append_message(session_id, "assistant", f"re: {text}")


def test_own_turns_never_read_as_movement(stores):
    mine, _ = stores
    mine.create_session("s1", "cli")
    _turn(mine, "s1", "one")
    mark = mine.get_transcript_mark("s1")
    assert not mine.transcript_moved_since(mark, "s1")
    # The next own turn re-marks; it is still not foreign movement.
    _turn(mine, "s1", "two")
    mark = mine.get_transcript_mark("s1")
    assert not mine.transcript_moved_since(mark, "s1")


def test_a_turn_from_another_process_is_movement(stores):
    mine, theirs = stores
    mine.create_session("s1", "cli")
    _turn(mine, "s1", "one")
    mark = mine.get_transcript_mark("s1")

    _turn(theirs, "s1", "from signal")

    assert mine.transcript_moved_since(mark, "s1")


def test_a_rewind_from_another_process_is_movement(stores):
    mine, theirs = stores
    mine.create_session("s1", "cli")
    _turn(mine, "s1", "one")
    _turn(mine, "s1", "two")
    mark = mine.get_transcript_mark("s1")

    target = theirs.list_recent_user_messages("s1", limit=1)[0]["id"]
    theirs.rewind_to_message("s1", target)

    assert mine.transcript_moved_since(mark, "s1")


def test_a_compaction_rotation_from_another_process_is_movement(stores):
    mine, theirs = stores
    mine.create_session("s1", "cli")
    _turn(mine, "s1", "one")
    mark = mine.get_transcript_mark("s1")

    # The other process compacts: ends s1 and continues in a child, without
    # touching s1's own rows.
    theirs.end_session("s1", "compaction")
    theirs.create_session("s1-cont", "telegram", parent_session_id="s1")
    _turn(theirs, "s1-cont", "after compaction")

    assert mine.transcript_moved_since(mark, "s1")


def test_no_mark_or_another_sessions_mark_proves_nothing(stores):
    mine, theirs = stores
    mine.create_session("s1", "cli")
    mine.create_session("s2", "cli")
    _turn(mine, "s1", "one")
    other_mark = mine.get_transcript_mark("s2")

    _turn(theirs, "s1", "from signal")

    # Without a mark for s1 there is no baseline: keep the caller's history
    # rather than reload on a guess.
    assert not mine.transcript_moved_since(None, "s1")
    assert not mine.transcript_moved_since(other_mark, "s1")


class _Agent:
    _durable_transcript_mark = None


class _CLI(CLIAgentSetupMixin):
    def __init__(self, db: SessionDB, session_id: str) -> None:
        self._session_db = db
        self.session_id = session_id
        self.agent = None


def test_a_load_before_the_agent_exists_still_reaches_its_first_turn(stores):
    """Startup resume loads history before the agent is built."""
    mine, theirs = stores
    mine.create_session("s1", "cli")
    _turn(mine, "s1", "one")

    cli = _CLI(mine, "s1")
    cli._note_loaded_transcript()  # resume loaded the transcript
    _turn(theirs, "s1", "from signal")  # ...then the gateway ran a turn

    cli.agent = _Agent()
    cli._seed_loaded_transcript_mark()  # before the TUI's first turn
    assert mine.transcript_moved_since(cli.agent._durable_transcript_mark, "s1")


def test_a_load_mark_for_a_session_left_since_is_not_applied(stores):
    mine, _ = stores
    mine.create_session("s1", "cli")
    mine.create_session("s2", "cli")
    cli = _CLI(mine, "s1")
    cli._note_loaded_transcript()
    cli.session_id = "s2"  # e.g. /new before the first turn

    cli.agent = _Agent()
    cli._seed_loaded_transcript_mark()
    assert cli.agent._durable_transcript_mark is None
