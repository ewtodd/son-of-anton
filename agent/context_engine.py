"""Base class of the context engine (the built-in ContextCompactor).

A context engine controls how conversation context is managed when
approaching the model's token limit. The engine is responsible for:
  - Deciding when compaction should fire
  - Performing compaction (summarization)
  - Tracking token usage from API responses

Lifecycle:
  1. Engine is instantiated
  2. on_session_start() called when a conversation begins
  3. update_from_response() called after each API response with usage data
  4. should_compact() checked after each turn
  5. compact() called when should_compact() returns True
  6. on_session_end() called at real session boundaries (CLI exit, /reset,
     gateway session expiry) — NOT per-turn
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


def automatic_compaction_status_message(
    engine: Any,
    *,
    phase: str,
    default_message: str,
    **context: Any,
) -> str | None:
    """Resolve host-visible status for an automatic compaction event.

    Engines can suppress routine automatic status with
    ``emit_automatic_compaction_status = False`` or customize it by defining
    ``get_automatic_compaction_status_message(...)``. Empty strings and
    ``None`` mean "do not emit a lifecycle status".
    """
    if not getattr(engine, "emit_automatic_compaction_status", True):
        return None

    formatter = getattr(engine, "get_automatic_compaction_status_message", None)
    if callable(formatter):
        message = formatter(
            phase=phase,
            default_message=default_message,
            **context,
        )
    else:
        message = default_message

    if message is None:
        return None
    message = str(message).strip()
    return message or None


class ContextEngine(ABC):
    """Base class all context engines must implement."""

    # -- Identity ----------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'compactor', 'lcm')."""

    # -- Token state (read by run_agent.py for display/logging) ------------
    #
    # Engines MUST maintain these. run_agent.py reads them directly.

    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compaction_count: int = 0

    # -- Compaction parameters (read by the turn-start / post-response gates) --
    #
    # These control the automatic compaction check.  Subclasses may
    # override via __init__ or property; defaults are sensible for most
    # engines.
    #
    # protect_first_n semantics (since PR #13754): count of non-system head
    # messages always preserved verbatim, IN ADDITION to the system prompt
    # which is always implicitly protected.  Default 3 keeps the
    # historical "system + first 3 non-system messages" head shape.

    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6

    # User-visible lifecycle status for automatic host-triggered compaction.
    # Alternative engines that treat compaction as routine background
    # maintenance can set this false to keep successful automatic passes silent;
    # warnings, errors, and explicit manual commands should still surface.
    emit_automatic_compaction_status: bool = True

    # -- Core interface ----------------------------------------------------

    @abstractmethod
    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """Update tracked token usage from an API response.

        Called after every LLM call with a normalized usage dict. The legacy
        keys ``prompt_tokens``, ``completion_tokens``, and ``total_tokens``
        are always present. Newer hosts also include canonical buckets:
        ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
        ``cache_write_tokens``, and ``reasoning_tokens``. Engines should
        treat those fields as optional for compatibility with older hosts.
        """

    @abstractmethod
    def should_compact(self, prompt_tokens: int = None) -> bool:
        """Return True if compaction should fire this turn."""

    def should_compact_info(self, prompt_tokens: int = None) -> "tuple[bool, str | None]":
        """Return ``(should_compact, reason)``.

        The base implementation is backward-compatible: engines that only
        implement ``should_compact`` get ``(should_compact(prompt_tokens),
        None)``. Concrete engines with richer block reasons (e.g. a
        summary-LLM cooldown or an anti-thrashing guard) override this to
        surface a human-readable reason so callers can warn the user instead
        of silently skipping compaction. Added for the silent-overflow
        warning fix (#62625) so plugin engines don't raise AttributeError.
        """
        return self.should_compact(prompt_tokens), None

    @abstractmethod
    def compact(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        """Compact the message list and return the new message list.

        This is the main entry point. The engine receives the full message
        list and returns a (possibly shorter) list that fits within the
        context budget. The implementation is free to summarize, build a
        DAG, or do anything else — as long as the returned list is a valid
        OpenAI-format message sequence.

        Args:
            focus_topic: Optional topic string from manual ``/compact <focus>``.
                Engines that support guided compaction should prioritise
                preserving information related to this topic.  Engines that
                don't support it may simply ignore this argument.
            force: Whether a user-requested compaction should bypass an
                engine-owned cooldown. Engines without cooldowns may ignore it.
        """

    # -- Optional: proactive tool-result prune -----------------------------

    def prune_tool_results_only(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int | None = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        """Deterministically trim old tool-result payloads without an LLM call.

        Runs on a low, cost-oriented trigger independent of ``should_compact``
        so large-window engines can reclaim re-sent tool output long before full
        compaction would fire. Returns ``(messages, n_pruned)``.

        Default is a safe no-op: the list is returned unchanged with ``0``
        pruned. Engines that don't implement a cheap prune — and any engine that
        predates this hook — inherit this default, so the agent loop's
        post-tool-call prune path never raises ``AttributeError`` on them. The
        built-in ContextCompactor overrides this with the real implementation.
        """
        return messages, 0

    # -- Optional: turn-start maintenance hook ------------------------------

    def should_compact_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        """Request a ``compact()`` pass at turn start while UNDER threshold.

        Consulted once per turn, before the first model call, only when the
        real-usage gate did not already fire (and no failure cooldown is
        active). Lets engines that compact incrementally (chunked / LCM-style
        maintenance) run below the token threshold. Default returns False.
        """
        return False

    def get_automatic_compaction_status_message(
        self,
        *,
        phase: str,
        default_message: str,
        **context: Any,
    ) -> str | None:
        """Return user-visible status for automatic host-triggered compaction.

        Return ``None`` to suppress successful automatic lifecycle status for
        this compaction event. ``phase`` identifies the host call site (for
        example ``"preflight"`` or ``"compact"``). ``context`` contains
        best-effort fields such as ``approx_tokens`` and ``threshold_tokens``.

        This hook does not control warning/error messages or explicit manual
        commands such as ``/compact``.
        """
        if not self.emit_automatic_compaction_status:
            return None
        return default_message

    # -- Optional: manual /compact preflight ------------------------------

    def has_content_to_compact(self, messages: List[Dict[str, Any]]) -> bool:
        """Quick check: is there anything in ``messages`` that can be compacted?

        Used by the gateway ``/compact`` command as a preflight guard —
        returning False lets the gateway report "nothing to compact yet"
        without making an LLM call.

        Default returns True (always attempt).  Engines with a cheap way
        to introspect their own head/tail boundaries should override this
        to return False when the transcript is still entirely protected.
        """
        return True

    # -- Optional: session lifecycle ---------------------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """Called when a new conversation session begins.

        Use this to load persisted state (DAG, store) for the session.
        kwargs may include son_of_anton_home, platform, model, etc.
        """

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Called at real session boundaries (CLI exit, /reset, gateway expiry).

        Use this to flush state, close DB connections, etc.
        NOT called per-turn — only when the session truly ends.
        """

    def on_session_reset(self) -> None:
        """Called on /new or /reset. Reset per-session state.

        Default resets compaction_count and token tracking.
        """
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compaction_count = 0

    # -- Optional: status / display ----------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Return status dict for display/logging.

        Default returns the standard fields run_agent.py expects.
        """
        # Clamp the -1 "compaction just ran, awaiting real usage" sentinel
        # (set by conversation_compaction) to 0 so status readers don't see a
        # raw -1 or a negative usage_percent on the transitional turn. Mirrors
        # the CLI/gateway status-bar paths (cli.py, tui_gateway/server.py).
        last_prompt = self.last_prompt_tokens if self.last_prompt_tokens > 0 else 0
        return {
            "last_prompt_tokens": last_prompt,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                min(100, last_prompt / self.context_length * 100)
                if self.context_length else 0
            ),
            "compaction_count": self.compaction_count,
        }

    # -- Optional: model switch support ------------------------------------

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        """Called when the user switches models or on fallback activation.

        Default updates context_length and recalculates threshold_tokens
        from threshold_percent. Override if your engine needs more
        (e.g. recalculate DAG budgets, switch summary models).
        """
        self.context_length = context_length
        # Apply per-model threshold overrides if set (longest substring match).
        # Falls back to _config_threshold_percent (the raw config value) when
        # no override matches. Plugin engines that override update_model() can
        # call resolve_model_threshold() for the same logic.
        from agent.context_compactor import resolve_model_threshold
        if not hasattr(self, "_config_threshold_percent"):
            # Snapshot the pre-override percent ONCE so repeated model
            # switches fall back to the engine's configured value, not the
            # previous model's override.
            self._config_threshold_percent = self.threshold_percent
        self._base_threshold_percent = resolve_model_threshold(
            model, getattr(self, "model_thresholds", {}),
            self._config_threshold_percent,
        )
        self.threshold_percent = self._base_threshold_percent
        self.threshold_tokens = int(context_length * self.threshold_percent)
