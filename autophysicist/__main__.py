"""``python -m autophysicist problem``: the physics mode's own entry.

The Go ``son-of-anton problem`` hands the physics mode to this entry until its
Go port (daemon/phases/11). This is the mode's only command line.
"""

from __future__ import annotations

import argparse
import sys

USAGE = (
    "usage: son-of-anton problem create --data PATH --goal TEXT -o FILE\n"
    "       son-of-anton problem run SPEC\n"
    "Run 'son-of-anton problem <action> --help' for the full options."
)


def build_parser() -> argparse.ArgumentParser:
    from autophysicist.spec_builder import add_arguments

    parser = argparse.ArgumentParser(
        prog="son-of-anton",
        description="The physics mode of Son of Anton.",
    )
    sub = parser.add_subparsers(dest="command")
    problem = sub.add_parser(
        "problem",
        help="Create and inspect physics problem specs",
        description=(
            "Problem specs for the physics mode.\n\n"
            "A spec is the task text, the data paths to expose read-only, and "
            "the numeric checks that score the run's RESULTS.txt. Run one "
            "with `son-of-anton problem run SPEC`."
        ),
        epilog=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    actions = problem.add_subparsers(dest="problem_action")

    create = actions.add_parser(
        "create",
        help="Generate a problem.toml from a dataset and a one-line goal",
        description=(
            "Probe a dataset, then write a problem.toml for it.\n\n"
            "The probe is deterministic and runs inside the same sandbox the "
            "run's computations will, with the data read-only. The model is "
            "given only that summary and your goal, and writes the task text; "
            "with --truth it does not choose the expected values either."
        ),
    )
    add_arguments(create)

    run = actions.add_parser(
        "run",
        help="Run a problem spec and print its answer and score",
        description="Run a spec to completion and print ANSWER.md and FORMAL_EVAL.md.",
    )
    run.add_argument("spec", help="Path to a problem.toml (or a problem statement)")
    run.add_argument(
        "--max-iterations", type=int, default=None, dest="max_iterations",
        help="Cap the outer loop. Overrides physics.max_iterations. Physics mode "
        "has no wall-clock or cost gate, so this is the ceiling: start low.",
    )
    run.add_argument(
        "--script-timeout", type=int, default=None, dest="script_timeout",
        help="Seconds one model-authored script may run for (default 60). "
        "Overrides physics.script_timeout.",
    )
    run.add_argument(
        "--token-budget", type=int, default=None, dest="token_budget",
        help="Token spend cap for each iteration (default 64000). Overrides "
        "physics.token_budget.",
    )
    run.add_argument(
        "--workspace", default=None,
        help="Run in this directory instead of a fresh one under "
        "physics.workspace_root. Point it at an existing workspace to continue "
        "that run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # The model's key lives in the home's .env; the physics LLM layer reads it
    # from the environment.
    from autophysicist.support import load_dotenv

    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "problem":
        print(USAGE)
        return 2
    action = getattr(args, "problem_action", None)

    if action == "create":
        from autophysicist.spec_builder import run as run_spec_builder

        return run_spec_builder(args, parser)

    if action == "run":
        from autophysicist.run import render_report, run_problem

        workspace = run_problem(
            args.spec,
            max_iterations=args.max_iterations,
            script_timeout=args.script_timeout,
            token_budget=args.token_budget,
            workspace_root=args.workspace,
        )
        print(render_report(workspace))
        return 0

    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
