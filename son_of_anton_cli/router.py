"""Request router for son-of-anton.

Classifies a user request into an agent mode (standard / physics / research)
and a model slot (simple / default / complex). The design mirrors the
heuristic router from the archived temple harness: conservative, fast, no
model call, with the final word left to explicit user overrides.

All knobs live under the ``router`` section of config.yaml.
"""

from __future__ import annotations

from typing import Optional

AGENT_MODES = ("auto", "standard", "physics", "research")

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


def classify_mode(text: str) -> str:
    """Return ``physics``, ``research``, or ``standard`` for *text*."""
    low = text.lower()
    if any(keyword in low for keyword in PHYSICS_KEYWORDS):
        return "physics"
    if any(keyword in low for keyword in RESEARCH_KEYWORDS):
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


def resolve_mode(override: Optional[str], text: str) -> str:
    """Resolve the agent mode for one request.

    *override* is the session's ``/mode`` pin (``None`` or ``"auto"`` means
    classify); the result is always one of the three concrete modes.
    """
    if override and override != "auto":
        return override
    return classify_mode(text)


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
