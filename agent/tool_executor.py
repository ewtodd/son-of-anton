"""Tool-call execution — sequential and concurrent dispatch.

Both AIAgent methods (``_execute_tool_calls_sequential`` and
``_execute_tool_calls_concurrent``) live here as module-level
functions that take the parent ``AIAgent`` as their first argument.

``run_agent`` keeps thin wrappers so existing call sites work; tests
that patch ``run_agent._set_interrupt`` are honored because the
extracted functions reach back through the ``run_agent`` module via
``_ra()`` for that symbol.
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import logging
import random
import threading
import time
from typing import Any, Optional

from agent.display import (
    KawaiiSpinner,
    build_tool_preview as _build_tool_preview,
    build_tool_label as _build_tool_label,
    get_cute_tool_message as _get_cute_tool_message_impl,
    get_tool_emoji as _get_tool_emoji,
    _detect_tool_failure,
)
from agent.tool_dispatch_helpers import (
    _NEVER_PARALLEL_TOOLS,
    _is_multimodal_tool_result,
    _multimodal_text_summary,
    _append_subdir_hint_to_multimodal,
    _plan_tool_batch_segments,
    make_tool_result_message,
)
from tools.terminal_tool import (
    get_active_env,
)
from tools.thread_context import propagate_context_to_thread
from tools.tool_result_storage import (
    maybe_persist_tool_result,
    enforce_turn_budget,
    extract_persisted_path,
)
from tools.budget_config import BudgetConfig, DEFAULT_BUDGET, budget_for_context_window

logger = logging.getLogger(__name__)


def _record_persisted_path_for_stub(agent, tool_call_id: str, function_result) -> None:
    """Tell the stall guards where a persisted result's full content lives.

    When a large result is spilled to disk (<persisted-output> preview), a
    later result-reference stub pointing at that first occurrence must carry
    the spillover file path so the reference can't dangle. Best-effort: never
    lets bookkeeping break tool execution.
    """
    try:
        if not isinstance(function_result, str):
            return
        path = extract_persisted_path(function_result)
        if path:
            agent._tool_guardrails.record_persisted_result(tool_call_id, path)
    except Exception as exc:
        logger.debug("persisted-path record for result stub failed: %s", exc)


def _budget_for_agent(agent) -> BudgetConfig:
    """Resolve a tool-result BudgetConfig scaled to the agent's context window.

    Large-context models keep the historical 100K/200K char defaults; small
    models (e.g. a 65K-token local model switched into mid-session) get a budget
    proportional to their window so a single large tool result can't push the
    request past the model's limit (#23767). Falls back to the default budget
    when the context length isn't resolvable.
    """
    try:
        ctx = getattr(getattr(agent, "context_compactor", None), "context_length", None)
        # budget_for_context_window(None) (rather than DEFAULT_BUDGET) so the
        # config-driven MCP threshold override still applies when the context
        # length isn't resolvable.
        return budget_for_context_window(int(ctx) if ctx else None)
    except Exception:
        return DEFAULT_BUDGET

# Maximum number of concurrent worker threads for parallel tool execution.
# Mirrors the constant in ``run_agent`` for tests/imports that look here.
_MAX_TOOL_WORKERS = 8
# Per-call wall-clock guard for a concurrent tool batch.
_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S = 420.0
# Upper bound a concurrent worker will wait at the start-order gate for all
# earlier-ordered tools to advance before proceeding out of order. Long enough
# to cover slow-but-legitimate authorization (e.g. an approval round-trip),
# short enough that one wedged dispatch cannot starve the batch forever.
_START_ORDER_GATE_TIMEOUT_S = 120.0


class _BatchAbandoned(BaseException):
    """Raised inside a worker when the batch was abandoned before dispatch.

    Derives from BaseException so intermediate ``except Exception`` handlers
    cannot swallow it and dispatch the tool anyway.
    """


def _parse_tool_arguments(raw_arguments: Any) -> tuple[dict, Optional[str]]:
    """Parse model-emitted arguments without repairing or coercing them."""
    try:
        arguments = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError):
        arguments = None
    if isinstance(arguments, dict):
        return arguments, None
    return {}, json.dumps(
        {
            "error": "Invalid tool arguments",
            "message": (
                "Tool arguments must be a valid JSON object; tool was not executed."
            ),
        },
        ensure_ascii=False,
    )


def _resolve_concurrent_tool_timeout() -> float | None:
    """The per-batch concurrent tool deadline."""
    return _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S


def _flush_session_db_after_tool_progress(
    agent,
    messages: list,
    *,
    stage: str,
) -> bool:
    """Flush tool-call progress before projecting it to any UI surface.

    Tool execution can perform side effects that terminate or restart the
    current Son of Anton process before the normal turn-end persistence path runs.
    Flush the already-appended assistant/tool messages immediately so the
    transcript survives destructive-but-valid tool calls.
    """
    try:
        persisted = agent._flush_messages_to_session_db(messages) is not False
        if not persisted:
            agent._incremental_persistence_failed = True
            # The flush caught its own exception and returned False; the
            # classified cause (if any) was captured at the catch site. Only
            # fall back to 'unknown' when nothing more specific is recorded.
            if getattr(agent, "_last_persistence_error_cause", None) is None:
                agent._last_persistence_error_cause = "unknown"
        return persisted
    except Exception as exc:
        agent._incremental_persistence_failed = True
        from son_of_anton_state import classify_persistence_error
        agent._last_persistence_error_cause = classify_persistence_error(exc)
        logger.warning("Incremental tool-call persistence failed after %s: %s", stage, exc)
        return False


def _max_workers_for_tool_batch(runnable_calls) -> int:
    """Return the worker cap for a concurrent tool batch."""
    if not runnable_calls:
        return 0
    return min(len(runnable_calls), _MAX_TOOL_WORKERS)


def _ra():
    """Lazy reference to ``run_agent`` so patches like ``run_agent._set_interrupt`` work."""
    import run_agent
    return run_agent


def _is_interpreter_shutdown_submit_error(exc: RuntimeError) -> bool:
    return "cannot schedule new futures after interpreter shutdown" in str(exc)


def _cancelled_tool_result(reason: str = "user interrupt") -> str:
    return json.dumps(
        {
            "error": f"Tool execution cancelled by {reason}",
            "status": "cancelled",
        },
        ensure_ascii=False,
    )


def _tool_search_scoped_names(agent) -> frozenset:
    """Return the deferrable tool names the session may invoke via tool_call.

    The Tool Search unwrap dispatches the underlying tool directly, bypassing
    the bridge branch (and its scope check) in
    ``model_tools.handle_function_call``. To keep a restricted-toolset session
    (subagent, kanban worker, curated gateway session) from reaching tools it
    was never granted, the unwrap validates the underlying name against this
    set: the deferrable subset of the session's own enabled/disabled toolset
    scope.

    Result is cached on the agent and refreshed when the tool registry's
    generation changes (e.g. an MCP server reconnects), so the common case is
    a dict lookup, not a full tool-defs rebuild on every tool call.
    """
    try:
        import model_tools
        from tools import tool_search as _ts
        from tools.registry import registry as _registry
    except Exception:
        return frozenset()

    enabled = getattr(agent, "enabled_toolsets", None)
    disabled = getattr(agent, "disabled_toolsets", None)
    cache_key = (
        _registry.current_scope_key(),
        getattr(_registry, "_generation", 0),
        frozenset(enabled) if enabled is not None else None,
        frozenset(disabled) if disabled is not None else None,
    )
    cached = getattr(agent, "_tool_search_scope_cache", None)
    if cached is not None and cached[0] == cache_key:
        return cached[1]
    try:
        scoped_defs = model_tools.get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=True,
            skip_tool_search_assembly=True,
        ) or []
        names = _ts.scoped_deferrable_names(scoped_defs)
    except Exception:
        names = frozenset()
    try:
        agent._tool_search_scope_cache = (cache_key, names)
    except Exception:
        pass
    return names


class _ToolTimeoutResult(str):
    """Marker for a synthesized sequential-tool timeout result."""


class _ToolCancelledResult(str):
    """Marker for a synthesized sequential-tool user-interrupt result.

    Like ``_ToolTimeoutResult``, it marks the result as synthesized so the
    tool row records an unknown effect.
    """


class _ConcurrentToolAuthorizationGate:
    """Exclude human approval waits from batch deadlines.

    Deadline exclusion is measured at the SOURCE of the human wait
    (``tools.approval.human_wait_seconds``: the CLI prompt and the gateway
    approval poll loop mark their own blocking windows), so a genuine
    approval wait (which can legitimately exceed any fixed bound) is excluded
    in full while a wedged tool still times out normally (#79719).
    """

    def __init__(self, *, session_key: str | None = None) -> None:
        self._session_key = session_key
        if self._session_key is None:
            try:
                from tools.approval import get_current_session_key

                # Snapshot the batch's session identity on the SUBMITTING
                # thread: excluded_seconds() is polled from the batch wait
                # loop, whose context may differ from the workers'.
                self._session_key = get_current_session_key()
            except Exception:
                logger.debug(
                    "authorization gate could not snapshot the session key; "
                    "human-wait exclusion will re-resolve it at poll time",
                    exc_info=True,
                )
        self._baseline_wait_seconds = self._human_wait_seconds()

    def _human_wait_seconds(self) -> float:
        try:
            from tools.approval import human_wait_seconds

            return human_wait_seconds(self._session_key)
        except Exception:
            return 0.0

    def excluded_seconds(self) -> float:
        """Return human-approval wait seconds accrued since the batch started."""
        return max(0.0, self._human_wait_seconds() - self._baseline_wait_seconds)


# Cadence for the in-flight tool activity heartbeat. Must stay far below the
# gateway turn-inactivity timeout (default 1800s) so a silent-but-healthy
# tool call never looks idle to the watchdog.
_TOOL_ACTIVITY_HEARTBEAT_INTERVAL_S = 30.0


def _run_tool_activity_heartbeat(
    agent,
    stop_event: threading.Event,
    label: str,
    interval: float = _TOOL_ACTIVITY_HEARTBEAT_INTERVAL_S,
) -> None:
    """Refresh the agent's activity clock while a tool call is in flight.

    The gateway's turn-inactivity watchdog
    (``gateway/run.py::_watch_gateway_turn_inactivity``) abandons a turn
    once ``seconds_since_activity`` exceeds the inactivity timeout
    (default 30 min). Activity is stamped when a tool *starts* and when it
    *completes*, but a tool call that runs silently for 30+ minutes
    (quiet builds, long pytest suites, large downloads, network waits that
    emit no output) previously froze the clock at "executing tool: <name>"
    and the watchdog hard-abandoned a turn that was still making progress,
    reaping the tool's processes mid-execution.

    This daemon thread touches ``agent._touch_activity`` every ``interval``
    seconds until ``stop_event`` is set (the tool call returned), so the
    gateway keeps seeing a live turn for the whole duration of the call.

    A tool that truly hangs is still bounded by the tool layer's own
    timeouts (terminal ``timeout`` default 180s, the concurrent batch
    deadline ~420s), so the heartbeat only extends the turn's life for as
    long as the tool call is legitimately executing — it does not unbind
    wedged tools. The 30-min gateway backstop remains for turns whose
    agent loop itself stalls (no API call, no tool call in flight).
    """

    try:
        while not stop_event.wait(interval):
            agent._touch_activity(label)
    except Exception:
        # A heartbeat must never break the agent loop.
        pass


def _run_managed_tool(
    agent,
    *,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: str,
    execute,
    scope_block: str | None = None,
    display_index: int | None = None,
    begin_execution=None,
) -> tuple[Any, bool]:
    """Apply the scope block and guardrails, then dispatch exactly once.

    Returns ``(result, blocked)``.
    """

    def _begin() -> None:
        _begin_tool_execution(
            agent,
            function_name=function_name,
            function_args=function_args,
            effective_task_id=effective_task_id,
            tool_call_id=tool_call_id,
            display_index=display_index,
        )

    def _advance_start_order(callback=None) -> None:
        if begin_execution is None:
            if callback is not None:
                callback()
            return
        begin_execution(callback)

    guardrail_decision = None
    if scope_block is None:
        guardrail_decision = agent._tool_guardrails.before_call(
            function_name, function_args
        )
        if guardrail_decision.allows_execution:
            guardrail_decision = None

    if scope_block is not None or guardrail_decision is not None:
        _advance_start_order()
        if scope_block is not None:
            result = json.dumps({"error": scope_block}, ensure_ascii=False)
        else:
            result = agent._guardrail_block_result(guardrail_decision)
        return result, True

    _advance_start_order(_begin)

    # Keep the gateway turn-inactivity watchdog from abandoning a turn
    # whose tool call runs silently for longer than the inactivity
    # timeout (#84491): stamp activity periodically while the tool is
    # in flight, not just at start/completion. Both the sequential and
    # the concurrent paths funnel through here, so a single heartbeat
    # covers every tool.
    _hb_stop = threading.Event()
    _hb_thread = threading.Thread(
        target=_run_tool_activity_heartbeat,
        args=(agent, _hb_stop, f"tool running: {function_name}"),
        kwargs={"interval": _TOOL_ACTIVITY_HEARTBEAT_INTERVAL_S},
        daemon=True,
        name=f"tool-activity-hb-{function_name[:24]}",
    )
    _hb_thread.start()
    try:
        return execute(function_args), False
    finally:
        _hb_stop.set()
        _hb_thread.join(timeout=2.0)


# How often the sequential-tool wait loop wakes to check for a user
# interrupt while the worker runs. Short enough that /stop or a redirect
# lands within ~1s even when the tool itself never polls is_interrupted().
_SEQUENTIAL_INTERRUPT_POLL_SECONDS = 1.0


def _resolve_sequential_tool_timeout() -> float | None:
    """Deadline for one sequential tool call: the concurrent batch deadline,
    so the two executor paths cannot drift apart."""
    return _resolve_concurrent_tool_timeout()


def _run_sequential_managed_tool(
    agent,
    *,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: str,
    execute,
    scope_block: str | None = None,
    display_index: int | None = None,
) -> tuple[Any, bool]:
    """Run one sequential call with the concurrent executor's deadline.

    Interactive input tools such as ``clarify`` wait on a human. Their own
    timeout (``agent.clarify_timeout``: default 3600s, or unlimited when
    ``<= 0``) owns that wait. Applying the generic tool deadline here would
    return ``tool_timeout`` while the prompt and worker stay active.
    """
    timeout_s = _resolve_sequential_tool_timeout()
    kwargs = {
        "function_name": function_name,
        "function_args": function_args,
        "effective_task_id": effective_task_id,
        "tool_call_id": tool_call_id,
        "execute": execute,
        "scope_block": scope_block,
        "display_index": display_index,
    }
    if function_name in _NEVER_PARALLEL_TOOLS:
        return _run_managed_tool(agent, **kwargs)

    from tools.daemon_pool import DaemonThreadPoolExecutor

    authorization_gate = _ConcurrentToolAuthorizationGate()
    worker_tid: list[int] = []

    def _run() -> tuple[Any, bool]:
        tid = threading.current_thread().ident
        worker_tid.append(tid)
        with agent._tool_worker_threads_lock:
            agent._tool_worker_threads.add(tid)
        try:
            return _run_managed_tool(agent, **kwargs)
        finally:
            with agent._tool_worker_threads_lock:
                agent._tool_worker_threads.discard(tid)
            try:
                _ra()._set_interrupt(False, tid)
            except Exception:
                pass

    executor = DaemonThreadPoolExecutor(max_workers=1)
    future = executor.submit(propagate_context_to_thread(_run))
    # ``timeout_s`` disabled (None) still runs on the worker: the wait loop
    # below is what makes a non-cooperative tool interruptible at all, so
    # "no deadline" must not mean "no interrupt checks" (#86xxx class fix —
    # sequential path previously blocked until the tool returned).
    deadline = time.monotonic() + timeout_s if timeout_s is not None else None
    started = time.monotonic()
    timed_out = False
    interrupted = False
    _last_heartbeat = 0
    try:
        while True:
            wait_slice = _SEQUENTIAL_INTERRUPT_POLL_SECONDS
            if deadline is not None:
                remaining = (
                    deadline + authorization_gate.excluded_seconds() - time.monotonic()
                )
                if remaining <= 0:
                    timed_out = True
                    break
                wait_slice = min(wait_slice, remaining)
            try:
                return future.result(timeout=wait_slice)
            except concurrent.futures.TimeoutError:
                if agent._interrupt_requested:
                    interrupted = True
                    break
                elapsed = int(time.monotonic() - started)
                if elapsed - _last_heartbeat >= 30:
                    _last_heartbeat = elapsed
                    agent._touch_activity(
                        f"sequential tool running ({elapsed}s): {function_name}"
                    )

        if interrupted:
            # Belt-and-braces: interrupt() already fans out to tracked worker
            # tids, but the worker may have registered after the fan-out ran.
            for tid in worker_tid:
                try:
                    _ra()._set_interrupt(True, tid)
                except Exception:
                    pass
            # Give a cooperative tool a moment to notice its per-thread
            # interrupt bit and return a real result (mirrors the concurrent
            # path's 3s grace).
            concurrent.futures.wait([future], timeout=3.0)
            if future.done() and not future.cancelled():
                return future.result()
            timed_out = True  # reuse the abandon-shutdown path in finally
            future.cancel()
            message = (
                f"[Tool execution cancelled — {function_name} was abandoned "
                "after user interrupt]"
            )
            logger.info(
                "sequential tool %s abandoned after user interrupt (%.1fs elapsed)",
                function_name, time.monotonic() - started,
            )
            return _ToolCancelledResult(message), False

        # Only reachable when a deadline exists (interrupted returns above).
        assert timeout_s is not None
        message = (
            f"Error executing tool '{function_name}': "
            f"timed out after {timeout_s:.1f}s"
        )
        logger.warning(
            "sequential tool %s timed out after %.1fs", function_name, timeout_s
        )
        future.cancel()
        for tid in worker_tid:
            try:
                _ra()._set_interrupt(True, tid)
            except Exception:
                pass
        return _ToolTimeoutResult(message), False
    finally:
        # Never join a wedged worker. DaemonThreadPoolExecutor also keeps it out
        # of the stdlib atexit join, matching the concurrent timeout path.
        executor.shutdown(wait=not timed_out, cancel_futures=timed_out)


def _begin_tool_execution(
    agent,
    *,
    function_name: str,
    function_args: dict[str, Any],
    effective_task_id: str,
    tool_call_id: str,
    display_index: int | None,
) -> None:
    """Run user-visible and checkpoint preflight on final tool arguments."""
    if not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off":
        display_args = function_args
        args_str = json.dumps(display_args, ensure_ascii=False)
        prefix = f"Tool {display_index}" if display_index is not None else "Tool"
        if agent.verbose_logging:
            print(f"  📞 {prefix}: {function_name}({list(display_args.keys())})")
            print(
                agent._wrap_verbose(
                    "Args: ", json.dumps(display_args, indent=2, ensure_ascii=False)
                )
            )
        else:
            args_preview = (
                args_str[: agent.log_prefix_chars] + "..."
                if len(args_str) > agent.log_prefix_chars
                else args_str
            )
            print(
                f"  📞 {prefix}: {function_name}({list(function_args.keys())}) - "
                f"{args_preview}"
            )

    agent._current_tool = function_name
    agent._touch_activity(f"executing tool: {function_name}")
    try:
        from tools.environments.base import set_activity_callback

        set_activity_callback(agent._touch_activity)
    except Exception:
        pass

    if agent.tool_progress_callback:
        try:
            display_args = function_args
            preview = _build_tool_preview(function_name, display_args)
            agent.tool_progress_callback(
                "tool.started", function_name, preview, display_args
            )
        except Exception as callback_error:
            logging.debug("Tool progress callback error: %s", callback_error)

    if agent.tool_start_callback:
        try:
            display_args = function_args
            agent.tool_start_callback(
                tool_call_id, function_name, display_args
            )
        except Exception as callback_error:
            logging.debug("Tool start callback error: %s", callback_error)


def execute_tool_calls_concurrent(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> None:
    """Execute multiple tool calls concurrently using a thread pool.

    Results are collected in the original tool-call order and appended to
    messages so the API sees them in the expected sequence.

    ``finalize=False`` skips the end-of-batch aggregate budget enforcement
    and /steer injection — used when this call is one segment of a larger
    mixed batch and the segmented dispatcher owns the turn-end work.
    """
    tool_calls = assistant_message.tool_calls
    num_tools = len(tool_calls)

    # Resolve the context-scaled tool-output budget once per turn (cheap, but
    # avoids rebuilding it per result inside the loop below).
    _tool_budget = _budget_for_agent(agent)

    # ── Pre-flight: interrupt check ──────────────────────────────────
    if agent._interrupt_requested:
        print(f"{agent.log_prefix}⚡ Interrupt: skipping {num_tools} tool call(s)")
        for tc in tool_calls:
            cancelled_result = (
                f"[Tool execution cancelled — {tc.function.name} was skipped "
                "due to user interrupt]"
            )
            messages.append(make_tool_result_message(
                tc.function.name,
                cancelled_result,
                tc.id,
                effect_disposition="none",
            ))
            _flush_session_db_after_tool_progress(
                agent,
                messages,
                stage=f"cancelled tool result {tc.function.name}",
            )
        return

    # ── Parse args + pre-execution bookkeeping ───────────────────────
    # (tool call, resolved name, parsed args, parse error, tool-search scope
    # block)
    parsed_calls = []
    for tool_call in tool_calls:
        function_name = tool_call.function.name

        function_args, malformed_args_result = _parse_tool_arguments(
            tool_call.function.arguments
        )

        if malformed_args_result is not None:
            parsed_calls.append(
                (
                    tool_call,
                    function_name,
                    function_args,
                    malformed_args_result,
                    None,
                )
            )
            continue

        # ── Tool Search unwrap ────────────────────────────────────────
        # When the model invokes the tool_call bridge, peel it open so
        # every downstream check (checkpointing, guardrails, the
        # display/activity feed, the post-call callback) sees the
        # underlying tool — not the bridge.
        #
        # The original tool_call entry on ``tool_call.function`` is left
        # untouched so the conversation transcript and the matching
        # tool_call_id are preserved exactly as the model emitted them.
        #
        # Scope gate: the unwrap dispatches the underlying tool directly
        # (bypassing the bridge branch in handle_function_call and its
        # scope check), so we enforce session toolset scope HERE. A tool
        # the session was not granted is rejected before any checkpoint
        # or dispatch fires.
        _ts_scope_block = None
        try:
            from tools import tool_search as _ts
            if function_name == _ts.TOOL_CALL_NAME:
                _underlying, _underlying_args, _err = _ts.resolve_underlying_call(function_args)
                if not _err and _underlying:
                    if _underlying in _tool_search_scoped_names(agent):
                        # Probe-validate before unwrapping (ironclaw#5149):
                        # missing required args return the parameter schema
                        # instead of dispatching into an opaque failure.
                        _probe_err = _ts.validate_deferred_call_args(_underlying, _underlying_args)
                        if _probe_err is not None:
                            _ts_scope_block = _probe_err
                        else:
                            function_name = _underlying
                            function_args = _underlying_args
                    else:
                        _ts_scope_block = (
                            f"'{_underlying}' is not available in this session. "
                            "Use tool_search to find tools you can call."
                        )
        except Exception:
            pass

        parsed_calls.append(
            (tool_call, function_name, function_args, None, _ts_scope_block)
        )

    # ── Logging / callbacks ──────────────────────────────────────────
    tool_names_str = ", ".join(name for _, name, _, _, _ in parsed_calls)
    if not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off":
        print(f"  ⚡ Concurrent: {num_tools} tool calls — {tool_names_str}")

    # ── Concurrent execution ─────────────────────────────────────────
    # Each slot holds (function_name, function_args, function_result, duration, error_flag, blocked_flag)
    results = [None] * num_tools
    for i, (tc, name, args, block_result, _scope_block) in enumerate(parsed_calls):
        if block_result is not None:
            results[i] = (name, args, block_result, 0.0, True, True)

    start_condition = threading.Condition()
    next_start_order = 0
    # Set once the batch is abandoned (deadline or interrupt) so a worker parked
    # at the start-order gate exits immediately instead of waking up minutes
    # later and dispatching a tool the turn has already reported as timed out.
    batch_abandoned = threading.Event()
    authorization_gate = _ConcurrentToolAuthorizationGate()

    def _abandon_batch() -> None:
        """Release every gate-parked worker so none dispatches post-abandon."""
        batch_abandoned.set()
        with start_condition:
            start_condition.notify_all()

    # The gate bound must sit UNDER the batch deadline, otherwise the deadline
    # fires first and the parked workers are still falsely reported as timed
    # out without ever starting — the very bug this gate timeout fixes. A
    # disabled deadline (None) keeps the stock bound rather than waiting forever.
    def _start_order_gate_timeout(batch_timeout: float | None) -> float:
        if batch_timeout is None:
            return _START_ORDER_GATE_TIMEOUT_S
        return min(_START_ORDER_GATE_TIMEOUT_S, batch_timeout / 2)

    def _begin_in_order(
        order: int, callback=None, *, tool_name: str = "", gate_timeout: float | None = None
    ) -> bool:
        """Serialize dispatch by submit order. Returns False if abandoned."""
        nonlocal next_start_order
        with start_condition:
            # Bounded wait: a tool that wedges during its dispatch must not
            # park every later-ordered worker forever. Without the timeout,
            # one blocking dispatch starves the whole batch (the parked tools
            # then get falsely reported as "timed out" by the batch deadline
            # despite never having started) and the parked threads leak
            # permanently after the batch is abandoned — f.cancel() cannot
            # cancel running threads and nothing ever notifies the condition
            # again. On expiry, proceed out of order: the worst case is
            # interleaved approval prompts, strictly better than permanent
            # starvation. The >= predicate (rather than ==) lets one worker's
            # timeout-jump release every skipped worker immediately instead
            # of each burning its own full timeout; max() keeps the counter
            # monotonic when workers advance out of order. batch_abandoned
            # short-circuits the wait so an abandoned batch releases its
            # parked workers in milliseconds instead of one gate timeout.
            in_order = start_condition.wait_for(
                lambda: next_start_order >= order or batch_abandoned.is_set(),
                timeout=(
                    _START_ORDER_GATE_TIMEOUT_S if gate_timeout is None else gate_timeout
                ),
            )
            if batch_abandoned.is_set():
                # Do not run the callback or advance the counter: the turn has
                # already synthesized this tool's result and moved on.
                return False
            if not in_order:
                logger.warning(
                    "start-order gate timed out for %s (order=%d next=%d); "
                    "proceeding out of order",
                    tool_name or "tool",
                    order,
                    next_start_order,
                )
            try:
                if callback is not None:
                    callback()
            finally:
                next_start_order = max(next_start_order, order + 1)
                start_condition.notify_all()
        return True

    # Resolved before the workers are defined so the start-order gate can clamp
    # its own bound against the batch deadline it must stay under.
    timeout_s = _resolve_concurrent_tool_timeout()
    gate_timeout_s = _start_order_gate_timeout(timeout_s)

    # Touch activity before launching workers so the gateway knows
    # we're executing tools (not stuck).
    agent._current_tool = tool_names_str
    agent._touch_activity(f"executing {num_tools} tools concurrently: {tool_names_str}")

    def _run_tool(
        index,
        tool_call,
        function_name,
        function_args,
        scope_block,
        start_order,
    ):
        """Worker function executed in a thread."""
        # Register this worker tid so the agent can fan out an interrupt
        # to it — see AIAgent.interrupt().  Must happen first thing, and
        # must be paired with discard + clear in the finally block.
        _worker_tid = threading.current_thread().ident
        with agent._tool_worker_threads_lock:
            agent._tool_worker_threads.add(_worker_tid)
        # Race: if the agent was interrupted between fan-out (which
        # snapshotted an empty/earlier set) and our registration, apply
        # the interrupt to our own tid now so is_interrupted() inside
        # the tool returns True on the next poll.
        if agent._interrupt_requested:
            try:
                _ra()._set_interrupt(True, _worker_tid)
            except Exception:
                pass
        # Set the activity callback on THIS worker thread so
        # _wait_for_process (terminal commands) can fire heartbeats.
        # The callback is thread-local; the main thread's callback
        # is invisible to worker threads.
        try:
            from tools.environments.base import set_activity_callback
            set_activity_callback(agent._touch_activity)
        except Exception:
            pass
        # Approval/sudo callbacks (thread-local) and the agent turn's
        # ContextVars are propagated by propagate_context_to_thread() at the
        # submit site below (GHSA-qg5c-hvr5-hjgr, #13617).
        start = time.time()
        blocked = False
        start_advanced = False

        def _advance_start(callback=None) -> None:
            nonlocal start_advanced
            if start_advanced:
                return
            try:
                proceed = _begin_in_order(
                    start_order,
                    callback,
                    tool_name=function_name,
                    gate_timeout=gate_timeout_s,
                )
            finally:
                start_advanced = True
            if not proceed:
                # Batch already abandoned: the turn synthesized this tool's
                # result and moved on. Abort instead of dispatching late.
                raise _BatchAbandoned(function_name)

        try:
            try:
                def _execute(next_args: dict[str, Any]) -> Any:
                    return agent._invoke_tool(
                        function_name,
                        next_args,
                        effective_task_id,
                        tool_call.id,
                        messages=messages,
                    )

                result, blocked = _run_managed_tool(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    execute=_execute,
                    scope_block=scope_block,
                    display_index=index + 1,
                    begin_execution=_advance_start,
                )
            except _BatchAbandoned:
                # The batch was abandoned while we were parked at the start-order
                # gate. The main thread already synthesized this tool's result
                # (timeout/cancelled) and moved on, so write nothing: a late
                # results[index] write or progress print would double-report
                # a tool_call_id the turn already closed.
                logger.info(
                    "tool %s abandoned at start-order gate; skipping dispatch",
                    function_name,
                )
                return
            except KeyboardInterrupt:
                try:
                    agent.interrupt("keyboard interrupt")
                except Exception:
                    pass
                result = _cancelled_tool_result()
                duration = time.time() - start
                logger.info("tool %s cancelled (%.2fs)", function_name, duration)
                results[index] = (
                    function_name,
                    function_args,
                    result,
                    duration,
                    True,
                    False,
                )
                return
            except Exception as tool_error:
                result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error("_invoke_tool raised for %s: %s", function_name, tool_error, exc_info=True)
            duration = time.time() - start
            is_error, _ = _detect_tool_failure(function_name, result)
            if is_error:
                logger.info("tool %s failed (%.2fs): %s", function_name, duration, result[:200])
            else:
                logger.info("tool %s completed (%.2fs, %d chars)", function_name, duration, len(result))
            results[index] = (
                function_name,
                function_args,
                result,
                duration,
                is_error,
                blocked,
            )
        finally:
            # Teardown advance: keep the counter moving for any later-ordered
            # worker. Never let the abandonment signal escape from here — the
            # worker is already unwinding and the turn owns the result.
            try:
                _advance_start()
            except _BatchAbandoned:
                pass
            # Tear down worker-tid tracking.  Clear any interrupt bit we may
            # have set so the next task scheduled onto this recycled tid
            # starts with a clean slate.  This MUST be in a finally block
            # because BaseException subclasses (CancelledError, KeyboardInterrupt)
            # bypass ``except Exception`` and would otherwise leak the tid
            # into _interrupted_threads, poisoning the recycled thread.
            with agent._tool_worker_threads_lock:
                agent._tool_worker_threads.discard(_worker_tid)
            try:
                _ra()._set_interrupt(False, _worker_tid)
            except Exception:
                pass

    # Start spinner for CLI mode (skip when TUI handles tool progress)
    spinner = None
    if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
        face = random.choice(KawaiiSpinner.get_waiting_faces())
        spinner = KawaiiSpinner(f"{face} ⚡ running {num_tools} tools concurrently", spinner_type='dots', print_fn=agent._print_fn)
        spinner.start()

    try:
        runnable_calls = [
            (i, tc, name, args, scope_block)
            for i, (tc, name, args, parse_error, scope_block) in enumerate(
                parsed_calls
            )
            if parse_error is None
        ]
        futures = []
        future_to_index = {}
        timed_out_indices: set[int] = set()
        deadline = time.monotonic() + timeout_s if timeout_s is not None else None
        if runnable_calls:
            max_workers = _max_workers_for_tool_batch(runnable_calls)
            # Daemon workers: an interrupted/timed-out batch is abandoned with
            # shutdown(wait=False), but stdlib ThreadPoolExecutor workers are
            # non-daemon and registered in concurrent.futures' atexit hook,
            # which joins them unconditionally — so one wedged tool thread
            # would block interpreter exit forever (multi-minute CLI exits).
            from tools.daemon_pool import DaemonThreadPoolExecutor
            executor = DaemonThreadPoolExecutor(max_workers=max_workers)
            abandon_executor = False
            try:
                for submit_index, (i, tc, name, args, scope_block) in enumerate(
                    runnable_calls
                ):
                    # Propagate the agent turn's ContextVars (e.g.
                    # _approval_session_key) AND thread-local approval/sudo
                    # callbacks into the worker thread; clears callbacks on exit.
                    try:
                        f = executor.submit(
                            propagate_context_to_thread(_run_tool),
                            i,
                            tc,
                            name,
                            args,
                            scope_block,
                            submit_index,
                        )
                    except RuntimeError as submit_error:
                        if not _is_interpreter_shutdown_submit_error(submit_error):
                            raise
                        skipped_calls = runnable_calls[submit_index:]
                        logger.warning(
                            "interpreter shutdown while scheduling concurrent tools; "
                            "skipping %d unsubmitted tool(s)",
                            len(skipped_calls),
                        )
                        for (
                            skipped_i,
                            _tc,
                            skipped_name,
                            skipped_args,
                            _scope_block,
                        ) in skipped_calls:
                            if results[skipped_i] is None:
                                result = (
                                    f"Error executing tool '{skipped_name}': "
                                    "Python interpreter is shutting down; tool was not started"
                                )
                                results[skipped_i] = (
                                    skipped_name,
                                    skipped_args,
                                    result,
                                    0.0,
                                    True,
                                    False,
                                )
                        break
                    futures.append(f)
                    future_to_index[f] = i

                # Wait for all to complete with periodic heartbeats so the
                # gateway's inactivity monitor doesn't kill us during long
                # concurrent tool batches. Also check for user interrupts
                # so we don't block indefinitely when the user sends /stop
                # or a new message during concurrent tool execution.
                _conc_start = time.time()
                _interrupt_logged = False
                while True:
                    wait_timeout = 5.0
                    if deadline is not None:
                        effective_deadline = (
                            deadline + authorization_gate.excluded_seconds()
                        )
                        remaining = effective_deadline - time.monotonic()
                        if remaining <= 0:
                            done, not_done = set(), {
                                f for f in futures if not f.done()
                            }
                        else:
                            wait_timeout = min(wait_timeout, remaining)
                            done, not_done = concurrent.futures.wait(
                                futures, timeout=wait_timeout,
                            )
                    else:
                        done, not_done = concurrent.futures.wait(
                            futures, timeout=wait_timeout,
                        )
                    if not not_done:
                        break

                    if (
                        deadline is not None
                        and time.monotonic()
                        >= deadline + authorization_gate.excluded_seconds()
                    ):
                        abandon_executor = True
                        timed_out_indices = {
                            future_to_index[f]
                            for f in not_done
                            if f in future_to_index
                        }
                        _still_running = [
                            parsed_calls[i][1]
                            for i in timed_out_indices
                        ]
                        logger.warning(
                            "concurrent tool batch timed out after %.1fs; "
                            "%d tool(s) still running: %s",
                            timeout_s,
                            len(timed_out_indices),
                            ", ".join(_still_running[:5]),
                        )
                        for f in not_done:
                            f.cancel()
                        # Release gate-parked workers before the interrupt
                        # fan-out so none of them wakes up later and dispatches
                        # a tool this loop just reported as timed out.
                        _abandon_batch()
                        with agent._tool_worker_threads_lock:
                            worker_tids = list(agent._tool_worker_threads)
                        for tid in worker_tids:
                            try:
                                _ra()._set_interrupt(True, tid)
                            except Exception:
                                pass
                        break

                    # Check for interrupt — the per-thread interrupt signal
                    # already causes individual tools (terminal, execute_code)
                    # to abort, but tools without interrupt checks (web_search,
                    # read_file) will run to completion. Cancel any futures
                    # that haven't started yet so we don't block on them.
                    if agent._interrupt_requested:
                        abandon_executor = True
                        if not _interrupt_logged:
                            _interrupt_logged = True
                            agent._vprint(
                                f"{agent.log_prefix}⚡ Interrupt: cancelling "
                                f"{len(not_done)} pending concurrent tool(s)",
                                force=True,
                            )
                        for f in not_done:
                            f.cancel()
                        # Release gate-parked workers so they abort instead of
                        # dispatching after the turn was already interrupted.
                        _abandon_batch()
                        # Give already-running tools a moment to notice the
                        # per-thread interrupt signal and exit gracefully.
                        concurrent.futures.wait(not_done, timeout=3.0)
                        break

                    _conc_elapsed = int(time.time() - _conc_start)
                    # Heartbeat every ~30s (6 × 5s poll intervals)
                    if _conc_elapsed > 0 and _conc_elapsed % 30 < 6:
                        _still_running = [
                            parsed_calls[future_to_index[f]][1]
                            for f in not_done
                            if f in future_to_index
                        ]
                        agent._touch_activity(
                            f"concurrent tools running ({_conc_elapsed}s, "
                            f"{len(not_done)} remaining: {', '.join(_still_running[:3])})"
                        )
            finally:
                # Belt-and-braces: any exit from the wait loop that abandoned
                # the batch must release gate-parked workers, including the
                # exception path that never reaches the branches above.
                if abandon_executor:
                    _abandon_batch()
                # On abandon (interrupt or deadline) we intentionally do NOT
                # join hung workers: wait=False returns immediately and
                # cancel_futures drops queued-but-unstarted work. A wedged tool
                # thread is left running detached — the deliberate tradeoff vs.
                # deadlocking the whole batch. Normal completion joins (wait=True).
                executor.shutdown(
                    wait=not abandon_executor,
                    cancel_futures=abandon_executor,
                )
    finally:
        if spinner:
            # Build a summary message for the spinner stop
            completed = sum(1 for r in results if r is not None)
            total_dur = sum(r[3] for r in results if r is not None)
            spinner.stop(f"⚡ {completed}/{num_tools} tools completed in {total_dur:.1f}s total")

    # ── Post-execution: display per-tool results ─────────────────────
    for i, (tc, name, args, _parse_error, _scope_block) in enumerate(
        parsed_calls
    ):
        r = results[i]
        blocked = False
        is_error = True
        progress_function_name = name
        # A worker can finish and write results[i] in the window between the
        # deadline snapshot (timed_out_indices, taken from not_done) and this
        # loop. Prefer that real result over a fabricated timeout message — the
        # tool genuinely succeeded, just slightly late.
        effect_disposition = None
        if i in timed_out_indices and r is None:
            suffix = f"{timeout_s:.1f}s" if timeout_s is not None else "the configured timeout"
            function_result = f"Error executing tool '{name}': timed out after {suffix}"
            effect_disposition = "unknown"
            tool_duration = float(timeout_s or 0.0)
        elif r is None:
            # Tool was cancelled (interrupt) or thread didn't return
            if agent._interrupt_requested:
                function_result = f"[Tool execution cancelled — {name} was skipped due to user interrupt]"
            else:
                function_result = f"Error executing tool '{name}': thread did not return a result"
            tool_duration = 0.0
        else:
            function_name, function_args, function_result, tool_duration, is_error, blocked = r
            name = function_name
            args = function_args
            progress_function_name = function_name
            if blocked:
                effect_disposition = "none"

            if not blocked:
                function_result = agent._append_guardrail_observation(
                    function_name,
                    function_args,
                    function_result,
                    failed=is_error,
                    tool_call_id=getattr(tc, "id", "") or "",
                )

            if is_error:
                _err_text = _multimodal_text_summary(function_result)
                result_preview = _err_text[:200] if len(_err_text) > 200 else _err_text
                logger.warning("Tool %s returned error (%.2fs): %s", function_name, tool_duration, result_preview)

            # Track file-mutation outcome for the turn-end verifier.
            # `blocked` calls never actually ran — don't let a guardrail
            # block count as either a failure or a success.
            if not blocked:
                try:
                    agent._record_file_mutation_result(
                        function_name, function_args, function_result, is_error,
                    )
                except Exception as _ver_err:
                    logging.debug("file-mutation verifier record failed: %s", _ver_err)

            if agent.verbose_logging:
                logging.debug("Tool %s completed in %.2fs", function_name, tool_duration)
                logging.debug("Tool result (%d chars): %s", len(function_result), function_result)

        agent._current_tool = None
        _status_suffix = " (error)" if is_error else ""
        agent._touch_activity(f"tool completed: {name} ({tool_duration:.1f}s){_status_suffix}")

        display_function_result = function_result
        function_result = maybe_persist_tool_result(
            content=function_result,
            tool_name=name,
            tool_use_id=tc.id,
            env=get_active_env(effective_task_id),
            config=_tool_budget,
        ) if not _is_multimodal_tool_result(function_result) else function_result
        _record_persisted_path_for_stub(agent, tc.id, function_result)

        subdir_hints = agent._subdirectory_hints.check_tool_call(name, args)
        if subdir_hints:
            if _is_multimodal_tool_result(function_result):
                # Append the hint to the text summary part so the model
                # still sees it; don't touch the image blocks.
                _append_subdir_hint_to_multimodal(function_result, subdir_hints)
            else:
                function_result += subdir_hints

        # Unwrap _multimodal dicts to an OpenAI-style content list so any
        # vision-capable provider receives [{type:text},{type:image_url}]
        # rather than a raw Python dict.  The Anthropic adapter already
        # accepts content lists; vision-capable OpenAI-compatible servers
        # (mlx-vlm, GPT-4o, …) accept image_url in tool messages natively.
        # Text-only servers get a string-safe fallback here so a rejected
        # image tool result never poisons canonical session history.
        # String results pass through unchanged.
        _tool_content = agent._tool_result_content_for_active_model(name, function_result)
        tool_message = make_tool_result_message(
            name,
            _tool_content,
            tc.id,
            effect_disposition=effect_disposition,
        )
        messages.append(tool_message)
        risk_metadata = tool_message.get("_tool_output_risk")
        if not _flush_session_db_after_tool_progress(
            agent,
            messages,
            stage=f"tool result {name}",
        ):
            return

        # Every completion surface is downstream of the canonical append. If
        # the UI bridge or process dies while projecting one of these events,
        # resume can reconstruct the tool result that was already visible.
        if not blocked and agent.tool_progress_callback:
            try:
                agent.tool_progress_callback(
                    "tool.completed", progress_function_name, None, None,
                    duration=tool_duration, is_error=is_error,
                    result=display_function_result,
                )
            except Exception as cb_err:
                logging.debug("Tool progress callback error: %s", cb_err)

        # Print cute message per tool
        if agent._should_emit_quiet_tool_messages():
            cute_msg = _get_cute_tool_message_impl(
                name, args, tool_duration, result=display_function_result,
            )
            agent._safe_print(f"  {cute_msg}")
        elif not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off":
            _preview_str = _multimodal_text_summary(display_function_result)
            if agent.verbose_logging:
                print(f"  ✅ Tool {i+1} completed in {tool_duration:.2f}s")
                print(agent._wrap_verbose("Result: ", _preview_str))
            else:
                response_preview = _preview_str[:agent.log_prefix_chars] + "..." if len(_preview_str) > agent.log_prefix_chars else _preview_str
                print(f"  ✅ Tool {i+1} completed in {tool_duration:.2f}s - {response_preview}")

        if not blocked and agent.tool_complete_callback:
            try:
                display_args = args
                agent.tool_complete_callback(
                    tc.id, name, display_args, display_function_result,
                )
            except Exception as cb_err:
                logging.debug("Tool complete callback error: %s", cb_err)

        if (
            risk_metadata is not None
            and risk_metadata.get("risk") != "low"
            and agent.tool_progress_callback
        ):
            try:
                agent.tool_progress_callback(
                    "tool.output_risk",
                    name,
                    None,
                    None,
                    tool_call_id=tc.id,
                    risk_metadata=risk_metadata,
                )
            except Exception as cb_err:
                logging.debug("Tool output risk callback error: %s", cb_err)

    # ── Per-turn aggregate budget enforcement ─────────────────────────
    # Keep /steer pending until the final post-budget drain below.  The model
    # cannot observe a partial batch, while an early drain can be discarded
    # when aggregate budget enforcement replaces that tool result.
    num_tools = len(parsed_calls)
    if finalize and num_tools > 0:
        turn_tool_msgs = messages[-num_tools:]
        enforce_turn_budget(turn_tool_msgs, env=get_active_env(effective_task_id), config=_tool_budget)

    # ── /steer injection ──────────────────────────────────────────────
    # Append any pending user steer text to the last tool result so the
    # agent sees it on its next iteration. Runs AFTER budget enforcement
    # so the steer marker is never truncated. See steer() for details.
    if finalize and num_tools > 0:
        agent._apply_pending_steer_to_tool_results(messages, num_tools)



def _append_cancelled_tool_results(messages: list, tool_calls, *, reason: str) -> None:
    """Append a cancelled ``tool`` result for each call in ``tool_calls``.

    Used when a hard interrupt (KeyboardInterrupt / BaseException) aborts the
    sequential executor mid-batch. Without this, the loop re-raises leaving the
    assistant tool-call turn with no matching tool results — a message-role
    alternation violation that malforms the next provider request. Mirrors the
    cooperative-interrupt skip block and the concurrent path, both of which
    already emit a result for every call_id.
    """
    for tc in tool_calls:
        name = getattr(getattr(tc, "function", None), "name", "") or "tool"
        messages.append(make_tool_result_message(
            name,
            f"[Tool execution cancelled — {name} was skipped due to {reason}]",
            getattr(tc, "id", "") or "",
            effect_disposition="none",
        ))


def execute_tool_calls_sequential(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> None:
    """Execute tool calls sequentially (original behavior). Used for single calls or interactive tools.

    ``finalize=False`` skips the end-of-batch aggregate budget enforcement
    and /steer injection — used when this call is one segment of a larger
    mixed batch and the segmented dispatcher owns the turn-end work.
    """
    # Resolve the context-scaled tool-output budget once per turn.
    _tool_budget = _budget_for_agent(agent)

    for i, tool_call in enumerate(assistant_message.tool_calls, 1):
        if getattr(agent, "_incremental_persistence_failed", False):
            return
        # SAFETY: check interrupt BEFORE starting each tool.
        # If the user sent "stop" during a previous tool's execution,
        # do NOT start any more tools -- skip them all immediately.
        if agent._interrupt_requested:
            remaining_calls = assistant_message.tool_calls[i-1:]
            if remaining_calls:
                agent._vprint(f"{agent.log_prefix}⚡ Interrupt: skipping {len(remaining_calls)} tool call(s)", force=True)
            for skipped_tc in remaining_calls:
                skipped_name = skipped_tc.function.name
                cancelled_result = (
                    f"[Tool execution cancelled — {skipped_name} was skipped "
                    "due to user interrupt]"
                )
                messages.append(make_tool_result_message(
                    skipped_name,
                    cancelled_result,
                    skipped_tc.id,
                    effect_disposition="none",
                ))
                if not _flush_session_db_after_tool_progress(
                    agent,
                    messages,
                    stage=f"cancelled tool result {skipped_name}",
                ):
                    return
            break

        function_name = tool_call.function.name

        function_args, malformed_args_result = _parse_tool_arguments(
            tool_call.function.arguments
        )
        if malformed_args_result is not None:
            messages.append(
                make_tool_result_message(
                    function_name,
                    malformed_args_result,
                    tool_call.id,
                )
            )
            if not _flush_session_db_after_tool_progress(
                agent,
                messages,
                stage=f"invalid tool arguments {function_name}",
            ):
                return
            continue

        # Tool Search unwrap — see execute_tool_calls_concurrent for full
        # rationale, including the scope gate (the unwrap dispatches the
        # underlying tool directly, so session toolset scope is enforced here).
        _ts_scope_block: Optional[str] = None
        try:
            from tools import tool_search as _ts
            if function_name == _ts.TOOL_CALL_NAME:
                _underlying, _underlying_args, _err = _ts.resolve_underlying_call(function_args)
                if not _err and _underlying:
                    if _underlying in _tool_search_scoped_names(agent):
                        # Probe-validate before unwrapping (ironclaw#5149):
                        # missing required args return the parameter schema
                        # instead of dispatching into an opaque failure.
                        _probe_err = _ts.validate_deferred_call_args(_underlying, _underlying_args)
                        if _probe_err is not None:
                            # This path wraps _block_msg in {"error": ...} —
                            # flatten the probe payload to one plain string.
                            try:
                                _probe = json.loads(_probe_err)
                                _ts_scope_block = (
                                    f"{_probe.get('error', '')} Parameters schema: "
                                    f"{json.dumps(_probe.get('parameters', {}), ensure_ascii=False)}. "
                                    f"{_probe.get('hint', '')}"
                                ).strip()
                            except Exception:
                                _ts_scope_block = _probe_err
                        else:
                            function_name = _underlying
                            function_args = _underlying_args
                    else:
                        _ts_scope_block = (
                            f"'{_underlying}' is not available in this session. "
                            "Use tool_search to find tools you can call."
                        )
        except Exception:
            pass

        _execution_blocked = False

        tool_start_time = time.time()

        if function_name == "todo":
            def _execute(next_args: dict) -> Any:
                from tools.todo_tool import todo_tool as _todo_tool
                return _todo_tool(
                    todos=next_args.get("todos"),
                    merge=next_args.get("merge", False),
                    store=agent._todo_store,
                )
            function_result, _execution_blocked = _run_sequential_managed_tool(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('todo', function_args, tool_duration, result=function_result)}")
        elif function_name == "session_search":
            def _execute(next_args: dict) -> Any:
                session_db = agent._get_session_db_for_recall()
                if not session_db:
                    from son_of_anton_state import format_session_db_unavailable
                    return json.dumps({"success": False, "error": format_session_db_unavailable()})
                from tools.session_search_tool import session_search as _session_search
                return _session_search(
                    query=next_args.get("query", ""),
                    role_filter=next_args.get("role_filter"),
                    limit=next_args.get("limit", 3),
                    session_id=next_args.get("session_id"),
                    around_message_id=next_args.get("around_message_id"),
                    window=next_args.get("window", 5),
                    sort=next_args.get("sort"),
                    detail=next_args.get("detail", "adaptive"),
                    db=session_db,
                    current_session_id=agent.session_id,
                )
            function_result, _execution_blocked = _run_sequential_managed_tool(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('session_search', function_args, tool_duration, result=function_result)}")
        elif function_name == "memory":
            def _execute(next_args: dict) -> Any:
                target = next_args.get("target", "memory")
                operations = next_args.get("operations")
                from tools.memory_tool import memory_tool as _memory_tool
                result = _memory_tool(
                    action=next_args.get("action"),
                    target=target,
                    content=next_args.get("content"),
                    old_text=next_args.get("old_text"),
                    operations=operations,
                    scope=next_args.get("scope"),
                    store=agent._memory_store,
                )
                return result
            function_result, _execution_blocked = _run_sequential_managed_tool(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('memory', function_args, tool_duration, result=function_result)}")
        elif function_name == "clarify":
            def _execute(next_args: dict) -> Any:
                from tools.clarify_tool import clarify_tool as _clarify_tool
                return _clarify_tool(
                    question=next_args.get("question", ""),
                    choices=next_args.get("choices"),
                    multi_select=next_args.get("multi_select", False),
                    questions=next_args.get("questions"),
                    callback=agent.clarify_callback,
                )
            function_result, _execution_blocked = _run_sequential_managed_tool(
                agent,
                function_name=function_name,
                function_args=function_args,
                effective_task_id=effective_task_id,
                tool_call_id=getattr(tool_call, "id", "") or "",
                execute=_execute,
                scope_block=_ts_scope_block,
                display_index=i,
            )
            tool_duration = time.time() - tool_start_time
            if agent._should_emit_quiet_tool_messages():
                agent._vprint(f"  {_get_cute_tool_message_impl('clarify', function_args, tool_duration, result=function_result)}")
        elif function_name == "delegate_task":
            _action_arg = str(function_args.get("action") or "").strip().lower()
            tasks_arg = function_args.get("tasks")
            if _action_arg in ("list", "steer", "stop"):
                spinner_label = f"🔀 subagent {_action_arg}"
            elif tasks_arg and isinstance(tasks_arg, list):
                spinner_label = f"🔀 delegating {len(tasks_arg)} tasks · (/agents to monitor)"
            else:
                goal_preview = (function_args.get("goal") or "")[:30]
                spinner_label = (
                    f"🔀 {goal_preview} · (/agents to monitor)"
                    if goal_preview
                    else "🔀 delegating · (/agents to monitor)"
                )
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                spinner = KawaiiSpinner(f"{face} {spinner_label}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            agent._delegate_spinner = spinner
            _delegate_result = None
            try:
                def _execute(next_args: dict) -> Any:
                    return agent._dispatch_delegate_task(next_args)
                function_result, _execution_blocked = _run_sequential_managed_tool(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    execute=_execute,
                    scope_block=_ts_scope_block,
                    display_index=i,
                )
                _delegate_result = function_result
            finally:
                agent._delegate_spinner = None
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl('delegate_task', function_args, tool_duration, result=_delegate_result)
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        elif agent.quiet_mode:
            spinner = None
            if agent._should_emit_quiet_tool_messages() and agent._should_start_quiet_spinner():
                face = random.choice(KawaiiSpinner.get_waiting_faces())
                emoji = _get_tool_emoji(function_name)
                display_args = function_args
                preview = _build_tool_label(function_name, display_args) or function_name
                spinner = KawaiiSpinner(f"{face} {emoji} {preview}", spinner_type='dots', print_fn=agent._print_fn)
                spinner.start()
            _spinner_result = None
            try:
                def _execute(next_args: dict) -> Any:
                    return _ra().handle_function_call(
                        function_name,
                        next_args,
                        effective_task_id,
                        session_id=agent.session_id or "",
                        enabled_tools=(
                            list(agent.valid_tool_names)
                            if agent.valid_tool_names
                            else None
                        ),
                        enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                        disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                    )

                function_result, _execution_blocked = _run_sequential_managed_tool(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    execute=_execute,
                    scope_block=_ts_scope_block,
                    display_index=i,
                )
                _spinner_result = function_result
            except KeyboardInterrupt:
                function_result = _cancelled_tool_result()
                _spinner_result = function_result
                try:
                    agent.interrupt("keyboard interrupt")
                except Exception:
                    pass
                # Emit a tool result for THIS call and every remaining call in
                # the batch before re-raising, so the assistant tool-call turn
                # is never left without matching tool results (alternation).
                _append_cancelled_tool_results(
                    messages,
                    assistant_message.tool_calls[i - 1:],
                    reason="keyboard interrupt",
                )
                raise
            except Exception as tool_error:
                function_result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error("handle_function_call raised for %s: %s", function_name, tool_error, exc_info=True)
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(function_name, function_args, tool_duration, result=_spinner_result)
                if spinner:
                    spinner.stop(cute_msg)
                elif agent._should_emit_quiet_tool_messages():
                    agent._vprint(f"  {cute_msg}")
        else:
            try:
                def _execute(next_args: dict) -> Any:
                    return _ra().handle_function_call(
                        function_name,
                        next_args,
                        effective_task_id,
                        session_id=agent.session_id or "",
                        enabled_tools=(
                            list(agent.valid_tool_names)
                            if agent.valid_tool_names
                            else None
                        ),
                        enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                        disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                    )

                function_result, _execution_blocked = _run_sequential_managed_tool(
                    agent,
                    function_name=function_name,
                    function_args=function_args,
                    effective_task_id=effective_task_id,
                    tool_call_id=getattr(tool_call, "id", "") or "",
                    execute=_execute,
                    scope_block=_ts_scope_block,
                    display_index=i,
                )
            except KeyboardInterrupt:
                try:
                    agent.interrupt("keyboard interrupt")
                except Exception:
                    pass
                # Emit a tool result for THIS call and every remaining call in
                # the batch before re-raising (see interactive branch above).
                _append_cancelled_tool_results(
                    messages,
                    assistant_message.tool_calls[i - 1:],
                    reason="keyboard interrupt",
                )
                raise
            except Exception as tool_error:
                function_result = f"Error executing tool '{function_name}': {tool_error}"
                logger.error("handle_function_call raised for %s: %s", function_name, tool_error, exc_info=True)
            tool_duration = time.time() - tool_start_time

        _execution_timed_out = isinstance(
            function_result, (_ToolTimeoutResult, _ToolCancelledResult)
        )
        if isinstance(function_result, str):
            result_preview = function_result if agent.verbose_logging else (
                function_result[:200] if len(function_result) > 200 else function_result
            )
            _result_len = len(function_result)
        else:
            # Multimodal dict result (_multimodal=True) — not sliceable as string
            result_preview = function_result
            _result_len = len(str(function_result))

        # Log tool errors to the persistent error log so [error] tags
        # in the UI always have a corresponding detailed entry on disk.
        _is_error_result, _ = _detect_tool_failure(function_name, function_result)
        if not _execution_blocked:
            function_result = agent._append_guardrail_observation(
                function_name,
                function_args,
                function_result,
                failed=_is_error_result,
                tool_call_id=getattr(tool_call, "id", "") or "",
            )
            result_preview = function_result if agent.verbose_logging else (
                function_result[:200] if len(function_result) > 200 else function_result
            )
        if _is_error_result:
            logger.warning("Tool %s returned error (%.2fs): %s", function_name, tool_duration, result_preview)
        else:
            logger.info("tool %s completed (%.2fs, %d chars)", function_name, tool_duration, _result_len)

        # Track file-mutation outcome for the turn-end verifier.  See
        # the concurrent path for the rationale; both paths must feed
        # the same state so the footer reflects every tool call in the
        # turn, not just the parallel ones.
        if not _execution_blocked:
            try:
                agent._record_file_mutation_result(
                    function_name, function_args, function_result, _is_error_result,
                )
            except Exception as _ver_err:
                logging.debug("file-mutation verifier record failed: %s", _ver_err)

        agent._current_tool = None
        _status_suffix = " (error)" if _is_error_result else ""
        agent._touch_activity(f"tool completed: {function_name} ({tool_duration:.1f}s){_status_suffix}")

        if agent.verbose_logging:
            logging.debug("Tool %s completed in %.2fs", function_name, tool_duration)
            _log_result = _multimodal_text_summary(function_result)
            logging.debug("Tool result (%d chars): %s", len(_log_result), _log_result)

        display_function_result = function_result
        function_result = maybe_persist_tool_result(
            content=function_result,
            tool_name=function_name,
            tool_use_id=tool_call.id,
            env=get_active_env(effective_task_id),
            config=_tool_budget,
        ) if not _is_multimodal_tool_result(function_result) else function_result
        _record_persisted_path_for_stub(agent, tool_call.id, function_result)

        # Discover subdirectory context files from tool arguments
        subdir_hints = agent._subdirectory_hints.check_tool_call(function_name, function_args)
        if subdir_hints:
            if _is_multimodal_tool_result(function_result):
                _append_subdir_hint_to_multimodal(function_result, subdir_hints)
            else:
                function_result += subdir_hints

        # Unwrap _multimodal dicts to an OpenAI-style content list
        # (see parallel path for rationale). String results pass through.
        _tool_content = agent._tool_result_content_for_active_model(function_name, function_result)
        tool_message = make_tool_result_message(
            function_name,
            _tool_content,
            tool_call.id,
            effect_disposition="unknown" if _execution_timed_out else None,
        )
        messages.append(tool_message)
        risk_metadata = tool_message.get("_tool_output_risk")
        if not _flush_session_db_after_tool_progress(
            agent,
            messages,
            stage=f"tool result {function_name}",
        ):
            return

        # UI completion/progress events are projections of the canonical tool
        # row, never a competing in-memory authority.
        if not _execution_blocked and agent.tool_progress_callback:
            try:
                agent.tool_progress_callback(
                    "tool.completed", function_name, None, None,
                    duration=tool_duration, is_error=_is_error_result,
                    result=display_function_result,
                )
            except Exception as cb_err:
                logging.debug("Tool progress callback error: %s", cb_err)

        if not _execution_blocked and agent.tool_complete_callback:
            try:
                display_args = function_args
                agent.tool_complete_callback(
                    tool_call.id,
                    function_name,
                    display_args,
                    display_function_result,
                )
            except Exception as cb_err:
                logging.debug("Tool complete callback error: %s", cb_err)

        if (
            risk_metadata is not None
            and risk_metadata.get("risk") != "low"
            and agent.tool_progress_callback
        ):
            try:
                agent.tool_progress_callback(
                    "tool.output_risk",
                    function_name,
                    None,
                    None,
                    tool_call_id=tool_call.id,
                    risk_metadata=risk_metadata,
                )
            except Exception as cb_err:
                logging.debug("Tool output risk callback error: %s", cb_err)

        if not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off":
            if agent.verbose_logging:
                print(f"  ✅ Tool {i} completed in {tool_duration:.2f}s")
                print(agent._wrap_verbose("Result: ", function_result))
            else:
                _fr_str = function_result if isinstance(function_result, str) else str(function_result)
                response_preview = _fr_str[:agent.log_prefix_chars] + "..." if len(_fr_str) > agent.log_prefix_chars else _fr_str
                print(f"  ✅ Tool {i} completed in {tool_duration:.2f}s - {response_preview}")

        if agent._interrupt_requested and i < len(assistant_message.tool_calls):
            remaining = len(assistant_message.tool_calls) - i
            agent._vprint(f"{agent.log_prefix}⚡ Interrupt: skipping {remaining} remaining tool call(s)", force=True)
            for skipped_tc in assistant_message.tool_calls[i:]:
                skipped_name = skipped_tc.function.name
                messages.append(make_tool_result_message(
                    skipped_name,
                    f"[Tool execution skipped — {skipped_name} was not started. User sent a new message]",
                    skipped_tc.id,
                    effect_disposition="none",
                ))
                if not _flush_session_db_after_tool_progress(
                    agent,
                    messages,
                    stage=f"skipped tool result {skipped_name}",
                ):
                    return
            break

    # ── Per-turn aggregate budget enforcement ─────────────────────────
    # Keep /steer pending until the final post-budget drain below.  The model
    # only receives this batch after all calls finish, and an early drain can
    # be discarded when aggregate budget enforcement replaces a tool result.
    num_tools_seq = len(assistant_message.tool_calls)
    if finalize and num_tools_seq > 0:
        enforce_turn_budget(messages[-num_tools_seq:], env=get_active_env(effective_task_id), config=_tool_budget)

    # ── /steer injection ──────────────────────────────────────────────
    # See _execute_tool_calls_parallel for the rationale. Same hook,
    # applied to sequential execution as well.
    if finalize and num_tools_seq > 0:
        agent._apply_pending_steer_to_tool_results(messages, num_tools_seq)




def execute_tool_calls_segmented(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, segments=None) -> None:
    """Execute a mixed tool-call batch as ordered parallel/sequential segments.

    ``segments`` is the ``(kind, calls)`` plan from
    ``_plan_tool_batch_segments``: maximal contiguous runs of parallel-safe
    calls execute on the concurrent path, barrier calls on the sequential
    path, strictly in the model's original call order. Because segments are
    contiguous, every tool result is still appended one-per-call in emission
    order and no call ever starts before an earlier barrier finishes —
    identical ordering and side-effect boundaries to fully-sequential
    execution, with I/O parallelism recovered inside the safe runs.

    Turn-end work (aggregate budget enforcement + /steer injection) is done
    once here for the WHOLE batch; the per-segment executor calls run with
    ``finalize=False`` so a multi-segment turn cannot multiply the budget or
    truncate a steer marker.

    Interrupt semantics: each segment executor already checks
    ``agent._interrupt_requested`` up front and appends a cancelled/skipped
    result per call, so an interrupt during segment *k* drains segments
    *k+1..n* without executing them while preserving one result per
    tool_call_id.
    """
    from types import SimpleNamespace

    if segments is None:
        _active_env = get_active_env(effective_task_id)
        _exec_cwd = Path(_active_env.cwd) if _active_env is not None and _active_env.cwd else None
        segments = _plan_tool_batch_segments(assistant_message.tool_calls, execution_cwd=_exec_cwd)

    for kind, calls in segments:
        if getattr(agent, "_incremental_persistence_failed", False):
            return
        segment_message = SimpleNamespace(tool_calls=list(calls))
        if kind == "parallel":
            execute_tool_calls_concurrent(
                agent, segment_message, messages, effective_task_id, api_call_count,
                finalize=False,
            )
        else:
            execute_tool_calls_sequential(
                agent, segment_message, messages, effective_task_id, api_call_count,
                finalize=False,
            )

        if getattr(agent, "_incremental_persistence_failed", False):
            return

    # ── Whole-turn finalize (budget + /steer) ─────────────────────────
    total_tools = len(assistant_message.tool_calls)
    if total_tools > 0:
        _tool_budget = _budget_for_agent(agent)
        enforce_turn_budget(
            messages[-total_tools:],
            env=get_active_env(effective_task_id),
            config=_tool_budget,
        )
        agent._apply_pending_steer_to_tool_results(messages, total_tools)


__all__ = [
    "execute_tool_calls_concurrent",
    "execute_tool_calls_sequential",
    "execute_tool_calls_segmented",
]
