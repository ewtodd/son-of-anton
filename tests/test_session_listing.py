"""``/sessions`` has to show the sessions that exist.

Two surfaces browse the same table and both were lying about it.

On a messenger, one gateway instance answers one chat, so the session key
scopes the listing to that chat and (for a per-user group key) to that
participant. Exactly one session lives in that lane until someone runs
``/new`` — the live one — and the listing dropped it as "the current session".
Every ``/sessions`` in Signal therefore answered "No sessions found" about a
database that was recording the conversation as it asked.

In the terminal the listing filtered on ``source="cli"``. One
``SON_OF_ANTON_HOME`` is one ``state.db``, shared by that account's gateway,
so the account's Signal conversations were in the file the CLI had open and
that filter hid every one of them. Picking a phone conversation back up in the
terminal is the reason the per-account services exist.

Excluding the current session still matters where a number resolves to a
resume target, so it stays the default and browsing surfaces opt out.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from son_of_anton_cli.session_listing import (
    format_gateway_session_listing,
    query_session_listing,
)

SIGNAL_ROW = {
    "id": "20260826_083515_1d4f7e19",
    "source": "signal",
    "title": "Yo As A Young Man",
    "preview": "Yo",
}
CLI_ROW = {"id": "20260825_090000_aaaaaaaa", "source": "cli", "title": "Nix module"}
CRON_ROW = {"id": "cron_16795f8ac0cb_2026", "source": "cron", "title": "Workout check"}
UNTITLED = {"id": "20260826_120000_bbbbbbbb", "source": "signal", "title": None}


class FakeDB:
    """Records the query it was handed and replays canned rows."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[dict] = []

    def list_sessions_rich(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.rows)


# ── the current session ───────────────────────────────────────────────────

def test_one_session_lane_is_not_reported_as_empty() -> None:
    """The exact Signal case: the only row in the lane is the live one."""
    db = FakeDB([SIGNAL_ROW])
    rows = query_session_listing(
        db, source="signal", session_key="agent:main:signal:group:g",
        current_session_id=SIGNAL_ROW["id"], include_current=True,
    )
    assert [r["id"] for r in rows] == [SIGNAL_ROW["id"]]
    assert rows[0]["is_current"] is True


def test_current_session_stays_excluded_by_default() -> None:
    """`/resume <number>` indexes into this list; landing on yourself is a no-op."""
    db = FakeDB([SIGNAL_ROW])
    assert query_session_listing(
        db, source="signal", current_session_id=SIGNAL_ROW["id"]
    ) == []


def test_the_current_row_is_not_mutated_in_place() -> None:
    """``is_current`` is a rendering flag, not a fact about the stored row."""
    db = FakeDB([SIGNAL_ROW])
    query_session_listing(
        db, source="signal", current_session_id=SIGNAL_ROW["id"], include_current=True
    )
    assert "is_current" not in SIGNAL_ROW


def test_an_untitled_current_session_still_shows() -> None:
    """A brand-new chat has no title yet, and is still where you are.

    Without this exemption the named-only default puts the first ``/sessions``
    of every conversation right back at "no sessions".
    """
    db = FakeDB([UNTITLED])
    rows = query_session_listing(
        db, source="signal", current_session_id=UNTITLED["id"],
        include_unnamed=False, include_current=True,
    )
    assert [r["id"] for r in rows] == [UNTITLED["id"]]


def test_other_untitled_sessions_are_still_hidden() -> None:
    """The exemption is for the current session only, not a blanket ``full``."""
    db = FakeDB([UNTITLED, SIGNAL_ROW])
    rows = query_session_listing(
        db, source="signal", current_session_id=SIGNAL_ROW["id"],
        include_unnamed=False, include_current=True,
    )
    assert [r["id"] for r in rows] == [SIGNAL_ROW["id"]]


# ── one home, one database ────────────────────────────────────────────────

def test_a_cross_surface_listing_reaches_every_source() -> None:
    db = FakeDB([SIGNAL_ROW, CLI_ROW, CRON_ROW])
    rows = query_session_listing(
        db, source=None, include_all_sources=True, include_unnamed=True
    )
    assert {r["source"] for r in rows} == {"signal", "cli", "cron"}
    assert db.calls[0]["source"] is None


