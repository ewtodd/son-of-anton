"""Loading the problem spec that a physics or research run is scored against.

Both modes are scored the same way: the run writes ``RESULTS.txt`` into its
workspace, and ``verification/experimental.py`` compares it against the numeric
``checks`` in a ``problem.yaml``. The evaluator reads that spec *from the
workspace*, so a run that never puts one there is silently never scored — it
finishes, prints an answer, and reports "Formal verification skipped".

That is exactly what research mode did. Its entry points passed the raw message
text and nothing else, nothing on the fresh-run path wrote a spec, and so the
checks never ran on a single research run. (``PhysicsIntern.resume`` also reads
that file, so those runs could not be resumed either.) The Autophysicist had
its own copy of the loading logic and did write the file, which is why the gap
showed up in one mode and not the other.

This module is that logic, once, for both.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SPEC_SUFFIXES = (".yaml", ".yml")


@dataclass
class ProblemSpec:
    """A run's problem statement, and the spec it is scored against if any."""

    text: str
    name: str = "session"
    definition: dict | None = None
    answer_template: str = ""
    _meta: dict = field(default_factory=dict)

    @property
    def meta(self) -> dict:
        """The subset the loop's termination gate reads."""
        if self._meta:
            return self._meta
        if not self.definition:
            return {}
        return {"steps": self.definition.get("steps", [])}

    @property
    def is_scored(self) -> bool:
        return bool((self.definition or {}).get("checks"))


def load_spec(message: str) -> ProblemSpec:
    """Read *message* as either a path to a problem spec, or the problem itself.

    A message naming a readable ``.yaml``/``.yml`` with a ``problem:`` key is
    loaded as a spec; anything else is the problem statement, and the run is
    unscored because there is nothing to score it against.

    ``~`` is expanded. It has to be: this is reached from a chat prompt, where
    ``~/runs/problem.yaml`` is what a person types, and ``Path`` does not expand
    it. Unexpanded, the tilde path is not a file, so it falls through to the
    prose branch and the run's problem statement becomes the literal string
    ``"~/runs/problem.yaml"`` — an expensive run answering nothing, scored
    against nothing, with no error anywhere.

    A message that looks like a spec path but cannot be loaded says so rather
    than quietly becoming the problem statement, for the same reason.
    """
    text = message.strip()
    candidate = Path(os.path.expanduser(text))
    looks_like_spec = (
        candidate.suffix.lower() in SPEC_SUFFIXES and "\n" not in text
    )
    try:
        is_spec_file = looks_like_spec and candidate.is_file()
    except OSError:
        is_spec_file = False
    if not is_spec_file:
        if looks_like_spec:
            warnings.warn(
                f"{text!r} looks like a problem spec but is not a readable "
                f"file ({candidate}). Running it as the problem statement "
                f"instead — the run will not be scored.",
                stacklevel=2,
            )
        return ProblemSpec(text=text)

    try:
        with open(candidate, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        warnings.warn(
            f"{candidate} could not be read as a problem spec ({exc}). "
            f"Running it as the problem statement instead — the run will not "
            f"be scored.",
            stacklevel=2,
        )
        return ProblemSpec(text=text)

    if not isinstance(loaded, dict) or "problem" not in loaded:
        warnings.warn(
            f"{candidate} has no 'problem:' key, so it is not a problem spec. "
            f"Running its path as the problem statement instead — the run will "
            f"not be scored.",
            stacklevel=2,
        )
        return ProblemSpec(text=text)

    return ProblemSpec(
        text=str(loaded.get("problem") or text),
        name=str(loaded.get("name") or candidate.stem),
        definition=loaded,
        answer_template=str(loaded.get("answer_template") or ""),
    )


def write_spec(workspace_root: Path | str, spec: ProblemSpec) -> bool:
    """Write the spec into the workspace so the evaluator can find it.

    Returns False when there is nothing to write. The name is normalized here
    rather than at the call sites, because ``resume`` and the evaluator both
    key off it.
    """
    if not spec.definition:
        return False
    data = dict(spec.definition)
    data["name"] = data.get("name") or spec.name
    path = Path(workspace_root) / "problem.yaml"
    with open(path, "w", encoding="utf-8") as handle:
        yaml.dump(data, handle, default_flow_style=False, sort_keys=False)
    return True
