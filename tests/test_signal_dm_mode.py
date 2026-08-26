"""SIGNAL_DM_MODE: the DM half of multi-gateway Signal routing.

Several gateways can serve ONE Signal account, separated by group id — each
subscribes to the same signal-cli SSE stream and drops groups that are not its
own. A DM carries no such routing key.

SIGNAL_ALLOWED_USERS cannot separate the two cases: gateway/authz_mixin.py
checks `check_ids & allowed_ids` against the sender for DMs and group messages
alike. The owner's number MUST therefore be listed for group messages to
authorize at all, which simultaneously authorizes the owner's DMs on every
running gateway — one DM, N full agent turns, N replies. SIGNAL_DM_MODE=ignore
is what stops that.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.platforms.signal import DM_MODES, SignalAdapter, resolve_dm_mode


# ── the parser (real code, not a copy) ────────────────────────────────────

def test_default_is_allow() -> None:
    """Unset must not change single-gateway behaviour."""
    assert resolve_dm_mode(None) == "allow"
    assert resolve_dm_mode("") == "allow"


@pytest.mark.parametrize("raw,expected", [
    ("allow", "allow"), ("ignore", "ignore"),
    (" IGNORE ", "ignore"), ("Allow", "allow"),
])
def test_mode_parsing(raw, expected) -> None:
    assert resolve_dm_mode(raw) == expected


def test_unknown_mode_falls_back_to_allow() -> None:
    """A typo must fail OPEN.

    Failing closed would make the gateway silently deaf to DMs, which is
    indistinguishable from the bot being down and much harder to diagnose
    than one unexpected reply.
    """
    assert resolve_dm_mode("ignroe") == "allow"
    assert resolve_dm_mode("off") == "allow"
    assert DM_MODES == {"allow", "ignore"}


# ── the intake filter (drives _handle_envelope) ───────────────────────────

def _adapter(dm_mode: str, groups: set[str] | None = None) -> SignalAdapter:
    """A SignalAdapter with only the attributes the intake path reads.

    object.__new__ per the AGENTS.md pitfall: adapters are routinely built
    without BasePlatformAdapter.__init__ in tests.
    """
    a = object.__new__(SignalAdapter)
    a.dm_mode = dm_mode
    a.group_allow_from = groups or set()
    a.require_mention = False
    a.ignore_stories = True
    a.account = "+15550000000"
    a._account_normalized = "+15550000000"
    a._dispatched = []

    async def _capture(*args, **kwargs):
        a._dispatched.append((args, kwargs))

    # Everything past the intake filter funnels through the emit path; capture
    # it so "was this dispatched?" is observable without a live gateway.
    a.emit_message = _capture
    return a


def _dm(text: str = "hi", sender: str = "+15551234567") -> dict:
    return {"envelope": {"source": sender, "sourceNumber": sender,
                         "timestamp": 1, "dataMessage": {"message": text}}}


def _group(text: str, group_id: str, sender: str = "+15551234567") -> dict:
    return {"envelope": {"source": sender, "sourceNumber": sender, "timestamp": 1,
                         "dataMessage": {"message": text,
                                         "groupInfo": {"groupId": group_id}}}}


def _run(adapter, envelope) -> int:
    """Drive intake; return how many messages made it past the filter."""
    try:
        asyncio.run(adapter._handle_envelope(envelope))
    except Exception:
        # Past the filter the real path needs a live gateway; anything that
        # raises later has already proved the message was NOT dropped.
        return 1
    return len(adapter._dispatched)


def test_ignore_drops_dms() -> None:
    assert _run(_adapter("ignore"), _dm()) == 0


def test_allow_does_not_drop_dms() -> None:
    """The default must still let DMs through."""
    assert _run(_adapter("allow"), _dm()) > 0


def test_ignore_does_not_affect_group_routing() -> None:
    """Dropping DMs must not change which groups an instance answers.

    These are the two halves of one routing decision. A change to one that
    silently altered the other would make an instance stop answering its own
    group — the failure that is hardest to notice.
    """
    a = _adapter("ignore", groups={"gW"})
    assert _run(a, _group("hi", "gW")) > 0           # my group: still served
    assert _run(_adapter("ignore", {"gW"}), _group("hi", "gP")) == 0  # not mine


def test_note_to_self_survives_ignore() -> None:
    """Note to Self arrives as a promoted syncMessage that looks like a DM.

    Dropping it would silence the operator's own channel, so the DM filter
    must exempt it explicitly.
    """
    own = "+15550000000"
    envelope = {"envelope": {"source": own, "sourceNumber": own, "timestamp": 1,
                             "syncMessage": {"sentMessage": {
                                 "message": "note", "destination": own}}}}
    assert _run(_adapter("ignore"), envelope) > 0