def test_a_source_scoped_listing_still_pushes_the_filter_down() -> None:
    """Gateway callers stay scoped; the widening is the CLI's, not everyone's."""
    db = FakeDB([SIGNAL_ROW])
    query_session_listing(db, source="signal")
    assert db.calls[0]["source"] == "signal"


def test_the_cli_browser_does_not_filter_by_source() -> None:
    """Guards the regression directly, at the call site that had it wrong.

    ``_list_recent_sessions`` feeds ``/sessions``, ``/resume``'s numbered
    picker, and the empty-``/history`` hint. It asked for ``source="cli"``,
    which is precisely the account's terminal sessions and nothing the gateway
    wrote.
    """
    import cli as cli_module

    obj = object.__new__(cli_module.SonOfAntonCLI)
    obj._session_db = FakeDB([SIGNAL_ROW, CLI_ROW])
    obj.session_id = "not-in-the-list"

    rows = obj._list_recent_sessions(limit=10)

    assert [r["id"] for r in rows] == [SIGNAL_ROW["id"], CLI_ROW["id"]]
    call = obj._session_db.calls[0]
    assert call["source"] is None
    assert call["exclude_sources"] == ["tool"]


# ── the gateway handler ───────────────────────────────────────────────────

def _gateway(rows, *, current_id):
    """A ``/sessions`` handler with only what the listing path touches.

    object.__new__ per the AGENTS.md pitfall: the mixin is routinely built
    without its host class's __init__ in tests.
    """
    from gateway.slash_commands import GatewaySlashCommandsMixin

    obj = object.__new__(GatewaySlashCommandsMixin)
    obj._session_db = FakeDB(rows)
    obj._session_key_for_source = lambda source: "agent:main:signal:group:g"
    obj._resume_caller_is_admin = lambda source: False

    async def _visible(source, row, allow_all):
        return True

    async def _get_or_create(source):
        return types.SimpleNamespace(session_id=current_id)

    obj._resume_row_visible = _visible
    obj.async_session_store = types.SimpleNamespace(
        get_or_create_session=_get_or_create
    )
    return obj


def _event(args: str = ""):
    return types.SimpleNamespace(
        get_command_args=lambda: args,
        source=types.SimpleNamespace(
            platform=types.SimpleNamespace(value="signal"),
        ),
    )


def test_signal_sessions_lists_the_conversation_you_are_in() -> None:
    """End to end at the site that answered "No sessions found"."""
    gw = _gateway([SIGNAL_ROW], current_id=SIGNAL_ROW["id"])
    out = asyncio.run(gw._handle_sessions_command(_event()))

    assert "No sessions" not in out
    assert SIGNAL_ROW["title"] in out
    assert "(current)" in out


def test_the_listing_stays_scoped_to_this_lane_for_a_non_admin() -> None:
    """Widening what is shown must not widen who can see it."""
    gw = _gateway([SIGNAL_ROW], current_id=SIGNAL_ROW["id"])
    asyncio.run(gw._handle_sessions_command(_event()))

    call = gw._session_db.calls[0]
    assert call["session_key"] == "agent:main:signal:group:g"
    assert call["source"] == "signal"


# ── rendering ─────────────────────────────────────────────────────────────

def test_the_live_session_is_marked() -> None:
    row = dict(SIGNAL_ROW, is_current=True)
    out = format_gateway_session_listing([row])
    assert "(current)" in out
    assert SIGNAL_ROW["id"] in out


def test_only_the_live_session_is_marked() -> None:
    out = format_gateway_session_listing([SIGNAL_ROW, CLI_ROW])
    assert "(current)" not in out


def test_the_empty_message_names_something_that_helps() -> None:
    """It used to advise ``/sessions full``, which changes nothing here.

    ``full`` only lifts the named-only filter, and an empty lane has nothing
    to unhide either way — the advice sent the operator looking for a flag
    problem instead of telling them the lane holds one conversation.
    """
    out = format_gateway_session_listing([])
    assert "/sessions full" not in out
    assert "/new" in out


@pytest.mark.parametrize("row", [SIGNAL_ROW, CLI_ROW, CRON_ROW])
def test_the_source_label_is_available_for_mixed_listings(row) -> None:
    out = format_gateway_session_listing([row], include_source=True)
    assert f"`{row['source']}`" in out
