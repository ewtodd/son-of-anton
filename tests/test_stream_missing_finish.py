"""A missing ``finish_reason`` is not by itself a mid-stream drop.

The provider is asked for a terminal usage chunk
(``stream_options={"include_usage": True}``), which arrives after the
finish_reason chunk. A proxy can forward usage while dropping the
finish_reason chunk; treating that as a drop re-sends the turn with the
"continue exactly where you left off" nudge, and a model that had already
finished reworks its own closing lines instead of stopping.
"""

from __future__ import annotations

from agent.chat_completion_helpers import _text_stream_end_is_drop


def test_terminal_usage_chunk_marks_the_text_stream_complete() -> None:
    assert _text_stream_end_is_drop(["the answer"], usage_received=True) is False


def test_absent_usage_chunk_still_routes_to_drop_recovery() -> None:
    assert _text_stream_end_is_drop(["the answer"], usage_received=False) is True


def test_no_text_never_enters_the_text_drop_path() -> None:
    assert _text_stream_end_is_drop([], usage_received=False) is False
