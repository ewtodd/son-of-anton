"""Request router for son-of-anton.

Classifies a user request into an agent mode: standard / physics.
The design mirrors the heuristic router from the archived temple harness:
conservative, fast, no model call, with the final word left to explicit user
overrides.

Temple also classified a *model slot* (simple / default / complex) here. That
half was ported, given a config surface and tests, and never wired to model
resolution — nothing read its answer — so it is gone. Per-request model
choice belongs to the normal resolution chain and ``/model``.

The knobs that exist are ``router.enabled`` and ``router.modes``.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

AGENT_MODES = ("auto", "standard", "physics")

# Modes a deployment may switch off. "standard" is not among them: it is the
# fallback every other path lands on, so it is always available.
OPTIONAL_MODES = ("physics",)

# Everything on, which is what an existing config.yaml with no `router.modes`
# key means.
DEFAULT_ENABLED_MODES = ("standard",) + OPTIONAL_MODES


def resolve_enabled_modes(raw: object) -> tuple[str, ...]:
    """Normalize ``router.modes`` into the set of selectable modes.

    A gateway serving a household group has no use for the physics loop, and
    listing it costs more than clutter: the router can route a message into a
    one-shot loop nobody there wants, and the mode shows up in ``/mode`` as if
    it were on offer.

    ``None`` (the key absent) means every mode, so an existing config.yaml is
    unaffected. "standard" is always included even if omitted, because it is
    where classification and every rejected override end up. Unknown names are
    dropped with a warning rather than failing the load: a typo in config.yaml
    should not take the agent down, and the result is a mode that is off, which
    is the safe direction.
    """
    if raw is None:
        return DEFAULT_ENABLED_MODES
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        logger.warning(
            "Ignoring router.modes: expected a list of mode names, got %s",
            type(raw).__name__,
        )
        return DEFAULT_ENABLED_MODES

    enabled = {"standard"}
    for entry in raw:
        name = str(entry).strip().lower()
        if name in ("standard", "auto"):
            continue
        if name in OPTIONAL_MODES:
            enabled.add(name)
        else:
            logger.warning("Ignoring unknown router.modes entry %r", entry)
    # Stable order so callers can render it in messages predictably.
    return tuple(m for m in DEFAULT_ENABLED_MODES if m in enabled)

# Keyword signal for the physics analysis mode. Keep this deliberately
# specific: it should fire for real analysis work (ROOT macros, histogram
# fitting, calibration) and stay quiet for everyday chat that merely
# mentions physics in passing.
PHYSICS_KEYWORDS = (
    "root file",
    ".root ",
    "uproot",
    "awkward array",
    "histogram",
    "fitting a",
    "fit the",
    "cross-section",
    "cross section",
    "half-life",
    "energy calibration",
    "calibration run",
    "geant4",
    "pulse shape",
    "coincidence",
    "detector response",
    "isotope",
    "decay curve",
    "gamma-ray",
    "gamma ray",
    "cern root",
    "tfile",
    "tchain",
    "roofit",
    "nuclear data",
)


def classify_mode(
    text: str,
    enabled: Optional[Sequence[str]] = None,
) -> str:
    """Return ``physics`` or ``standard`` for *text*.

    *enabled* limits what may be returned; a disabled mode is simply not
    classified into, so its keywords stop meaning anything. ``None`` means
    every mode.
    """
    allowed = DEFAULT_ENABLED_MODES if enabled is None else tuple(enabled)
    low = text.lower()
    if "physics" in allowed and any(k in low for k in PHYSICS_KEYWORDS):
        return "physics"
    return "standard"


def resolve_mode(
    override: Optional[str],
    text: str,
    *,
    is_first_turn: bool = True,
    enabled: Optional[Sequence[str]] = None,
) -> str:
    """Resolve the agent mode for one request.

    *override* is the session's ``/mode`` pin (``None`` or ``"auto"`` means
    classify); the result is always one of the two concrete modes.

    *is_first_turn* gates keyword classification. physics is a
    one-shot loop that receives ONLY the current message — no conversation
    history — so routing a mid-conversation turn into it silently drops
    everything said so far and the run answers as if it had never spoken to
    the user. A follow-up like "and the cross-section?" is enough to trigger
    it, and for anyone who talks about this subject matter routinely that is
    most follow-ups.

    So auto-classification applies to the FIRST turn of a session only.
    Later turns stay in the standard loop, which is the one that carries
    history. An explicit ``/mode physics`` still wins on any turn — the user
    asking for it is unambiguous in a way a keyword match is not.
    """
    allowed = DEFAULT_ENABLED_MODES if enabled is None else tuple(enabled)
    if override and override != "auto":
        # A pin can outlive the config that allowed it: sessions persist, and
        # the mode is stored per session. Honour the current config, not the
        # one in force when the pin was set.
        return override if override in allowed else "standard"
    if not is_first_turn:
        return "standard"
    return classify_mode(text, allowed)
