"""One critique per iteration, from outside the Manager's head.

The Autophysicist is a single agent that decides what to investigate, judges
its own sub-agents, and decides what is true — and its own system prompt names
that as the design's weak point ("you are the least reliable component",
"nothing is reliable until independently verified"). Those are norms with
nothing enforcing them, and the observed failure matches: iterations end with a
confident plan, an empty permanent memory, and the same environment facts
rediscovered next time.

This is the one part of the nine-agent pipeline worth keeping: a reviewer that
is not the thing being reviewed. Deliberately the cheapest possible version of
it — one prompt, one answer, no tools, no state machine, no verdict that gates
anything. The critique goes into the next iteration's context and the Manager
does what it likes with it.

It is also the natural place for a slower, more knowledgeable model. One call
per iteration against a Manager that spends five rounds and several sub-agent
dispatches is a rounding error, so ``physics.agent_models.critic`` can point at
something that would be far too slow to run the loop with.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..core.console import console

MAX_CRITIQUE_CHARS = 4_000


def _runtime_description() -> str:
    """What the sandbox can actually import. Never raises."""
    try:
        from ..utils.sandbox import describe_runtime, runtime_guidance

        parts = [describe_runtime()]
        guidance = runtime_guidance()
        if guidance:
            parts.append(guidance)
        return "\n\n".join(parts)
    except Exception:
        return ""


def _load_prompt() -> str:
    return (Path(__file__).parent / "critic_prompt.md").read_text(encoding="utf-8")


def _render_activity(result) -> str:
    """What the iteration actually did, as the critic sees it."""
    if result is None or not getattr(result, "tool_calls", None):
        return "(no tool calls — the iteration produced nothing)"
    lines = []
    for call in result.tool_calls:
        status = "ERROR" if call.is_error else "ok"
        body = (call.output or "").strip().replace("\n", " ")
        lines.append(f"- {call.tool_name} [{status}]: {body[:400]}")
    return "\n".join(lines)


def build_context(
    problem_text: str,
    permanent_memory,
    scratchpad,
    iteration: int,
    result,
    runtime: str = "",
) -> str:
    """Assemble what the critic sees.

    *runtime* matters more than it looks. Without it the critic advised
    `import uproot` — a package this runtime deliberately does not ship — the
    Manager copied that into a sub-agent brief, and the sub-agent wrote it
    three times because an explicit instruction beats general guidance. A
    reviewer who does not know what is installed invents work that cannot run.
    """
    memory = permanent_memory.read_full().strip()
    runtime_block = (
        f"<execution_environment>\nCode written by this system runs under: "
        f"{runtime}\nDo not suggest a package that is not in that list.\n"
        f"</execution_environment>\n\n"
        if runtime
        else ""
    )
    return (
        f"# Iteration {iteration} just finished\n\n"
        f"<problem_statement>\n{problem_text.strip()}\n</problem_statement>\n\n"
        f"{runtime_block}"
        f"<permanent_memory>\n"
        f"{memory if memory and memory != '# Permanent Memory' else '(EMPTY — nothing has been recorded as established.)'}\n"
        f"</permanent_memory>\n\n"
        f"<scratchpad>\n{scratchpad.read_window().strip() or '(empty)'}\n</scratchpad>\n\n"
        f"<what_the_manager_did_this_iteration>\n{_render_activity(result)}\n"
        f"</what_the_manager_did_this_iteration>\n\n"
        "Write your critique."
    )


def run_critique(
    config,
    problem_text: str,
    permanent_memory,
    scratchpad,
    workspace_root: Path,
    iteration: int,
    result,
) -> str:
    """Critique one iteration. Returns "" on any failure — never raises.

    A critic that can take the run down with it is worse than no critic: this
    is advice, and advice is not worth an aborted run.
    """
    import copy

    from ..llm import call_llm

    critic_config = copy.copy(config)
    critic_config.model = config.model_for_agent("critic")
    try:
        response = call_llm(
            system=_load_prompt(),
            user_content=build_context(
                problem_text,
                permanent_memory,
                scratchpad,
                iteration,
                result,
                runtime=_runtime_description(),
            ),
            config=critic_config,
            agent_name="critic",
            iteration=iteration,
        )
    except Exception as exc:  # noqa: BLE001 — advice is never worth the run
        console.print(f"[yellow]Critic failed ({type(exc).__name__}) — continuing[/]")
        return ""

    text = (response.text or "").strip()[:MAX_CRITIQUE_CHARS]
    if text:
        _append_log(workspace_root, iteration, text)
    return text


def _append_log(workspace_root: Path, iteration: int, text: str) -> None:
    """Persist every critique; only the latest is shown to the Manager."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with open(
            Path(workspace_root) / "CRITIQUE_LOG.md", "a", encoding="utf-8"
        ) as handle:
            handle.write(f"\n## Iteration {iteration} — {stamp}\n\n{text}\n")
    except OSError:
        pass
