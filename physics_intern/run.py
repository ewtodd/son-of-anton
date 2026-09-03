"""Run one problem spec, in the physics mode.

The chat turn (``cli._run_problem_mode``), the gateway
(``gateway.run._run_physics_mode_sync``), and ``son-of-anton problem run`` all
go through here, so a spec runs the same way from a shell as from a chat.

``max_iterations`` is settable here for the first time. The Autophysicist
took a hardcoded 50, and physics mode has no wall-clock or cost gate, so an
unattended run had no ceiling you could set short of editing the source. At
roughly ten seconds a call and up to fifteen tool calls an iteration, that
is the difference between a test and an afternoon.
"""

from __future__ import annotations

from pathlib import Path

from .core.problem_spec import ProblemSpec, load_spec

MODES = ("physics",)


def _physics_config() -> dict:
    try:
        from son_of_anton_cli.config import load_config

        section = (load_config() or {}).get("physics")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _resolve_limit(explicit: int | None, key: str, default: int) -> int:
    """CLI flag, then ``physics.<key>``, then the built-in default."""
    if explicit is not None and explicit > 0:
        return explicit
    try:
        configured = int(_physics_config().get(key) or 0)
    except (TypeError, ValueError):
        configured = 0
    return configured if configured > 0 else default


def resolve_max_iterations(explicit: int | None, default: int) -> int:
    """How many outer-loop iterations a run may take."""
    return _resolve_limit(explicit, "max_iterations", default)


def resolve_script_timeout(explicit: int | None) -> int:
    """Wall-clock seconds one model-authored script may run for.

    The default of 60 came from a scaffold built for symbolic work, where a
    script that runs a minute is stuck. On experimental data it is the opposite
    problem: reading a few hundred thousand waveforms out of a multi-GB file and
    training on them is legitimate work that does not fit in a minute, and a
    timeout there reads to the agent as "my approach was wrong" — so it retries
    something smaller instead of the thing that would have worked.
    """
    return _resolve_limit(explicit, "script_timeout", 60)


def run_problem(
    message: str,
    *,
    mode: str = "physics",
    max_iterations: int | None = None,
    script_timeout: int | None = None,
    workspace_root: Path | str | None = None,
    spec: ProblemSpec | None = None,
) -> Path:
    """Run *message* — a spec path or a problem statement — and return the workspace.

    The spec, when there is one, is written into the workspace: that is where
    the formal evaluation reads it from.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    spec = spec if spec is not None else load_spec(message)

    from .autophysicist.runner import run_autophysicist

    return run_autophysicist(
        problem_text=spec.text,
        answer_template=spec.answer_template,
        problem_def=spec.definition,
        problem_name=spec.name,
        max_iterations=resolve_max_iterations(max_iterations, 50),
        sandbox_timeout=resolve_script_timeout(script_timeout),
        workspace_root=workspace_root,
    )


def render_report(workspace: Path | str, mode: str = "physics") -> str:
    """The run's answer and score, for a chat reply or a terminal."""
    workspace = Path(workspace)
    lines = [f"{mode} run complete. Workspace: {workspace}"]
    for name in ("ANSWER.md", "FORMAL_EVAL.md"):
        report = workspace / name
        if report.exists():
            lines.append("")
            lines.append(report.read_text(encoding="utf-8").strip())
    return "\n".join(lines)
