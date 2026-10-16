"""Normalized response types for the chat.completions transport.

Only fields every downstream consumer reads are top-level; anything else goes
in ``provider_data`` so that only the code that needs it reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """A normalized tool call.

    ``id`` may be ``None`` when the endpoint omits it; the agent fills it via
    ``_deterministic_call_id()`` before storing in history.
    """

    id: str | None
    name: str
    arguments: str  # JSON string
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    # The agent loop reads tc.function.name / tc.function.arguments.
    @property
    def type(self) -> str:
        return "function"

    @property
    def function(self) -> ToolCall:
        return self


@dataclass
class Usage:
    """Token usage from an API response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class NormalizedResponse:
    """Normalized API response; ``provider_data`` holds ``reasoning_content``,
    ``reasoning_details`` and ``refusal`` when the endpoint sent them."""

    content: str | None
    tool_calls: list[ToolCall] | None
    finish_reason: str  # "stop", "tool_calls", "length", "content_filter"
    reasoning: str | None = None
    usage: Usage | None = None
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def reasoning_content(self) -> str | None:
        return (self.provider_data or {}).get("reasoning_content")

    @property
    def reasoning_details(self):
        return (self.provider_data or {}).get("reasoning_details")
