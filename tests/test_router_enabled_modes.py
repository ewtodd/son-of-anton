"""``router.modes`` — switching physics off per deployment.

A household or general-purpose gateway has no use for the physics loop.
Leaving it on is not just clutter: the router can drop a message into a
one-shot loop that answers with no conversation history, and ``/mode`` offers
it as though it were on hand.

Turning the mode off means three things together — its keywords stop routing,
``/mode`` stops accepting it, and a session pinned to it before the change
falls back to standard.
"""

from __future__ import annotations

import pytest

from son_of_anton_cli.router import (
    DEFAULT_ENABLED_MODES,
    OPTIONAL_MODES,
    classify_mode,
    resolve_enabled_modes,
    resolve_mode,
)

HOUSEHOLD = ("standard",)


# ── parsing router.modes ──────────────────────────────────────────────────

def test_absent_key_enables_everything() -> None:
    """An existing config.yaml with no router.modes must not change."""
    assert resolve_enabled_modes(None) == DEFAULT_ENABLED_MODES
    assert set(DEFAULT_ENABLED_MODES) == {"standard", *OPTIONAL_MODES}


def test_standard_is_always_present() -> None:
    """It is where classification and every rejected override land."""
    for raw in ([], ["physics"], ["phsyics"]):
        assert "standard" in resolve_enabled_modes(raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param(["standard"], ("standard",), id="household"),
        pytest.param(["physics"], ("standard", "physics"), id="physics-only"),
        pytest.param(["physics", "research"], ("standard", "physics"),
                     id="research-is-gone"),
        pytest.param("physics", ("standard", "physics"), id="bare-string"),
        pytest.param(["PHYSICS", " physics "], ("standard", "physics"),
                     id="case-and-whitespace"),
        pytest.param(["physics", "physics"], ("standard", "physics"), id="duplicates"),
    ],
)
def test_parsing(raw, expected) -> None:
    assert resolve_enabled_modes(raw) == expected


def test_order_is_stable() -> None:
    """Callers interpolate this into user-facing messages."""
    assert resolve_enabled_modes(["physics"]) == ("standard", "physics")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(["phsyics"], id="typo"),
        pytest.param(["nonsense"], id="unknown"),
        pytest.param(42, id="not-a-list"),
        pytest.param({"physics": True}, id="mapping"),
    ],
)
def test_bad_input_never_raises(raw) -> None:
    """config.yaml is hand-edited; a typo must not take the agent down."""
    result = resolve_enabled_modes(raw)
    assert "standard" in result
    assert all(m in DEFAULT_ENABLED_MODES for m in result)


def test_a_typo_fails_closed_not_open() -> None:
    """An unrecognized name yields a mode that is OFF.

    The alternative — treating anything unparseable as "enable everything" —
    would mean a misspelled entry silently restores the loops the operator was
    trying to remove.
    """
    assert resolve_enabled_modes(["phsyics"]) == ("standard",)


# ── routing ───────────────────────────────────────────────────────────────

def test_disabled_keywords_stop_routing() -> None:
    """The keywords go inert, rather than routing to something switched off."""
    assert classify_mode("fit the histogram", HOUSEHOLD) == "standard"
    assert resolve_mode(None, "fit the histogram", enabled=HOUSEHOLD) == "standard"


def test_enabled_keywords_still_route() -> None:
    assert classify_mode("fit the histogram") == "physics"
    assert resolve_mode(None, "fit the histogram") == "physics"


def test_research_keywords_no_longer_route() -> None:
    """The nine-agent pipeline is gone; its former triggers stay standard."""
    assert classify_mode("derive the equation") == "standard"
    assert classify_mode("write a literature review on X") == "standard"
    assert resolve_mode(None, "derive the equation") == "standard"


def test_a_stale_pin_falls_back() -> None:
    """Sessions outlive config edits.

    The mode is stored per session, so a session pinned to physics before the
    operator removed it would otherwise keep entering a loop the deployment no
    longer offers. A stale research pin from before the mode was removed
    lands in standard as well.
    """
    assert resolve_mode("physics", "hello", enabled=HOUSEHOLD) == "standard"
    assert resolve_mode("research", "hello", enabled=HOUSEHOLD) == "standard"


def test_pin_is_honoured_when_the_mode_is_enabled() -> None:
    assert resolve_mode("physics", "hello") == "physics"
    assert resolve_mode("physics", "hello", enabled=("standard", "physics")) == "physics"


def test_first_turn_gate_still_applies() -> None:
    """Enabling a mode does not re-enable mid-conversation classification."""
    assert resolve_mode(None, "fit the histogram", is_first_turn=True) == "physics"
    assert resolve_mode(None, "fit the histogram", is_first_turn=False) == "standard"


def test_standard_pin_is_always_accepted() -> None:
    assert resolve_mode("standard", "fit the histogram", enabled=HOUSEHOLD) == "standard"
