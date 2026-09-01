"""Phase-2 Textual spike contracts.

The Textual front-end is opt-in (extra ``tui`` / lazy feature ``tui.textual``),
so its two hard contracts must hold whether or not Textual is installed:

  * importing the module never crashes on a lean install, and
  * the availability flag is consistent with the app class being defined.

When Textual IS present, the layout is exercised end-to-end via ``App.run_test``:
streaming markdown, the context panel, a user turn, and the canned reply.
"""

from __future__ import annotations

import asyncio

import pytest

from son_of_anton_tui import tui as _tui


def test_module_import_never_crashes_and_flags_agree() -> None:
    # Importing the module succeeds on a lean install (no textual) too.
    assert _tui.is_available() == (_tui.App is not None)
    if not _tui.is_available():
        # Without Textual the app class must be a consistent None, not missing.
        assert _tui.SonOfAntonTUIApp is None
    else:
        assert _tui.SonOfAntonTUIApp is not None


def test_tui_layout_streams_markdown_and_handles_a_turn() -> None:
    if not _tui.is_available():
        pytest.skip("textual not installed (opt-in tui extra); spike proven in a throwaway venv")

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.2)
            transcript = app._transcript
            # streaming markdown surfaced in the transcript accumulator
            assert "streaming markdown" in transcript
            assert "```python" in transcript      # fenced code kept
            assert "|---|---|" in transcript       # table kept
            assert "\\int_0^" in transcript        # LaTeX source kept
            # context panel + input dock present
            assert app.query_one("#context") is not None
            assert app.query_one("#input") is not None

            # drive a real user turn
            app.query_one("#input").focus()
            app.query_one("#input").value = "hello from pilot"
            await pilot.press("enter")
            await pilot.pause(1.5)

            feed = app.query_one("#feed")
            assert [w for w in feed.children if w.has_class("user")], "user block not mounted"
            assert "canned reply" in app._transcript, "assistant turn not streamed"

    asyncio.run(run())
