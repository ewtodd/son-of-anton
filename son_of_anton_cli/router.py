"""Request router for son-of-anton.

Classifies a user request into an agent mode (standard / physics / research)
and a model slot (simple / default / complex). The design mirrors the
heuristic router from the archived temple harness: conservative, fast, no
model call, with the final word left to explicit user overrides.

All knobs live under the ``router`` section of config.yaml.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

AGENT_MODES = ("auto", "standard", "physics", "research")

# Modes a deployment may switch off. "standard" is not among them: it is the
# fallback every other path lands on, so it is always available.
OPTIONAL_MODES = ("physics", "research")

# Everything on, which is what an existing config.yaml with no `router.modes`
# key means.
DEFAULT_ENABLED_MODES = ("standard",) + OPTIONAL_MODES


def resolve_enabled_modes(raw: object) -> tuple[str, ...]:
    """Normalize ``router.modes`` into the set of selectable modes.

    A gateway serving a household group has no use for the physics or research
    loops, and listing them costs more than clutter: the router can route a
    message into a one-shot loop nobody there wants, and the modes show up in
    ``/mode`` as if they were on offer.

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

# Keyword signal for the critical self-research pipeline. Also specific:
# exploratory investigation, derivation, and literature-scale questions.
RESEARCH_KEYWORDS = (
    "from first principles",
    "open research question",
    "literature review",
    "derive the",
    "prove that",
    "prove the",
    "formal derivation",
    "theoretical framework",
    "survey the literature",
    "state of the art",
    "write a research",
    "research this",
    "investigate deeply",
    "scientific review",
)

COMPLEX_CODE_SIGNALS = (
    " code",
    " bug",
    "fix ",
    "implement ",
    "refactor",
    " rewrite",
    "rewrite ",
    " build",
    "building ",
    "compile",
    " debug",
    "debugging ",
    "commit ",
    "push ",
    " test",
    "testing ",
)

COMPLEX_SUBSTANCE_SIGNALS = (
    ".",
    "/",
    "(",
    "src",
    "fn ",
    "def ",
    "class ",
    "struct ",
    "import ",
    "cargo",
    "nix ",
    "flake",
    ".rs",
    ".py",
    ".nix",
    ".cpp",
    ".cxx",
    ".ts",
    ".js",
)


def classify_mode(
    text: str,
    enabled: Optional[Sequence[str]] = None,
) -> str:
    """Return ``physics``, ``research``, or ``standard`` for *text*.

    *enabled* limits what may be returned; a disabled mode is simply not
    classified into, so its keywords stop meaning anything. ``None`` means
    every mode.
    """
    allowed = DEFAULT_ENABLED_MODES if enabled is None else tuple(enabled)
    low = text.lower()
    if "physics" in allowed and any(k in low for k in PHYSICS_KEYWORDS):
        return "physics"
    if "research" in allowed and any(k in low for k in RESEARCH_KEYWORDS):
        return "research"
    return "standard"


def classify_complexity(text: str) -> str:
    """Return ``simple``, ``complex``, or ``default`` for *text*.

    Ported from temple's heuristic classifier: only classify the obvious
    cases, leave everything else at the middle tier.
    """
    q = text.lower()
    length = len(text.strip())

    if length < 15 or q in {
        "hi", "hey", "hello", "yo", "sup", "thanks", "thank you",
        "thx", "ty", "ok", "okay", "k", "kk", "status", "help",
        "ping", "lol", "nice", "cool",
    }:
        return "simple"

    greeting_prefixes = ("hello ", "hi ", "hey ", "thanks ", "thank you ")
    for prefix in greeting_prefixes:
        if q.startswith(prefix):
            rest = q[len(prefix):]
            if len(rest) < 30:
                return "simple"

    has_code_signal = any(signal in q for signal in COMPLEX_CODE_SIGNALS)
    has_substance = length > 40 or any(
        signal in q for signal in COMPLEX_SUBSTANCE_SIGNALS
    )
    if has_code_signal and has_substance:
        return "complex"

    return "default"


def resolve_mode(
    override: Optional[str],
    text: str,
    *,
    is_first_turn: bool = True,
    enabled: Optional[Sequence[str]] = None,
) -> str:
    """Resolve the agent mode for one request.

    *override* is the session's ``/mode`` pin (``None`` or ``"auto"`` means
    classify); the result is always one of the three concrete modes.

    *is_first_turn* gates keyword classification. physics/research are
    one-shot loops that receive ONLY the current message — no conversation
    history — so routing a mid-conversation turn into one silently drops
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


def resolve_model_slot(
    text: str,
    router_config: Optional[dict],
) -> str:
    """Return the router model slot for *text*: simple/default/complex."""
    if not router_config:
        return "default"
    if not router_config.get("enabled", True):
        return "default"
    return classify_complexity(text)
