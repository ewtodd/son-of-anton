"""Formatter agent: produces clean ANSWER.md from final research state."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING

from physics_intern.llm import LLMResponse

from ..base import BaseAgent
from .context import render_formatter_context

if TYPE_CHECKING:
    from physics_intern.core.config import Config
    from physics_intern.core.metrics import MetricsTracker
    from physics_intern.state.research_state import ResearchState
    from physics_intern.state.task import Task
    from physics_intern.core.workspace import WorkspaceManager

_REJECTION_PREFIX = "FORMATTER_REJECTION:"
_FORCED_PROMPT_FILE = "prompt_forced.md"


class FormatterAgent(BaseAgent):
    name = "formatter"
    prompt_file = "prompt.md"

    def __init__(
        self,
        config: Config,
        workspace: WorkspaceManager,
        metrics: MetricsTracker,
        answer_template: str = "",
    ):
        super().__init__(config, workspace, metrics)
        self.answer_template = answer_template
        self.research_state: ResearchState | None = None
        self.rejection_reason: str | None = None
        self.best_effort: bool = False
        # Periodic best-guess snapshots redirect output to BEST_GUESS.md.
        # The exit-path forced formatter keeps the default ANSWER.md.
        self.output_filename: str = "ANSWER.md"
        self._system_prompt_forced: str | None = None

    @property
    def system_prompt(self) -> str:
        if self.best_effort:
            if self._system_prompt_forced is None:
                subclass_dir = Path(inspect.getfile(type(self))).parent
                self._system_prompt_forced = (
                    subclass_dir / _FORCED_PROMPT_FILE
                ).read_text()
            return self._system_prompt_forced
        return super().system_prompt

    def build_context(self, task: Task, iteration: int) -> str:
        # If instance-level template override exists, temporarily set on research_state
        # so the renderer emits it (renderer reads state.answer_template)
        if (
            self.answer_template
            and self.research_state
            and not self.research_state.answer_template
        ):
            self.research_state.answer_template = self.answer_template
        return render_formatter_context(
            self.research_state,
            answer_ers=task.answer_ers,
            best_effort=self.best_effort,
        )

    def process_response(self, response: LLMResponse, task: Task, iteration: int):
        """Write ANSWER.md, checking for LLM-emitted rejection marker.

        If the LLM emits a ``FORMATTER_REJECTION:`` line, no file is
        written — the rejection text would otherwise be committed as
        ANSWER.md (and block resume via the canonical-completion-signal
        contract).
        """
        self.rejection_reason = None
        text = response.text or ""

        if response.stop_reason == "max_tokens":
            self.rejection_reason = (
                "formatter hit max_tokens before producing a complete answer"
            )
            return

        if not text.strip():
            self.rejection_reason = "formatter produced an empty answer"
            return

        if text.lstrip().startswith(_REJECTION_PREFIX):
            self.rejection_reason = (
                text.lstrip().split("\n", 1)[0].removeprefix(_REJECTION_PREFIX).strip()
            )
            return

        self.workspace.write_file(self.output_filename, text)
