"""An instance can be restricted to part of the day without going offline.

markets and ricky share a GPU with the operator's working day. Stopping their
units outside the shared hours is the obvious move and the wrong one: inbound
messages vanish, the people using them see a bot that is simply broken, and a
restart replays a day of pending work. So the unit stays up and the gateway
answers with a notice instead of starting a turn.

Two properties carry the design. The window wraps midnight, because "8pm to
7am" is one window and not two. And the gate is not a wall: recognized slash
commands and anything owned by in-flight work still get through, exactly as
they do for the global emergency stop, whose passthrough logic this now
shares.
"""

from __future__ import annotations

import types
from datetime import datetime

import pytest

from agent.hour_window import (
    format_window,
    hour_in_window,
    parse_hour_window,
    window_gap_key,
)
from gateway.active_hours import gap_key, inactive_notice, is_active

NIGHT = (20, 7)   # wraps midnight — the deployed case
DAY = (9, 17)     # does not wrap — the ordinary case


def _at(hour: int, day: int = 27) -> datetime:
    return datetime(2026, 8, day, hour, 30)


# ── the window predicate ──────────────────────────────────────────────────

@pytest.mark.parametrize("hour", [20, 21, 23, 0, 3, 6])
def test_a_wrapping_window_is_one_window(hour: int) -> None:
    """8pm→7am spans midnight; both halves are inside it."""
    assert hour_in_window(hour, NIGHT)


@pytest.mark.parametrize("hour", [7, 8, 12, 17, 19])
def test_the_working_day_is_outside_a_night_window(hour: int) -> None:
    assert not hour_in_window(hour, NIGHT)


def test_the_window_is_half_open() -> None:
    """[start, end): the start hour is in, the end hour is already out."""
    assert hour_in_window(20, NIGHT) and not hour_in_window(7, NIGHT)
    assert hour_in_window(9, DAY) and not hour_in_window(17, DAY)


@pytest.mark.parametrize("hour", [9, 12, 16])
def test_a_non_wrapping_window_still_works(hour: int) -> None:
    assert hour_in_window(hour, DAY)


# ── parsing ───────────────────────────────────────────────────────────────

def test_a_window_parses_from_a_config_list() -> None:
    assert parse_hour_window([20, 7]) == (20, 7)
    assert parse_hour_window(("20", "7")) == (20, 7)


@pytest.mark.parametrize(
    "raw", [None, "", "20-7", [20], [20, 24], [-1, 7], ["x", "y"], {}]
)
def test_junk_leaves_the_instance_unrestricted(raw) -> None:
    """A malformed window must not silence an instance. Default wins."""
    assert parse_hour_window(raw) is None


def test_a_repeated_hour_is_rejected_rather_than_read_as_always_closed() -> None:
    """`[9, 9]` is a typo. Reading it as a zero-length window would mute the
    instance for good, and "no window" is already how you say "no limit"."""
    assert parse_hour_window([9, 9]) is None


# ── one notice per closed stretch ─────────────────────────────────────────

def test_the_gap_key_is_stable_across_one_closed_stretch() -> None:
    """07:00 and 19:00 on the same day are the same gap, so one notice."""
    assert window_gap_key(_at(7), NIGHT) == window_gap_key(_at(19), NIGHT)


def test_the_gap_key_changes_on_the_next_day() -> None:
    assert window_gap_key(_at(12, day=27), NIGHT) != window_gap_key(
        _at(12, day=28), NIGHT
    )


def test_a_closed_stretch_that_crosses_midnight_is_still_one_stretch() -> None:
    """Invert the window and the *gap* wraps instead. A 07:00-20:00 instance
    is shut from 8pm to 7am, so 22:00 and 02:00 are the same closed stretch
    and must not produce two notices either side of midnight."""
    day_shift = (7, 20)
    assert window_gap_key(_at(22, day=27), day_shift) == window_gap_key(
        _at(2, day=28), day_shift
    )


# ── the notice ────────────────────────────────────────────────────────────

def test_no_notice_while_the_window_is_open() -> None:
    assert inactive_notice(NIGHT, now=_at(22)) is None


def test_an_unrestricted_instance_is_always_active() -> None:
    assert is_active(None, _at(12)) is True
    assert inactive_notice(None, now=_at(12)) is None
    assert gap_key(None, _at(12)) == ""


def test_the_default_notice_names_the_hours() -> None:
    """Without them the reply is just "no", which sends people looking for a
    fault instead of waiting an hour."""
    out = inactive_notice(NIGHT, now=_at(12))
    assert "20:00" in out and "07:00" in out


