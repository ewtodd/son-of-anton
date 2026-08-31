"""``son-of-anton problem`` subcommand parser.

Writing a physics problem spec by hand means listing every scored key, its
expected value and its tolerance, and keeping the task text in sync with all
three. The builder does that from a data directory and a one-line goal — but
it only helps if it is reachable, and a script under ``scripts/`` is not
shipped in the sealed wheel and is not on anyone's PATH.

The options come from ``physics_intern.spec_builder.add_arguments``, so this
subcommand and ``python -m physics_intern.spec_builder`` cannot drift apart.
"""

from __future__ import annotations

from typing import Callable

from physics_intern.run import MODES


def build_problem_parser(subparsers, *, cmd_problem: Callable) -> None:
    """Attach the ``problem`` subcommand to *subparsers*."""
    problem_parser = subparsers.add_parser(
        "problem",
        help="Create and inspect physics problem specs",
        description=(
            "Problem specs for the physics and research modes.\n\n"
            "A spec is the task text, the data paths to expose read-only, and "
            "the numeric checks that score the run's RESULTS.txt. Run one by "
            "handing its path to a physics or research turn."
        ),
    )
    problem_sub = problem_parser.add_subparsers(dest="problem_action")

    create = problem_sub.add_parser(
        "create",
        help="Generate a problem.yaml from a dataset and a one-line goal",
        description=(
            "Probe a dataset, then write a problem.yaml for it.\n\n"
            "The probe is deterministic and runs inside the same sandbox the "
            "run's computations will, with the data read-only — so what it "
            "reports is what the agent will be able to see. The model is given "
            "only that summary and your goal, and writes the task text; with "
            "--truth it does not choose the expected values either."
        ),
    )

    from physics_intern.spec_builder import add_arguments

    add_arguments(create)

    run = problem_sub.add_parser(
        "run",
        help="Run a problem spec and print its answer and score",
        description=(
            "Run a spec to completion and print ANSWER.md and FORMAL_EVAL.md.\n\n"
            "The same run a physics or research chat turn performs, without the "
            "chat: a spec is a batch job, and driving one by starting a session, "
            "pinning a mode and pasting a path is a worse way to say so."
        ),
    )
    run.add_argument("spec", help="Path to a problem.yaml (or a problem statement)")
    run.add_argument(
        "--mode",
        choices=list(MODES),
        default="physics",
        help=(
            "physics: one Research Manager with append-only memory (default). "
            "research: nine agents over a claim ledger with adversarial review."
        ),
    )
    run.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        dest="max_iterations",
        help=(
            "Cap the outer loop. Overrides physics.max_iterations. Physics mode "
            "has no wall-clock or cost gate, so this is the ceiling — start low."
        ),
    )
    run.add_argument(
        "--workspace",
        default=None,
        help=(
            "Run in this directory instead of a fresh one under "
            "physics.workspace_root. Point it at an existing workspace to "
            "continue that run."
        ),
    )

    problem_parser.set_defaults(func=cmd_problem)
