"""Run one problem spec, in either mode.

Both entry points did this already — ``cli._run_physics_mode`` /
``_run_research_mode`` and the gateway's ``_run_physics_mode_sync`` — as four
near-copies that had already drifted once (research mode dropped the spec
entirely, so its runs were never scored). This is that logic once, and it is
also what ``son-of-anton problem run`` calls, so a spec runs the same way from
a shell as from a chat turn.

``max_iterations`` is settable here for the first time. The Autophysicist took
a hardcoded 50, and physics mode has none of the wall-clock or cost gates
research mode has, so an unattended run had no ceiling you could set short of
editing the source. At roughly ten seconds a call and up to fifteen tool calls
an iteration, that is the difference between a test and an afternoon.
"""

from __future__ import annotations

from pathlib import Path

from .core.problem_spec import ProblemSpec, load_spec

MODES = ("physics", "research")


def _physics_config() -> dict:
    try:
        from son_of_anton_cli.config import load_config

        section = (load_config() or {}).get("physics")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def resolve_max_iterations(explicit: int | None, default: int) -> int:
    """CLI flag, then ``physics.max_iterations``, then the mode's default."""
    if explicit is not None and explicit > 0:
        return explicit
    configured = _physics_config().get("max_iterations")
    try:
        configured = int(configured or 0)
    except (TypeError, ValueError):
        configured = 0
    return configured if configured > 0 else default


def run_problem(
    message: str,
    *,
    mode: str = "physics",
    max_iterations: int | None = None,
    workspace_root: Path | str | None = None,
    spec: ProblemSpec | None = None,
) -> Path:
    """Run *message* — a spec path or a problem statement — and return the workspace.

    The spec, when there is one, is written into the workspace: that is where
    the formal evaluation reads it from and where ``resume`` picks it up.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    spec = spec if spec is not None else load_spec(message)

    if mode == "physics":
        from .autophysicist.runner import run_autophysicist

        return run_autophysicist(
            problem_text=spec.text,
            answer_template=spec.answer_template,
            problem_def=spec.definition,
            problem_name=spec.name,
            max_iterations=resolve_max_iterations(max_iterations, 50),
            workspace_root=workspace_root,
        )

    from .core.config import build_config
    from .core.models import resolve_models
    from .core.workspace import resolve_workspace_root
    from .engine import PhysicsIntern

    config = build_config(None)
    resolve_models(config)
    config.max_iterations = resolve_max_iterations(
        max_iterations, config.max_iterations
    )
    # An explicit, absolute workspace is mandatory: Config.workspace_dir
    # defaults to "" -> Path(".") -> the process cwd, and WorkspaceManager.init
    # runs `git init && git add -A && git commit` in it.
    config.workspace_dir = str(
        Path(workspace_root).expanduser().resolve()
        if workspace_root
        else resolve_workspace_root(spec.name, config.model, "research")
    )
    engine = PhysicsIntern(
        spec.text,
        config=config,
        answer_template=spec.answer_template,
        problem_def=spec.definition,
    )
    engine.run()
    return engine.workspace.root


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
