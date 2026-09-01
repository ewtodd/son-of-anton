"""Phase-2 Textual spike contracts.

The Textual front-end is opt-in (extra ``tui`` / lazy feature ``tui.textual``),
so its two hard contracts must hold whether or not Textual is installed:

  * importing the module never crashes on a lean install, and
  * the availability flag is consistent with the app class being defined.

When Textual IS present, the streaming-markdown demo is exercised end-to-end via
``App.run_test``.
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


def test_spike_demo_renders_streaming_markdown() -> None:
    if not _tui.is_available():
        pytest.skip("textual not installed (opt-in tui extra); spike proven in a throwaway venv")

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.2)
            transcript = getattr(app, "_transcript", "")
            assert "Why this matters" in transcript   # heading rendered
            assert "```python" in transcript          # fenced code kept
            assert "|---|---|" in transcript           # table kept
            assert "\\int_0^" in transcript            # LaTeX source kept
            assert app.query_one("#input") is not None  # input dock present

    asyncio.run(run())
