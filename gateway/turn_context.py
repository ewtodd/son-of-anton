"""Per-turn context shared between ``GatewayRunner._run_agent_inner`` and the
``TurnRunner`` collaborator (gateway/run.py).

``_run_agent_inner`` historically defined its turn plumbing as nested closures
that closed over enclosing locals.  ``TurnContext`` is the extraction seam:
each closed-over local becomes a field on this dataclass, so the closure bodies
can move onto ``TurnRunner`` methods unchanged modulo ``name`` -> ``ctx.name``
rewrites.

Field notes:

- All fields are written once by ``_run_agent_inner`` while wiring up the turn
  (a few — ``agent_holder`` — are computed slightly later than construction
  and assigned onto the ctx as soon as the original locals were bound).
  Mutable state uses single-element-list containers so mutation stays visible
  to the outer body through the shared objects.
- ``_run_still_current`` stays a callable (it captures ``self``/
  ``session_key``/``run_generation``); carrying the callable keeps the
  extracted bodies byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class TurnContext:
    """Closed-over locals of ``_run_agent_inner`` needed by ``TurnRunner``."""

    # --- read-only turn identity / wiring -------------------------------
    source: Any = None
    _run_still_current: Callable[[], bool] = None  # type: ignore[assignment]
    _thinking_enabled: bool = False

    # --- queues ----------------------------------------------------------
    log_queue: Any = None

    # --- mutable single-element containers (shared with the outer body) --
    agent_holder: list = field(default_factory=lambda: [None])

    # ------------------------------------------------------------------
    # run_sync extraction (second wave of the seam): the closed-over locals
    # of ``_run_agent_inner`` that ``run_sync`` (and the four sibling bridge
    # callbacks) captured.  Same rules as above: read-only snapshots or
    # shared mutable containers; ``message`` is the ONE exception — the old
    # closure rebound it via ``nonlocal``, so the rebind sites now write
    # ``ctx.message`` and the outer body reads ``ctx.message`` afterwards.
    # ------------------------------------------------------------------

    # --- the ex-``nonlocal`` turn message (rebindable) --------------------
    message: Optional[str] = None

    # --- turn parameters / config snapshots (read-only in run_sync) -------
    history: Any = None
    context_prompt: Optional[str] = None
    channel_prompt: Optional[str] = None
    session_id: Optional[str] = None
    session_key: Optional[str] = None
    run_generation: Optional[int] = None
    process_task_id: str = ""
    process_baseline: frozenset[str] = field(default_factory=frozenset)
    _interrupt_depth: int = 0
    event_message_id: Optional[str] = None
    persist_user_message: Optional[Any] = None
    persist_user_timestamp: Optional[float] = None
    # display_kind stamped on the persisted user row at turn start when this
    # turn was self-injected (MessageEvent.internal), e.g.
    # "internal_notification" for async-delegation/background notifications
    # (#82888). DB-only presentation metadata; never sent to the provider.
    persist_user_display_kind: Optional[str] = None
    user_config: Any = None
    enabled_toolsets: Any = None
    disabled_toolsets: Any = None
    log_mode_enabled: bool = False
    interim_assistant_messages_enabled: bool = False

    # --- lazy-imported callables captured from the outer body -------------
    AIAgent: Any = None
    resolve_display_setting: Any = None

    # --- mutable holder cells (shared-list pattern; outer body + the
    #     post-executor closures read mutations through the same objects) --
    result_holder: list = field(default_factory=lambda: [None])
    tools_holder: list = field(default_factory=lambda: [None])
    stream_consumer_holder: list = field(default_factory=lambda: [None])

    # --- hook / status bridge wiring (published at original binding sites) -
    _loop_for_step: Any = None
    _status_adapter: Any = None
    _status_chat_id: Any = None
    _status_thread_metadata: Optional[dict] = None

    # --- extracted sibling callbacks (bound TurnRunner methods; run_sync
    #     reads them through the ctx exactly where it used to close over
    #     the sibling closures) ---------------------------------------------
    progress_callback: Optional[Callable] = None
    _status_callback_sync: Optional[Callable] = None

