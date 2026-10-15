"""``/plan`` — write an implementation plan to a file instead of doing the work.

Like ``/commit``, this is a prompt handed to the agent as an ordinary user
turn, not a tool: the agent inspects the project with the tools it
already has and writes the plan file itself. The terminal queues the prompt as
the next turn; the gateway rewrites the incoming message to it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def build_plan_prompt(task: str = "", *, now: Optional[datetime] = None) -> str:
    """The turn ``/plan`` hands to the agent, on any surface."""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    lines = [
        "Plan only: write an implementation plan and do not carry it out.",
        "- Edit no file except the plan, and run nothing that changes anything "
        "(no installs, writes, commits, pushes or messages to anyone). Read-only "
        "inspection (reading files, searching, git log/diff/status) is expected.",
        f"- Save the plan as .son-of-anton/plans/{stamp}-<slug>.md in the project it "
        "is about: the current working directory unless the task names another "
        "project. <slug> is a few lowercase words joined by hyphens.",
        "- Make it concrete enough for someone with no context to carry out: the "
        "goal; what you found about the current state; the approach and why; "
        "numbered steps; the files likely to change, by exact path; how to test "
        "and verify each step; risks, tradeoffs and open questions.",
        "- If the task is too unclear to plan, ask one short question instead of "
        "guessing.",
        "- Once it is saved, reply briefly: what the plan does, its main steps, and "
        "the file's path.",
    ]
    task = (task or "").strip()
    if task:
        lines.append(f"The task to plan: {task}")
    else:
        lines.append("No task was given: plan the work this conversation is heading toward.")
    return "\n".join(lines)
