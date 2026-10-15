"""The cron trigger: the gateway's in-process 60s ticker.

The ticker decides *when* a due job fires. It does NOT decide what firing
means: execution + delivery stay in cron.scheduler.run_job / _deliver_result.
The gateway runs ``InProcessCronScheduler.start`` in a daemon thread.
"""
from __future__ import annotations

# Cap for the exponential tick backoff applied while consecutive ticks fail
# with fd exhaustion (EMFILE/ENFILE, #87644).  Base is the tick interval
# (60s by default); each consecutive EMFILE failure doubles the wait, capped
# here so a still-alive-but-exhausted gateway never sleeps longer than this
# between recovery attempts.
_EMFILE_BACKOFF_MAX_SECONDS = 15 * 60  # 15 minutes


def _backoff_wait_seconds(interval: float, consecutive_failures: int) -> float:
    """Exponential tick backoff for the ticker loop (#87644).

    Returns the plain ``interval`` while healthy; doubles per consecutive
    fd-exhaustion failure, capped at ``_EMFILE_BACKOFF_MAX_SECONDS``.
    """
    if consecutive_failures <= 0:
        return interval
    return min(
        interval * (2 ** (consecutive_failures - 1)),
        _EMFILE_BACKOFF_MAX_SECONDS,
    )


def _note_tick_failure(exc: BaseException, consecutive_failures: int) -> int:
    """Classify one failed tick and return the updated failure counter.

    On fd exhaustion (#87644), attempt
    reclamation (gc.collect + raise the soft nofile limit) so the NEXT tick
    can succeed, and bump the counter so ``_backoff_wait_seconds`` backs off
    exponentially while the process has no chance of making progress.  Any
    other failure resets the counter — backoff is reserved for the
    self-inflicted EMFILE storm, not transient errors.
    """
    from cron.scheduler import _is_fd_exhaustion, _reclaim_fds_best_effort

    if _is_fd_exhaustion(exc):
        _reclaim_fds_best_effort()
        return consecutive_failures + 1
    return 0


class InProcessCronScheduler:
    """The in-process 60s ticker.

    ``start()`` blocks in the tick loop until ``stop_event`` is set. The caller
    runs it in a daemon thread. ``can_dispatch`` is an optional synchronous
    gate supplied by GatewayRunner during external drain; skipped ticks leave
    due jobs intact for the next allowed tick.
    """

    def start(
        self,
        stop_event,
        *,
        adapters=None,
        loop=None,
        interval=60,
        can_dispatch=None,
    ):
        import logging
        from cron.executions import recover_interrupted_executions
        from cron.scheduler import tick as cron_tick
        from cron.jobs import (
            clear_ticker_error,
            record_ticker_error,
            record_ticker_heartbeat,
        )

        logger = logging.getLogger("cron.scheduler_provider")
        logger.info("In-process cron scheduler started (interval=%ds)", interval)

        recovered = recover_interrupted_executions()
        if recovered:
            logger.warning(
                "Marked %d interrupted cron execution(s) unknown after restart",
                recovered,
            )
        # Heartbeat once before the first sleep so `son-of-anton cron status` sees a
        # live ticker immediately after startup, not only after the first tick.
        record_ticker_heartbeat()
        # Exponential backoff for consecutive tick failures — most importantly
        # fd exhaustion (EMFILE/ENFILE, #87644).  While FDs stay exhausted the
        # ticker must NOT hammer the store every 60s; once they free (leak
        # fixed, reclamation ran) the next tick succeeds and the backoff
        # resets, so the scheduler self-heals without a gateway restart.
        consecutive_failures = 0
        while not stop_event.is_set():
            ok = False
            try:
                if can_dispatch is not None and not can_dispatch():
                    logger.debug("Cron dispatch paused while gateway drains existing work")
                else:
                    cron_tick(
                        verbose=False,
                        adapters=adapters,
                        loop=loop,
                        sync=False,
                        can_dispatch=can_dispatch,
                    )
                ok = True
            except BaseException as e:
                # Catch BaseException (not just Exception) so a SystemExit from
                # a misbehaving provider SDK / agent retry path does not kill
                # the ticker thread silently (#32612). KeyboardInterrupt is
                # intentionally caught here too — gateway shutdown is driven by
                # stop_event (set by the main thread's signal handler), not by
                # an exception in this daemon thread, so swallowing it and
                # re-checking stop_event keeps shutdown clean.
                logger.error("Cron tick error: %s", e, exc_info=True)
                # Persist the failure reason next to the heartbeat markers so
                # `son-of-anton cron status`/`list` (separate processes) can show
                # WHY ticks fail, not just that the success marker is stale —
                # e.g. a root-rewritten jobs.json locking out the ticker's
                # uid went unnoticed for ~14h with the reason buried in the
                # gateway log (#68483).
                record_ticker_error(f"{type(e).__name__}: {e}")
                # EMFILE: reclaim fds + back off exponentially so the
                # exhausted process stops hammering the store while it has no
                # chance of making progress (#87644).
                consecutive_failures = _note_tick_failure(e, consecutive_failures)
            # Record liveness every iteration; bump the success marker only on a
            # clean tick, so status can tell "alive but failing every tick" from
            # "actually firing jobs" (#32612, #32895).
            record_ticker_heartbeat(success=ok)
            if ok:
                clear_ticker_error()
                consecutive_failures = 0
            stop_event.wait(_backoff_wait_seconds(interval, consecutive_failures))

