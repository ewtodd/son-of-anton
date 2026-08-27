"""The wordmark, and the terminal width it needs.

Rich wraps anything wider than the console, and a wrapped block-letter logo
is rubble rather than a logo — which is the whole reason there is a width
gate around it at all.

The wordmark reads best on one line, so that is what a normal terminal gets.
The stacked form exists for windows too narrow for it (and for the README,
where GitHub renders into a fixed column). Below the stacked form's own floor
the banner goes without a logo instead of printing a wrapped one.
"""

from __future__ import annotations

import pytest

from son_of_anton_cli.banner import (
    LOGO_STACKED_MIN_COLUMNS,
    LOGO_WIDE_MIN_COLUMNS,
    SON_OF_ANTON_AGENT_LOGO_STACKED,
    SON_OF_ANTON_AGENT_LOGO_WIDE,
    pick_banner_logo,
)


def _width(art: str) -> int:
    return max(len(line) for line in art.splitlines())


# ── the art itself ────────────────────────────────────────────────────────

def test_the_wide_form_is_one_line_of_letters() -> None:
    """Six rows of block characters, no blank row splitting the words."""
    lines = SON_OF_ANTON_AGENT_LOGO_WIDE.splitlines()
    assert len(lines) == 6
    assert all(line.strip() for line in lines)


def test_the_stacked_form_is_two_lines_of_letters() -> None:
    lines = SON_OF_ANTON_AGENT_LOGO_STACKED.splitlines()
    assert len(lines) == 13
    assert lines[6].strip() == ""


def test_each_floor_matches_the_art_it_gates() -> None:
    """A floor below the art's real width would let Rich wrap it."""
    assert LOGO_WIDE_MIN_COLUMNS >= _width(SON_OF_ANTON_AGENT_LOGO_WIDE)
    assert LOGO_STACKED_MIN_COLUMNS >= _width(SON_OF_ANTON_AGENT_LOGO_STACKED)
    assert LOGO_WIDE_MIN_COLUMNS > LOGO_STACKED_MIN_COLUMNS


# ── picking one ───────────────────────────────────────────────────────────

def test_a_normal_terminal_gets_the_wide_form() -> None:
    assert pick_banner_logo(120) is SON_OF_ANTON_AGENT_LOGO_WIDE
    assert pick_banner_logo(LOGO_WIDE_MIN_COLUMNS) is SON_OF_ANTON_AGENT_LOGO_WIDE


def test_a_narrow_terminal_falls_back_rather_than_going_bare() -> None:
    """The band between the two floors used to print the stacked form.

    Widening the default must not cost those windows their logo.
    """
    assert pick_banner_logo(LOGO_WIDE_MIN_COLUMNS - 1) is SON_OF_ANTON_AGENT_LOGO_STACKED
    assert pick_banner_logo(LOGO_STACKED_MIN_COLUMNS) is SON_OF_ANTON_AGENT_LOGO_STACKED


def test_a_very_narrow_terminal_gets_no_logo() -> None:
    assert pick_banner_logo(LOGO_STACKED_MIN_COLUMNS - 1) == ""
    assert pick_banner_logo(40) == ""


# ── skins ─────────────────────────────────────────────────────────────────

def test_a_skin_logo_wins_when_there_is_room() -> None:
    """A skin ships its own art; its width is the skin author's business."""
    assert pick_banner_logo(200, skin_logo="[bold]ART[/]") == "[bold]ART[/]"
    assert pick_banner_logo(
        LOGO_STACKED_MIN_COLUMNS, skin_logo="[bold]ART[/]"
    ) == "[bold]ART[/]"


def test_a_skin_logo_is_still_dropped_on_a_tiny_terminal() -> None:
    assert pick_banner_logo(LOGO_STACKED_MIN_COLUMNS - 1, skin_logo="[bold]ART[/]") == ""


@pytest.mark.parametrize("empty", ["", None])
def test_no_skin_logo_means_ours(empty) -> None:
    assert pick_banner_logo(120, skin_logo=empty or "") is SON_OF_ANTON_AGENT_LOGO_WIDE
