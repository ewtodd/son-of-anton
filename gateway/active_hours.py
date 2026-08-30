"""Restrict a gateway instance to one window of the day.

Several instances on this host share a GPU with the operator's own working
day. Stopping their units outside the shared hours would work, but it loses
inbound messages outright and makes the bot look broken to the people using
it. Instead the service stays up and answers outside its window with a short
notice: the conversation and its history are still there, every slash command
still works, and only agent turns are held.

``gateway.active_hours: [start, end]`` is local time and wraps past midnight,
so ``[20, 7]`` means 8pm through 7am. Unset means always active, which is what
every instance that isn't sharing scarce hardware wants.

Messages arriving in the closed stretch are saved to disk (see
:mod:`gateway.held_messages`).  When the window reopens the gateway
presents a summary and asks for confirmation before processing them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from agent.hour_window import (
    HourWindow,
    format_window,
    hour_in_window,
    window_gap_key,
)

__all__ = ["is_active", "inactive_notice", "gap_key"]


def is_active(
    window: Optional[HourWindow], now: Optional[datetime] = None
) -> bool:
    """Is this instance inside its active window? No window means always."""
    if not window:
        return True
    return hour_in_window((now or datetime.now()).hour, window)


def gap_key(
    window: Optional[HourWindow], now: Optional[datetime] = None
) -> str:
    """Identity of the current closed stretch, for one-notice-per-gap."""
    if not window:
        return ""
    return window_gap_key(now or datetime.now(), window)


def inactive_notice(
    window: Optional[HourWindow],
    message: str = "",
    now: Optional[datetime] = None,
) -> Optional[str]:
    """The reply to send instead of running a turn, or None when active.

    *message* overrides the default wording — an instance whose hours exist
    because someone else is using the hardware can say so in its own words.
    """
    if is_active(window, now):
        return None
    if message.strip():
        return message.strip()
    return (
        f"💤 Not on duty right now — I run {format_window(window)}. "
        "Your message is saved; I'll ask about it when I'm back."
    )
