"""Phase-2 Textual spike contracts.

The Textual front-end is opt-in (extra ``tui`` / lazy feature ``tui.textual``),
so its two hard contracts must hold whether or not Textual is installed:

  * importing the module never crashes on a lean install, and
  * the availability flag is consistent with the app class being defined.

When Textual IS present, the layout is exercised end-to-end via ``App.run_test``:
an opencode-inspired frame (wide terminals get a right-hand context panel), a
terminal-native ``ansi`` theme, the ASCII wordmark, a multi-line prompt that
submits on Enter, an unhighlighted transcript, and ``:q`` to quit.
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


def test_tui_layout_streams_and_handles_a_turn() -> None:
    if not _tui.is_available():
        pytest.skip("textual not installed (opt-in tui extra); spike proven in a throwaway venv")

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(160, 40)) as pilot:  # wide -> sidebar on
            await pilot.pause(2.2)
            transcript = app._transcript
            # streaming markdown surfaced in the transcript accumulator
            assert "streaming markdown" in transcript
            assert "```python" in transcript      # fenced code kept
            assert "|---|---|" in transcript       # table kept
            assert "int_0^" in transcript          # LaTeX source kept

            # opencode-inspired frame: a 42-col right context panel when wide,
            # and the ASCII wordmark at the top of the transcript.
            ctx = app.query_one("#context")
            assert ctx.display is True, "sidebar should be visible at width 160"
            assert ctx.styles.width.value == 42.0
            assert any(w.has_class("wordmark") for w in app.query_one("#feed").children)

            # terminal-native chrome: an ansi-* theme and no hardcoded hex surfaces.
            assert app.theme.startswith("ansi-"), f"not an ansi theme: {app.theme}"
            vars_ = app.get_css_variables()
            for key in ("background", "panel", "surface", "text", "foreground"):
                assert "#" not in vars_.get(key, ""), f"{key} hardcodes a hex: {vars_.get(key)!r}"

            # fenced code renders as plain text (no syntax highlighting).
            assert _tui.PlainMarkdown.BLOCKS["fence"] is _tui._PlainFence

            # the prompt auto-focuses on mount so keystrokes land.
            assert app.focused is app.query_one("#input"), "prompt not focused on mount"

            # a multi-line prompt submits on Enter.
            prompt = app.query_one("#input")
            prompt.text = "hello from pilot"
            await pilot.press("enter")
            await pilot.pause(1.6)

            feed = app.query_one("#feed")
            assert [w for w in feed.children if w.has_class("user")], "user block not mounted"
            assert "canned reply" in app._transcript, "assistant turn not streamed"

    asyncio.run(run())


def test_tui_sidebar_responsive_and_q_quits() -> None:
    if not _tui.is_available():
        pytest.skip("textual not installed (opt-in tui extra); spike proven in a throwaway venv")

    async def narrow() -> None:
        # Below the threshold the right panel disappears.
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.5)
            assert app.query_one("#context").display is False

    async def wide_and_quit() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.5)
            assert app.query_one("#context").display is True
            # :q quits (typed into the prompt, then Enter to submit).
            prompt = app.query_one("#input")
            prompt.focus()
            await pilot.press(":", "q")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert not app.is_running, ":q did not quit the app"

    asyncio.run(narrow())
    asyncio.run(wide_and_quit())
