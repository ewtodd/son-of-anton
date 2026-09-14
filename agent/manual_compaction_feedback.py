"""User-facing summaries for manual compaction commands."""

from __future__ import annotations

from typing import Any, Sequence

from agent.redact import redact_sensitive_text


def describe_compaction_lock_skip(lock_signal: Any) -> str:
    """User-facing text for a manual /compact skipped by the compaction lock.

    ``lock_signal`` is ``agent._compaction_skipped_due_to_lock`` (or the
    ``holder`` carried by the TUI's ``CompactionLockHeld``): a descriptive
    holder string when another compactor CONFIRMED holds the lock, or
    ``True``/``None`` when acquisition failed without a confirmed holder
    (``son_of_anton_state.try_acquire_compaction_lock`` catches ``sqlite3.Error``
    internally and returns ``False``, so a failed acquire is NOT proof that
    another compaction is running). The two cases must be worded
    differently: claiming "already in progress" on an unconfirmed failure
    misdirects the user when the real problem is a broken lock subsystem.
    """
    holder = (
        lock_signal
        if isinstance(lock_signal, str) and lock_signal.strip()
        else None
    )
    if holder:
        return (
            f"⏳ Compaction already in progress for this session "
            f"(holder: {holder}). Please wait for it to finish."
        )
    return (
        "⏳ Compaction skipped: could not acquire this session's "
        "compaction lock. Another compaction may still be running, or "
        "the lock check failed — try again shortly."
    )


def summarize_manual_compaction(
    before_messages: Sequence[dict[str, Any]],
    after_messages: Sequence[dict[str, Any]],
    before_tokens: int,
    after_tokens: int,
    *,
    compaction_state: Any = None,
) -> dict[str, Any]:
    """Return consistent user-facing feedback for manual compaction."""
    before_count = len(before_messages)
    after_count = len(after_messages)
    noop = list(after_messages) == list(before_messages)
    aborted = (
        compaction_state is not None
        and getattr(compaction_state, "_last_compact_aborted", False) is True
    )
    refused_would_grow = (
        compaction_state is not None
        and getattr(compaction_state, "_last_compact_refused_would_grow", False)
        is True
    )
    fallback_used = (
        compaction_state is not None
        and getattr(compaction_state, "_last_summary_fallback_used", False) is True
    )
    failure_reason = (
        getattr(compaction_state, "_last_summary_error", None)
        if compaction_state is not None
        else None
    )
    if not isinstance(failure_reason, str) or not failure_reason.strip():
        failure_reason = None

    if refused_would_grow:
        headline = (
            f"Compaction refused (summary would grow the conversation): "
            f"{before_count} messages preserved"
        )
    elif aborted:
        headline = f"Compaction aborted: {before_count} messages preserved"
    elif fallback_used:
        headline = (
            f"Compacted with fallback: {before_count} → {after_count} messages"
        )
    elif noop:
        headline = f"No changes from compaction: {before_count} messages"
    else:
        headline = f"Compacted: {before_count} → {after_count} messages"

    if noop and after_tokens == before_tokens:
        token_line = f"Approx request size: ~{before_tokens:,} tokens (unchanged)"
    elif refused_would_grow:
        token_line = f"Approx request size: ~{before_tokens:,} tokens (unchanged)"
    else:
        token_line = (
            f"Approx request size: ~{before_tokens:,} → "
            f"~{after_tokens:,} tokens"
        )

    note = None
    if refused_would_grow:
        note = (
            "The generated summary was larger than what it would replace; "
            "no messages were removed."
        )
    elif aborted:
        note = "Summary generation failed; no messages were removed."
    elif fallback_used:
        dropped_count = getattr(
            compaction_state, "_last_summary_dropped_count", None
        )
        if not isinstance(dropped_count, int) or isinstance(dropped_count, bool):
            dropped_count = max(before_count - after_count, 0)
        note = (
            "Summary generation failed; Son of Anton used limited fallback context "
            f"and removed {dropped_count} message(s)."
        )
    elif not noop and after_count < before_count and after_tokens > before_tokens:
        note = (
            "Note: fewer messages can still raise this estimate when "
            "compaction rewrites the transcript into denser summaries."
        )

    if failure_reason and (aborted or fallback_used):
        # This text crosses a user-facing UI boundary.  Never let a disabled
        # global redaction preference expose credentials embedded in provider
        # exception text.
        safe_reason = redact_sensitive_text(failure_reason.strip(), force=True)
        note = f"{note} Reason: {safe_reason}"

    return {
        "noop": noop,
        "aborted": aborted,
        "refused_would_grow": refused_would_grow,
        "fallback_used": fallback_used,
        "headline": headline,
        "token_line": token_line,
        "note": note,
    }
