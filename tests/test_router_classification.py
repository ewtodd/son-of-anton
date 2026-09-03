"""Router classification contracts — the two-mode heuristic that routes
every request. Assert classification behavior (invariants), not keyword
snapshots: the keyword tuples may grow, but the routing rules must hold.
"""

from __future__ import annotations

from son_of_anton_cli.router import (
    AGENT_MODES,
    classify_mode,
    resolve_mode,
)


def test_classify_mode_physics_keywords_route_physics() -> None:
    physics_requests = [
        "fit the histogram to the calibration data",
        "what is the half-life of bromine-82?",
        "measure this cross-section from the root file",
    ]
    for text in physics_requests:
        assert classify_mode(text) == "physics"


def test_research_pipeline_keywords_are_standard_now() -> None:
    """The nine-agent pipeline was removed; its former triggers stay standard."""
    standard_requests = [
        "derive the effective action for this theory",
        "do a literature review on superconducting qubits",
        "prove that the sequence converges",
    ]
    for text in standard_requests:
        assert classify_mode(text) == "standard"


def test_classify_mode_everything_else_is_standard() -> None:
    everyday = [
        "what time is it?",
        "tell me a joke",
        "create a file with today's date",
        "physics class was fun today",  # passing mention must not route
    ]
    for text in everyday:
        assert classify_mode(text) == "standard"


def test_classify_mode_is_case_insensitive() -> None:
    assert classify_mode("FIT THE HISTOGRAM") == "physics"


def test_mode_pins_override_classification() -> None:
    assert resolve_mode("physics", "hello") == "physics"
    assert resolve_mode("standard", "fit the histogram") == "standard"
    # auto / None mean "classify"
    assert resolve_mode(None, "fit the histogram") == "physics"
    assert resolve_mode("auto", "fit the histogram") == "physics"


def test_agent_modes_are_the_documented_values() -> None:
    assert set(AGENT_MODES) == {"auto", "standard", "physics"}


def test_followups_do_not_reroute_into_the_stateless_loop() -> None:
    """Auto-classification is first-turn only.

    physics is a one-shot loop fed ONLY the current message
    (gateway.run._run_physics_mode_sync takes ``problem_text``, no history),
    so re-classifying a mid-conversation turn silently discards the exchange
    and the run answers as if it had never spoken to the user. For anyone who
    discusses this subject matter routinely, ordinary follow-ups trip the
    keyword list constantly.
    """
    from son_of_anton_cli.router import resolve_mode

    followups = [
        "what about the half-life though",
        "ok now fit the histogram",
        "and the cross-section?",
        "what does that mean for the isotope",
        "how about pulse shape discrimination",
    ]
    for text in followups:
        # First turn: classification is what the router is for.
        assert resolve_mode(None, text, is_first_turn=True) == "physics", text
        # Mid-conversation: stay in the loop that carries history.
        assert resolve_mode(None, text, is_first_turn=False) == "standard", text


def test_explicit_mode_pin_wins_on_any_turn() -> None:
    """A /mode pin is unambiguous; only keyword guessing is gated."""
    from son_of_anton_cli.router import resolve_mode

    for turn in (True, False):
        assert resolve_mode("physics", "hello there", is_first_turn=turn) == "physics"
        assert resolve_mode("standard", "fit the histogram", is_first_turn=turn) == "standard"


def test_first_turn_default_preserves_legacy_callers() -> None:
    """Callers that don't pass is_first_turn still classify (back-compat)."""
    from son_of_anton_cli.router import resolve_mode

    assert resolve_mode(None, "fit the histogram") == "physics"
