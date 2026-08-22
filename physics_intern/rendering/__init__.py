"""Rendering subsystem: Markdown snapshots and shared context primitives.

Per-agent context renderers live next to each agent in
`src/physics_intern/agents/<name>/context.py`.
"""

from .shared import (
    _dedup_failed_approaches,
    _problem_guidelines,
    _render_sanity_checks,
    er_id_label,
    render_background_survey_xml,
    render_research_context_xml,
)
from .snapshots import (
    render_background_survey,
    render_critique_log_md,
    render_evidence_log_md,
    render_research_state_md,
)

__all__ = [
    "_dedup_failed_approaches",
    "_problem_guidelines",
    "_render_sanity_checks",
    "er_id_label",
    "render_background_survey",
    "render_background_survey_xml",
    "render_critique_log_md",
    "render_evidence_log_md",
    "render_research_context_xml",
    "render_research_state_md",
]
