"""One critique per iteration, from outside the Manager's head.

The Autophysicist is a single agent that decides what to investigate, judges
its own sub-agents, and decides what is true — and its own system prompt names
that as the design's weak point ("you are the least reliable component",
"nothing is reliable until independently verified"). Those are norms with
nothing enforcing them, and the observed failure matches: iterations end with a
confident plan, an empty permanent memory, and the same environment facts
rediscovered next time.

This is the part of the original physics-intern design worth keeping: a
reviewer that is not the thing being reviewed. Deliberately the cheapest
possible version of it — one prompt, one answer, no tools, no state machine,
no verdict that gates anything. The critique goes into the next iteration's
context and the Manager does what it likes with it.

It is also the natural place for a slower, more knowledgeable model. One call
per iteration against a Manager that spends five rounds and several sub-agent
dispatches is a rounding error, so ``physics.agent_models.critic`` can point at
something that would be far too slow to run the loop with.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .core.console import console

MAX_CRITIQUE_CHARS = 4_000
HUMAN_VERDICTS = ("progress", "stalled", "wrong_approach", "needs_help")
HUMAN_DIFF_MAX_CHARS = 60_000
_FEEDBACK_MARKER = "# --- feedback below this line ---"


class Critique(str):
    """Critique text, with any stop request the human attached.

    Behaves as a plain string everywhere the Manager sees it: injection,
    logging, and the runner's truthiness checks are unchanged. ``block_next``
    is only read back by the runner.
    """

    block_next: bool = False

    def __new__(cls, text: str, *, block_next: bool = False) -> "Critique":
        instance = super().__new__(cls, text)
        instance.block_next = block_next
        return instance


def _runtime_description() -> str:
    """What the sandbox can actually import. Never raises."""
    try:
        from .utils.sandbox import describe_runtime, runtime_guidance

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

    ``config.critic_mode`` chooses who writes it — the model (default) or the
    human at the terminal. Both return the same kind of string, so the Manager
    cannot tell which one reviewed the iteration.
    """
    mode = str(getattr(config, "critic_mode", "model") or "model").strip().lower()
    if mode == "human":
        return run_human_critique(
            config=config,
            problem_text=problem_text,
            permanent_memory=permanent_memory,
            scratchpad=scratchpad,
            workspace_root=workspace_root,
            iteration=iteration,
            result=result,
        )
    return run_model_critique(
        config=config,
        problem_text=problem_text,
        permanent_memory=permanent_memory,
        scratchpad=scratchpad,
        workspace_root=workspace_root,
        iteration=iteration,
        result=result,
    )


def run_model_critique(
    config,
    problem_text: str,
    permanent_memory,
    scratchpad,
    workspace_root: Path,
    iteration: int,
    result,
) -> str:
    """The original path: one model call per iteration."""
    import copy

    from .llm import call_llm

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
        return Critique("")

    text = (response.text or "").strip()[:MAX_CRITIQUE_CHARS]
    source = f"model:{critic_config.model or 'unknown'}"
    if text:
        _append_log(workspace_root, iteration, text, source=source)
        _write_record(workspace_root, iteration, source=source, text=text)
    return Critique(text)


def run_human_critique(
    config,
    problem_text: str,
    permanent_memory,
    scratchpad,
    workspace_root: Path,
    iteration: int,
    result,
) -> str:
    """Render the iteration for a human, take feedback, log it like any critique.

    Attended runs only: without ``$EDITOR`` or a TTY the critique is skipped,
    the same fail-open contract as a model critic that errors.
    """
    del config  # human mode never consults the model registry
    try:
        review, diff = build_human_context(
            problem_text,
            permanent_memory,
            scratchpad,
            iteration,
            result,
            Path(workspace_root),
        )
        outcome = _interactive_human_review(review, diff, Path(workspace_root))
    except Exception as exc:  # noqa: BLE001 — advice is never worth the run
        console.print(
            f"[yellow]Human critic failed ({type(exc).__name__}) — continuing[/]"
        )
        return Critique("")

    if outcome is None:
        return Critique("")

    text, verdict, block_next = outcome
    text = (text or "").strip()[:MAX_CRITIQUE_CHARS]
    if not text and verdict is None and not block_next:
        return Critique("")
    if text:
        _append_log(workspace_root, iteration, text, source="human")
    _write_record(
        workspace_root,
        iteration,
        source="human",
        text=text,
        verdict=verdict,
        block_next=block_next,
        diff_text=diff,
    )
    return Critique(text, block_next=block_next)


def build_human_context(
    problem_text: str,
    permanent_memory,
    scratchpad,
    iteration: int,
    result,
    workspace_root: Path,
) -> tuple[str, str]:
    """The review bundle a human sees. Returns ``(review_text, diff_text)``.

    ``diff_text`` is hashed into the critique record so model and human
    critiques of the same iteration can be paired later.
    """
    del permanent_memory, scratchpad  # the human has the workspace itself
    diff = _workspace_diff(workspace_root)
    lines = [
        f"# Iteration {iteration} review",
        "",
        f"Workspace: {workspace_root}",
        "",
        "## Problem",
        problem_text.strip(),
        "",
        "## What the Manager did this iteration",
        _render_activity(result),
        "",
        "## git status + diff (uncommitted)",
        diff or "(no tracked changes)",
    ]
    return "\n".join(lines), diff