def test_an_instance_can_say_it_in_its_own_words() -> None:
    out = inactive_notice(NIGHT, "  GPU is mine until 8pm.  ", now=_at(12))
    assert out == "GPU is mine until 8pm."


def test_format_window_reads_like_a_clock() -> None:
    assert format_window(NIGHT) == "20:00–07:00"


# ── the gateway gate ──────────────────────────────────────────────────────

def _runner(*, window=NIGHT, message="", running=False, command=None):
    """A runner with only what the two gates touch (AGENTS.md pitfall:
    gateway classes are routinely built without __init__ in tests)."""
    from gateway.run import GatewayRunner

    obj = object.__new__(GatewayRunner)
    obj.config = types.SimpleNamespace(
        active_hours=window, inactive_message=message
    )
    obj._session_key_for_source = lambda source: "agent:main:signal:group:g"
    obj._peek_session_state = lambda key: None
    obj._is_session_running = lambda key: running
    return obj


def _source(chat_id: str = "group:1"):
    return types.SimpleNamespace(
        chat_id=chat_id,
        platform=types.SimpleNamespace(value="signal"),
    )


def _event(command=None, text: str = "hello"):
    return types.SimpleNamespace(
        text=text,
        get_command=lambda: command,
    )


def test_a_chat_is_told_once_per_closed_stretch() -> None:
    gw = _runner()
    first = gw._active_hours_notice(_source(), now=_at(8))
    second = gw._active_hours_notice(_source(), now=_at(14))
    assert first and "20:00" in first
    # "" holds the turn without speaking again.
    assert second == ""


def test_each_chat_gets_its_own_notice() -> None:
    """One group being told must not silence the gate for another."""
    gw = _runner()
    assert gw._active_hours_notice(_source("group:1"), now=_at(8))
    assert gw._active_hours_notice(_source("group:2"), now=_at(8))


def test_the_notice_comes_back_the_next_day() -> None:
    gw = _runner()
    gw._active_hours_notice(_source(), now=_at(8, day=27))
    assert gw._active_hours_notice(_source(), now=_at(8, day=28))


def test_an_open_window_returns_none_and_records_nothing() -> None:
    gw = _runner()
    assert gw._active_hours_notice(_source(), now=_at(22)) is None
    # Still speaks when the window later closes.
    assert gw._active_hours_notice(_source(), now=_at(8, day=28))


def test_an_unrestricted_runner_never_holds_a_turn() -> None:
    gw = _runner(window=None)
    assert gw._active_hours_notice(_source(), now=_at(12)) is None


# ── the passthroughs ──────────────────────────────────────────────────────

def test_a_slash_command_is_not_swallowed_by_a_closed_gate() -> None:
    """/status and /help have to work while the window is shut, or the only
    way to ask what is going on is the thing that is blocked."""
    gw = _runner()
    assert gw._turn_gate_passthrough(_event(command="status"), _source()) is True


def test_an_unrecognized_slash_word_is_not_a_passthrough() -> None:
    gw = _runner()
    assert gw._turn_gate_passthrough(
        _event(command="not-a-real-command"), _source()
    ) is False


def test_plain_text_is_held() -> None:
    gw = _runner()
    assert gw._turn_gate_passthrough(_event(), _source()) is False


def test_steering_a_running_agent_is_delivered() -> None:
    """The gate holds NEW turns. Work already in flight when the window closed
    keeps its follow-ups, or it stalls on a message it is waiting for."""
    gw = _runner(running=True)
    assert gw._turn_gate_passthrough(_event(), _source()) is True


def test_auto_resume_is_held_outside_the_window() -> None:
    """A restart at noon must not replay pending turns — that is exactly the
    work the window exists to defer."""
    from gateway.run import _active_hours_open

    open_cfg = types.SimpleNamespace(active_hours=None)
    night_cfg = types.SimpleNamespace(active_hours=NIGHT)
    assert _active_hours_open(open_cfg) is True
    assert _active_hours_open(night_cfg) == hour_in_window(
        datetime.now().hour, NIGHT
    )


# ── config plumbing ───────────────────────────────────────────────────────

def test_the_window_survives_a_config_round_trip() -> None:
    from gateway.config import GatewayConfig

    cfg = GatewayConfig.from_dict(
        {"active_hours": [20, 7], "inactive_message": "  busy  "}
    )
    assert cfg.active_hours == (20, 7)
    assert cfg.inactive_message == "busy"
    assert cfg.to_dict()["active_hours"] == [20, 7]


def test_an_absent_window_stays_absent_through_the_config() -> None:
    from gateway.config import GatewayConfig

    cfg = GatewayConfig.from_dict({})
    assert cfg.active_hours is None
    assert cfg.to_dict()["active_hours"] is None
