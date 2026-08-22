"""State-transition bookkeeping for ResearchState.

WH↔ER renames need paired updates to the hypothesis dict key, the entity's
id/status fields, and any stale depends_on / resolved_to backlinks. These
helpers keep the pair atomic. ResearchState itself is a passive data container.
"""

from __future__ import annotations

from .research_state import HypothesisStatus, ResearchState


def demote_hypothesis(state: ResearchState, hid: str) -> str | None:
    """Demote ER→WH: update status, rename key, fix references.

    Returns the new ID (e.g. 'WH-002') or None if hid not found / not ER.
    Caller is responsible for any further status changes (e.g. marking
    the demoted hypothesis ABANDONED) and for stamping iteration_modified.
    """
    if hid not in state.hypotheses or not hid.startswith("ER-"):
        return None
    num = hid.split("-")[1]
    new_id = f"WH-{num}"
    h = state.hypotheses.pop(hid)
    h.id = new_id
    h.status = HypothesisStatus.WORKING
    state.hypotheses[new_id] = h
    normalize_references(state)
    return new_id


def promote_hypothesis(state: ResearchState, wh_id: str, iteration: int) -> str | None:
    """Promote WH→ER: update status, rename key, fix references, stamp iteration.

    Returns the new ID (e.g. 'ER-002') or None if wh_id not found / not WH.
    """
    if wh_id not in state.hypotheses or not wh_id.startswith("WH-"):
        return None
    num = wh_id.split("-")[1]
    new_id = f"ER-{num}"
    h = state.hypotheses.pop(wh_id)
    h.id = new_id
    h.status = HypothesisStatus.ESTABLISHED
    h.iteration_modified = iteration
    state.hypotheses[new_id] = h
    normalize_references(state)
    return new_id


def normalize_references(state: ResearchState) -> None:
    """Normalize hypothesis references after ID changes (promote/demote).

    When auto-promotion or demotion safety renames WH-002 → ER-002 (or
    vice versa), depends_on and resolved_to references may become stale.
    This function fixes those backlinks by mapping the numeric suffix to
    the current hypothesis ID.
    """
    id_by_num: dict[str, str] = {}
    for hid in state.hypotheses:
        parts = hid.split("-")
        if len(parts) == 2:
            id_by_num[parts[1]] = hid

    for h in state.hypotheses.values():
        h.depends_on = [
            id_by_num.get(dep.split("-")[1], dep)
            if "-" in dep and dep.split("-")[0] in ("WH", "ER")
            else dep
            for dep in h.depends_on
        ]

    for rq in state.research_questions.values():
        rq.resolved_to = [
            id_by_num.get(ref.split("-")[1], ref)
            if "-" in ref and ref.split("-")[0] in ("WH", "ER")
            else ref
            for ref in rq.resolved_to
        ]
