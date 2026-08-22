"""Shared Rich console with optional tee-to-file logging.

Also hosts pure printing helpers (progress callbacks, task summaries,
final report) used both directly from the engine loop and as callbacks
passed into agents.
"""

from __future__ import annotations

import atexit
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.text import Text

if TYPE_CHECKING:
    from ..state.task import Task
    from .config import Config
    from .metrics import MetricsTracker


class LoggingConsole(Console):
    """Console that optionally tees output to a log file."""

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self._log_console: Console | None = None
        self._log_file = None

    def setup_log(self, path: str | Path) -> None:
        """Start tee-ing to *path* (append mode). Idempotent."""
        if self._log_console is not None:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(path, "a", encoding="utf-8")  # noqa: SIM115
        self._log_console = Console(
            file=self._log_file,
            force_terminal=True,
            width=120,
            color_system="standard",
        )
        atexit.register(self._close_log)

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
            self._log_console = None

    def print(self, *args: Any, **kwargs: Any) -> None:
        super().print(*args, **kwargs)
        if self._log_console is not None:
            self._log_console.print(*args, **kwargs)

    def rule(self, *args: Any, **kwargs: Any) -> None:
        super().rule(*args, **kwargs)
        if self._log_console is not None:
            self._log_console.rule(*args, **kwargs)


def replay_log(path: str | Path, tail: int | None = 50) -> None:
    """Print the last *tail* lines of a console log to stdout.

    Pass ``tail=None`` to replay the entire file.
    """
    path = Path(path)
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return
    show = lines if tail is None else lines[-tail:]
    sys.stdout.write(
        "\033[2m--- replaying %d lines of console log ---\033[0m\n" % len(show)
    )
    for line in show:
        sys.stdout.write(line + "\n")
    sys.stdout.write("\033[2m--- end of replay ---\033[0m\n")
    sys.stdout.flush()


# Module-level singleton — every module imports this.
console = LoggingConsole()


def fmt_duration(seconds: float) -> str:
    """Format a duration as e.g. '6.3s' or '2m05s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def print_task(task: Task) -> None:
    """Print a one-line task summary to the console."""
    text = Text()
    text.append("Task: ", style="bold")
    text.append(f"{task.task_id} ", style="cyan")
    text.append(f"[{task.task_type}] ", style="yellow")
    if task.target_claim:
        text.append(f"{task.target_claim} ", style="bold magenta")
    text.append(f"-> {task.assigned_to}", style="green")
    console.print(text)


def on_round_progress(
    round_num,
    stop_reason,
    tool_calls,
    total_input,
    total_output,
    round_input,
    round_output,
    round_duration,
    round_reasoning=0,
    round_answer=0,
) -> None:
    """Progress callback for agent tool-use rounds."""
    if round_reasoning:
        tokens = f"{round_input:,}in + {round_output:,}out ({round_reasoning:,}r + {round_answer:,}a)"
    else:
        tokens = f"{round_input:,}in + {round_output:,}out"
    tps = f"{round_output / round_duration:,.0f} t/s" if round_duration > 0 else ""
    dur = fmt_duration(round_duration)
    detail = f"{tokens}, {dur}"
    if tps:
        detail += f", {tps}"
    if stop_reason == "forced_partial":
        console.print(
            f"  round {round_num}: forced final call ({detail})", style="dim magenta"
        )
        return
    n_tools = len(tool_calls)
    errors = sum(1 for tc in tool_calls if tc.is_error)
    if errors:
        status = f"{n_tools} tool call{'s' if n_tools != 1 else ''}, {errors} error{'s' if errors != 1 else ''}"
    else:
        status = f"{n_tools} tool call{'s' if n_tools != 1 else ''}"
    console.print(f"  round {round_num}: {status} ({detail})", style="dim magenta")


def print_call_summary(result) -> None:
    """Print a one-line timing/token summary for one-shot LLM calls."""
    from ..llm import (
        AgentResult,
        LLMResponse,
    )  # deferred — llm imports `console` from this module

    if isinstance(result, AgentResult):
        out = result.total_output_tokens
        reasoning = result.total_reasoning_tokens
        answer = result.total_answer_tokens
        tokens = f"{result.total_input_tokens:,}in + {out:,}out"
        if reasoning:
            tokens += f" ({reasoning:,}r + {answer:,}a)"
    elif isinstance(result, LLMResponse):
        out = result.output_tokens
        reasoning = result.reasoning_tokens
        answer = result.answer_tokens
        tokens = f"{result.input_tokens:,}in + {out:,}out"
        if reasoning:
            tokens += f" ({reasoning:,}r + {answer:,}a)"
    else:
        return
    dur = fmt_duration(result.duration)
    tps = f", {out / result.duration:,.0f} t/s" if result.duration > 0 else ""
    console.print(f"  ({tokens}, {dur}{tps})", style="dim")


def print_iteration_summary(
    iteration: int,
    metrics: MetricsTracker,
    config: Config,
    elapsed_seconds: float,
) -> None:
    """One-line dim summary printed at the end of each iteration."""
    parts = [
        f"iter {iteration} totals:",
        f"{metrics.total_output_tokens:,} out tokens",
    ]
    if config.input_cost or config.output_cost:
        cost = (
            metrics.total_input_tokens * config.input_cost
            + metrics.total_output_tokens * config.output_cost
        ) / 1_000_000
        parts.append(f"${cost:.2f}")
    parts.append(f"{fmt_duration(elapsed_seconds)} elapsed")
    console.print(f"[dim]{', '.join(parts)}[/dim]")


def print_final_report(
    iteration: int,
    metrics: MetricsTracker,
    config: Config,
    workspace_root,
) -> None:
    """Print the end-of-run summary (iterations, tokens, cost, alerts)."""
    console.rule("[bold green]SESSION COMPLETE[/bold green]")
    console.print(f"Total iterations: {iteration}")
    console.print(f"Total LLM calls: {len(metrics.calls)}")
    console.print(f"Total input tokens: {metrics.total_input_tokens:,}")
    console.print(f"Total output tokens: {metrics.total_output_tokens:,}")
    if config.input_cost or config.output_cost:
        cost = (
            metrics.total_input_tokens * config.input_cost
            + metrics.total_output_tokens * config.output_cost
        ) / 1_000_000
        console.print(f"Estimated cost: ${cost:.2f}")
    console.print(f"Workspace: {workspace_root.resolve()}")
    if metrics.alerts:
        console.print(f"\n[yellow]Alerts ({len(metrics.alerts)}):[/yellow]")
        for a in metrics.alerts[-5:]:
            console.print(f"  [iter {a['iteration']}] {a['message']}")
