"""cronjob schedules mean what the tool's schema tells the model they mean.

A delay and a timestamp run once, an interval and a cron expression repeat,
and a timestamp without an offset is wall-clock time in the configured zone.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import son_of_anton_time
from cron.jobs import parse_schedule


def test_once_and_repeating_forms() -> None:
    assert parse_schedule("30m")["kind"] == "once"
    assert parse_schedule("2026-06-01T09:00:00")["kind"] == "once"
    assert parse_schedule("every 2h")["kind"] == "interval"
    assert parse_schedule("0 9 * * *")["kind"] == "cron"


def test_offsetless_timestamp_is_configured_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SON_OF_ANTON_TIMEZONE", "America/Chicago")
    son_of_anton_time.reset_cache()
    try:
        run_at = datetime.fromisoformat(parse_schedule("2026-06-01T18:00:00")["run_at"])
    finally:
        son_of_anton_time.reset_cache()
    assert (run_at.hour, run_at.minute) == (18, 0)
    assert run_at.utcoffset() == timedelta(hours=-5)
