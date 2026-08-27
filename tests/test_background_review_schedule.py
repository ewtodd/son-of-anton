"""Automatic background-review scheduling (nudge vs daily).

Covers the once-daily overnight gate added for the self-improvement review
fork: ``auxiliary.background_review.schedule: daily`` replaces the legacy
turn/iteration counters with a wall-clock window plus a per-day latch, so the
fork fires at most once a day.

Stdlib + pytest only; the autouse ``_isolate_son_of_anton_home`` fixture keeps
the daily-latch state file inside a temp SON_OF_ANTON_HOME.
"""

from __future__ import annotations

from datetime import datetime

from agent.background_review import (
    _hour_in_window,
    _parse_daily_window,
    _review_schedule,
    apply_automatic_review_schedule,
    automatic_review_due,
)

# 3am — inside the default [0, 6) overnight window.
_INSIDE = datetime(2026, 8, 27, 3, 0)
# 2pm — outside the default window.
_OUTSIDE = datetime(2026, 8, 27, 14, 0)


def test_nudge_schedule_passes_counters_through() -> None:
    cfg = {"schedule": "nudge"}
    assert apply_automatic_review_schedule(True, False, task_cfg=cfg, now=_INSIDE) == (True, False)
    assert apply_automatic_review_schedule(False, True, task_cfg=cfg, now=_OUTSIDE) == (False, True)
    assert apply_automatic_review_schedule(False, False, task_cfg=cfg, now=_INSIDE) == (False, False)


def test_default_schedule_is_nudge() -> None:
    assert _review_schedule({}) == "nudge"
    assert _review_schedule(None) == "nudge"
    assert _review_schedule({"schedule": "bogus"}) == "nudge"
    assert _review_schedule({"schedule": "DAILY"}) == "daily"


def test_daily_window_parsing_and_fallback() -> None:
    assert _parse_daily_window({}) == (0, 6)
    assert _parse_daily_window({"daily_window": [22, 6]}) == (22, 6)
    # Invalid shapes fall back to the default window.
    assert _parse_daily_window({"daily_window": "midnight"}) == (0, 6)
    assert _parse_daily_window({"daily_window": [24, 6]}) == (0, 6)
    assert _parse_daily_window({"daily_window": [5]}) == (0, 6)
    assert _parse_daily_window({"daily_window": [6, 6]}) == (0, 6)


def test_hour_in_window_normal_and_wrapping() -> None:
    assert _hour_in_window(0, (0, 6)) is True
    assert _hour_in_window(5, (0, 6)) is True
    assert _hour_in_window(6, (0, 6)) is False
    assert _hour_in_window(14, (0, 6)) is False
    # Wrap-midnight window 22 -> 6.
    assert _hour_in_window(23, (22, 6)) is True
    assert _hour_in_window(3, (22, 6)) is True
    assert _hour_in_window(12, (22, 6)) is False


def test_daily_outside_window_never_due() -> None:
    cfg = {"schedule": "daily"}
    assert automatic_review_due(task_cfg=cfg, now=_OUTSIDE) is False
    assert apply_automatic_review_schedule(False, False, task_cfg=cfg, now=_OUTSIDE) == (False, False)


def test_daily_fires_once_per_day() -> None:
    cfg = {"schedule": "daily"}
    # First turn inside the window: due, and a combined review is admitted.
    assert automatic_review_due(task_cfg=cfg, now=_INSIDE) is True
    assert apply_automatic_review_schedule(False, False, task_cfg=cfg, now=_INSIDE) == (True, True)

    # Same day, still inside the window: the latch suppresses a second run.
    assert automatic_review_due(task_cfg=cfg, now=_INSIDE) is False
    assert apply_automatic_review_schedule(True, True, task_cfg=cfg, now=_INSIDE) == (False, False)

    # Next day: due again.
    next_day = datetime(2026, 8, 28, 4, 0)
    assert automatic_review_due(task_cfg=cfg, now=next_day) is True
    assert apply_automatic_review_schedule(False, False, task_cfg=cfg, now=next_day) == (True, True)
