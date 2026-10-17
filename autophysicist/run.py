"""Run one problem spec, in the physics mode.

``son-of-anton problem run`` goes through here (and an agent in a chat
session drives a run by calling that subcommand through its terminal), so a
spec runs the same way from a shell as from a chat.

Physics mode has no wall-clock or cost gate, so ``max_iterations`` is the
ceiling on an unattended run. At roughly ten seconds a call and up to fifteen
tool calls an iteration, that is the difference between a test and an
afternoon.
"""

from __future__ import annotations

from pathlib import Path

from .core.config import physics_config as _physics_config
from .core.problem_spec import load_spec


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


def resolve_token_budget(explicit: int | None) -> int:
    """Token spend cap for each iteration.

    Charged as new input plus output tokens across the Manager and its
    sub-agents (a re-sent or cached prompt prefix counts once); see
    ``ManagerToolExecutor``. It is not a context-window size. The default of
    64000 suits short symbolic problems; experimental-data runs need more.
    """
    return _resolve_limit(explicit, "token_budget", 64000)


def run_problem(
    message: str,
    *,
    max_iterations: int | None = None,
    script_timeout: int | None = None,
    token_budget: int | None = None,
    workspace_root: Path | str | None = None,
) -> Path:
    """Run *message* — a spec path or a problem statement — and return the workspace.

    The spec, when there is one, is written into the workspace: that is where
    the formal evaluation reads it from.
    """
    spec = load_spec(message)

    from .runner import run_autophysicist

    return run_autophysicist(
        problem_text=spec.text,
        answer_template=spec.answer_template,
        problem_def=spec.definition,
        problem_name=spec.name,
        max_iterations=resolve_max_iterations(max_iterations, 50),
        sandbox_timeout=resolve_script_timeout(script_timeout),
        token_budget=resolve_token_budget(token_budget),
        workspace_root=workspace_root,
    )


def render_report(workspace: Path | str) -> str:
    """The run's answer and score, for a chat reply or a terminal."""
    workspace = Path(workspace)
    lines = [f"physics run complete. Workspace: {workspace}"]
    for name in ("ANSWER.md", "FORMAL_EVAL.md"):
        report = workspace / name
        if report.exists():
            lines.append("")
            lines.append(report.read_text(encoding="utf-8").strip())
    return "\n".join(lines)
