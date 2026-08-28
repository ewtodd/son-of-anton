"""Wall-clock hour windows that may wrap past midnight.

Two features need this predicate and they must not drift apart. The once-daily
background review only fires inside a nightly ``daily_window`` (default
00:00–06:00), and a gateway instance can be restricted to ``active_hours`` so
it answers only outside the operator's working day. Both mean the same thing:
``[start, end)`` in local hours, wrapping past midnight when ``start > end``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

HourWindow = tuple


def parse_hour_window(
    raw: Any, default: Optional[HourWindow] = None
) -> Optional[HourWindow]:
    """Coerce ``[start, end]`` into a validated window, or return *default*.

    Rejects anything that is not two distinct hours in ``0..23``.
    ``start == end`` is rejected rather than read as "always closed": leaving
    the option unset is how you say "no window", and a typo that repeats an
    hour should not silence an instance for a day.
    """
    try:
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            start = int(raw[0])
            end = int(raw[1])
            if 0 <= start < 24 and 0 <= end < 24 and start != end:
                return (start, end)
    except (TypeError, ValueError):
        pass
    return default


def hour_in_window(hour: int, window: HourWindow) -> bool:
    """Is *hour* inside ``[start, end)``, counting a wrap past midnight?"""
    start, end = window
    if start < end:
        return start <= hour < end
    # Window wraps midnight (e.g. 20 → 7).
    return hour >= start or hour < end


def window_gap_key(now: datetime, window: HourWindow) -> str:
    """Identify the closed stretch *now* falls in.

    The gap runs from the window's end hour to its next start. Callers use
    this to send one notice per closed stretch per conversation instead of one
    per message. Only meaningful while ``hour_in_window`` is False; the
    day rolls back when the gap itself began before midnight.
    """
    _, end = window
    day = now.date() if now.hour >= end else now.date() - timedelta(days=1)
    return f"{day.isoformat()}:{end:02d}"


def format_window(window: HourWindow) -> str:
    """Render a window the way a person reads a clock: ``20:00–07:00``."""
    start, end = window
    return f"{start:02d}:00–{end:02d}:00"