def _workspace_diff(workspace_root: Path) -> str:
    """``git status --short`` plus ``git diff HEAD``, best-effort and capped."""

    def run_git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=30,
        )

    try:
        status = run_git("status", "--short")
        diff = run_git("diff", "HEAD")
        if diff.returncode != 0:
            diff = run_git("diff")
        parts = []
        if status.stdout.strip():
            parts.append("git status --short")
            parts.append(status.stdout.rstrip())
        if diff.stdout.strip():
            parts.append("git diff")
            parts.append(diff.stdout.rstrip())
        text = "\n\n".join(parts)
    except Exception:
        return ""
    if len(text) > HUMAN_DIFF_MAX_CHARS:
        text = (
            text[:HUMAN_DIFF_MAX_CHARS]
            + "\n\n[diff truncated — open the workspace for the rest]"
        )
    return text


def _interactive_human_review(
    review: str, diff: str, workspace_root: Path
) -> tuple[str, str | None, bool] | None:
    """Get human feedback via ``$EDITOR``, or line prompts, or skip.

    Returns ``None`` when there is no way to ask — human mode is for attended
    runs, and skipping is the same fail-open contract as a failed model critic.
    """
    editor = (
        os.environ.get("VISUAL", "").strip()
        or os.environ.get("EDITOR", "").strip()
    )
    if editor and shutil.which(shlex.split(editor)[0]):
        return _editor_review(review, diff, workspace_root, editor)
    if sys.stdin.isatty():
        return _prompt_review(review, diff, workspace_root)
    console.print(
        "[yellow]Human critic: no $EDITOR and no TTY — skipping this critique[/]"
    )
    return None


def _editor_review(
    review: str, diff: str, workspace_root: Path, editor: str
) -> tuple[str, str | None, bool]:
    """Open ``$EDITOR`` on the review bundle and parse the response back."""
    scaffold = "\n".join(
        [
            review,
            "",
            "## git diff",
            diff or "(no tracked changes)",
            "",
            "## How to answer",
            "# Write feedback below the marker. Lines starting with '#' are ignored.",
            "# The Manager sees the feedback exactly as if the model critic wrote it.",
            "",
            _FEEDBACK_MARKER,
            "# feedback:",
            "",
            "# verdict (one of: progress, stalled, wrong_approach, needs_help):",
            "verdict = ",
            "# block the next iteration? (y/n):",
            "block_next = ",
            "",
        ]
    )
    fd, tmp_path = tempfile.mkstemp(prefix="soa-critique-", suffix=".md")
    os.close(fd)
    try:
        Path(tmp_path).write_text(scaffold, encoding="utf-8")
        subprocess.run(shlex.split(editor) + [tmp_path], check=True)
        edited = Path(tmp_path).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return _parse_human_response(edited)


def _prompt_review(
    review: str, diff: str, workspace_root: Path
) -> tuple[str, str | None, bool]:
    """Line-based fallback when there is a TTY but no usable editor."""
    console.print(review)
    if diff:
        console.print(diff)
    console.print(
        f"Workspace: {workspace_root} — enter feedback one line at a time; "
        "an empty line ends it."
    )
    feedback_lines = []
    while True:
        try:
            line = console.input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not line.strip():
            break
        feedback_lines.append(line)
    verdict_raw = console.input(
        "Verdict (progress/stalled/wrong_approach/needs_help, empty to skip): "
    ).strip().lower()
    verdict = verdict_raw if verdict_raw in HUMAN_VERDICTS else None
    block_raw = console.input("Block the next iteration? [y/N]: ").strip().lower()
    block_next = block_raw in ("y", "yes")
    return "\n".join(feedback_lines).strip(), verdict, block_next


def _parse_human_response(edited: str) -> tuple[str, str | None, bool]:
    """Split one editor buffer into ``(feedback, verdict, block_next)``."""
    if _FEEDBACK_MARKER in edited:
        body = edited.split(_FEEDBACK_MARKER, 1)[1]
    else:
        body = edited
    verdict = None
    block_next = False
    feedback_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("verdict") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip().lower()
            if value in HUMAN_VERDICTS:
                verdict = value
            continue
        if lowered.startswith("block_next") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip().lower()
            block_next = value in ("y", "yes", "true", "1")
            continue
        if stripped.startswith("#"):
            continue
        feedback_lines.append(line)
    return "\n".join(feedback_lines).strip(), verdict, block_next


def _sha256(text: str) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_record(
    workspace_root: Path,
    iteration: int,
    *,
    source: str,
    text: str,
    verdict: str | None = None,
    block_next: bool | None = None,
    diff_text: str | None = None,
) -> None:
    """Append one critique record for later analysis. Never raises."""
    record = {
        "run": Path(workspace_root).name,
        "iteration": iteration,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "verdict": verdict,
        "block_next": block_next,
        "diff_sha256": _sha256(diff_text or ""),
        "text_sha256": _sha256(text),
        "text": text,
    }
    try:
        with open(
            Path(workspace_root) / "CRITIQUE_DATA.jsonl", "a", encoding="utf-8"
        ) as handle:
            print(json.dumps(record, ensure_ascii=False), file=handle)
    except OSError:
        pass


def _append_log(
    workspace_root: Path, iteration: int, text: str, *, source: str = "model"
) -> None:
    """Persist every critique; only the latest is shown to the Manager."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with open(
            Path(workspace_root) / "CRITIQUE_LOG.md", "a", encoding="utf-8"
        ) as handle:
            handle.write(
                f"\n## Iteration {iteration} — {stamp} — critic: {source}\n\n{text}\n"
            )
    except OSError:
        pass
