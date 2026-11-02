"""Textual front-end contracts.

Two hard contracts hold whether or not Textual is installed:

  * importing ``son_of_anton_tui.tui`` never crashes on a lean install, and
  * the availability flag agrees with the app class being defined.

With Textual present the layout is exercised end-to-end through
``App.run_test``: the opencode-inspired frame (wide terminals get a 42-col
right panel), a terminal-native ``ansi`` theme, the ASCII wordmark, the
multi-line prompt with its ↑/↓ history recall, slash completion, the backend
event stream (assistant markdown, reasoning, tool rows, ANSI lines), the modal
screens, and ``:q``.

The ``backend`` fixture is a ``DaemonBackend`` on a real ``soa-d`` in a temp
home, for the tests that need a backend only as a host for app-local behaviour.
The daemon-backed behaviour itself (turns, queue, modals, commands) is
tests/test_tui_daemon.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from son_of_anton_tui import tui as _tui


def _textual() -> None:
    if not _tui.is_available():
        pytest.skip("textual not installed (opt-in tui extra)")


def test_module_import_never_crashes_and_flags_agree() -> None:
    assert _tui.is_available() == (_tui.App is not None)
    if not _tui.is_available():
        assert _tui.SonOfAntonTUIApp is None
    else:
        assert _tui.SonOfAntonTUIApp is not None


# ---------------------------------------------------------------------------
# Frame
# ---------------------------------------------------------------------------

def test_frame_is_terminal_native_and_responsive() -> None:
    _textual()

    async def wide() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.3)
            ctx = app.query_one("#context")
            assert ctx.display is True, "sidebar should be visible at width 160"
            assert ctx.styles.width.value == 42.0
            assert app.theme.startswith("ansi-"), f"not an ansi theme: {app.theme}"
            vars_ = app.get_css_variables()
            for key in ("background", "panel", "surface", "text", "foreground"):
                assert "#" not in vars_.get(key, ""), f"{key} hardcodes a hex: {vars_.get(key)!r}"
            assert app.focused is app.query_one("#input"), "prompt not focused on mount"

    async def narrow() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.3)
            assert app.query_one("#context").display is False

    asyncio.run(wide())
    asyncio.run(narrow())


def test_backend_events_render_into_the_feed() -> None:
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.2)
            post = lambda kind, **p: app.post_message(_tui.TuiEvent(kind, p))  # noqa: E731
            post("reasoning_start")
            post("reasoning_delta", text="let me think\nabout this")
            post("reasoning_end")
            post("tool_gen", name="terminal")
            post("tool_start", name="terminal", label="ls -la")
            post("tool_done", name="terminal", line="\x1b[2m┊ ran ls -la  0.3s\x1b[0m", duration=0.3)
            post("assistant_start")
            for chunk in ("## Hello\n\n", "streaming **markdown** ", "works\n\n```python\nprint(1)\n```\n",
                          "| a | b |\n|---|---|\n| 1 | 2 |\n"):
                post("assistant_delta", text=chunk)
            post("assistant_end")
            post("ansi", text="\x1b[1;33mnote one\x1b[0m")
            post("ansi", text="note two")
            post("turn_end")
            await pilot.pause(0.8)

            feed = app.query_one("#feed")
            kinds = [b.kind for b in feed._blocks]
            assert kinds.count("reasoning") == 1
            assert kinds.count("tool") == 1, "tool_gen + tool_start + tool_done must share one row"
            assert kinds.count("markdown") == 1
            reasoning = next(b for b in feed._blocks if b.kind == "reasoning")
            assert reasoning.collapsed, "reasoning folds once the answer starts"
            assert "▶ reasoning · 2 lines" in "".join(s.text for s in reasoning.lines)
            tool = next(b for b in feed._blocks if b.kind == "tool")
            assert tool.done
            notes = [b for b in feed._blocks if b.kind == "note"]
            assert len(notes) == 1, "consecutive lines merge into one block"
            joined = "".join(s.text for s in notes[0].lines)
            assert "note one" in joined and "note two" in joined
            assert "streaming **markdown**" in app._transcript
            assert "```python" in app._transcript
            assert "|---|---|" in app._transcript

    asyncio.run(run())


def test_prompt_completes_slash_commands_and_q_quits() -> None:
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            prompt = app.query_one("#input")
            prompt.focus()
            await pilot.press("/", "m", "o")
            await pilot.pause(0.2)
            assert app.query_one("#completer").display is True
            offered = [c.display for c in app._completions.items]
            assert "/model" in offered
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert prompt.text == offered[0] + " "
            assert app.query_one("#completer").display is False
            prompt.text = ""
            await pilot.press("h", "i")
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert any(b.kind == "user" for b in app.query_one("#feed")._blocks)
            prompt.text = ":q"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert not app.is_running, ":q did not quit the app"

    asyncio.run(run())


def _keys(text: str) -> list:
    return ["space" if c == " " else c for c in text]


def test_tab_never_leaves_the_prompt() -> None:
    """Tab with nothing to complete used to move focus to the transcript and
    swallow every key typed after it."""
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            prompt = app.query_one("#input")
            prompt.focus()
            await pilot.press(*_keys("/model qw"), "tab", "e", "n")
            await pilot.pause(0.2)
            assert app.focused is prompt
            assert prompt.text == "/model qwen"
            prompt.text = ""
            await pilot.press(*_keys("hello"), "tab", "!")
            await pilot.pause(0.2)
            assert app.focused is prompt and prompt.text == "hello!"

    asyncio.run(run())


def test_prompt_completes_command_arguments() -> None:
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            prompt = app.query_one("#input")
            completer = app.query_one("#completer")
            prompt.focus()
            await pilot.press(*_keys("/reasoning hi"))
            await pilot.pause(0.2)
            assert completer.display is True
            assert "high" in [c.display for c in app._completions.items]
            await pilot.press("tab")
            await pilot.pause(0.2)
            assert prompt.text == "/reasoning high "

            # An empty argument opens nothing while typing — Enter must still
            # send `/reasoning` — but Tab lists every choice.
            prompt.text = ""
            await pilot.press(*_keys("/reasoning "))
            await pilot.pause(0.2)
            assert completer.display is False
            await pilot.press("tab")
            await pilot.pause(0.2)
            assert completer.display is True
            first = app._completions.items[0].display
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert prompt.text == f"/reasoning {first} "

    asyncio.run(run())


def test_prompt_completes_at_references_and_paths(tmp_path, monkeypatch) -> None:
    _textual()
    (tmp_path / "src").mkdir()
    (tmp_path / "notes.txt").write_text("n\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            prompt = app.query_one("#input")
            completer = app.query_one("#completer")
            prompt.focus()
            await pilot.press(*_keys("look at @"))
            await pilot.pause(0.2)
            assert completer.display is True
            assert "@diff" in [c.display for c in app._completions.items]

            prompt.text = ""
            await pilot.press(*_keys("look at @file:no"))
            await pilot.pause(0.2)
            await pilot.press("enter")  # accepts the reference, does not send
            await pilot.pause(0.2)
            assert prompt.text == "look at @file:notes.txt "

            # Plain paths complete on Tab only, keeping what follows the cursor.
            prompt.text = ""
            await pilot.press(*_keys("see ./no"))
            await pilot.pause(0.2)
            assert completer.display is False
            await pilot.press("tab")
            await pilot.pause(0.2)
            assert prompt.text == "see ./notes.txt "

    asyncio.run(run())


def test_history_shows_as_ghost_text_and_right_takes_it() -> None:
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            prompt = app.query_one("#input")
            prompt.focus()
            prompt.prompt_history.record("explain the relay worker")
            await pilot.press(*_keys("expl"))
            await pilot.pause(0.2)
            assert prompt.suggestion == "ain the relay worker"
            await pilot.press("left")
            await pilot.pause(0.1)
            assert prompt.suggestion == "", "ghost text lives only at the end of the draft"
            await pilot.press("end", "right")
            await pilot.pause(0.2)
            assert prompt.text == "explain the relay worker"
            assert prompt.suggestion == ""

    asyncio.run(run())


def test_text_we_did_not_author_is_never_parsed_as_markup() -> None:
    """A stray ``[`` in model prose used to take the whole app down.

    ``Static.update`` runs Textual's content-markup parser by default, so one
    bracket in a reasoning stream (or in a tool-approval title) raised
    ``MarkupError`` out of the render and killed the session mid-turn.  Every
    Static that shows text from the model, the filesystem or the user is
    ``markup=False``.
    """
    _textual()

    hostile = "run `ls [-a]` — the [bold] flag is unclosed"

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.2)
            post = lambda kind, **p: app.post_message(_tui.TuiEvent(kind, p))  # noqa: E731
            post("reasoning_start")
            post("reasoning_delta", text=hostile)
            post("reasoning_end")
            await pilot.pause(0.4)
            assert app.is_running, "a bracket in the reasoning stream crashed the app"
            block = next(b for b in app.query_one("#feed")._blocks if b.kind == "reasoning")
            assert hostile in block.buffer

            app.push_screen(_tui.ChoiceModal(hostile, [("ok", "OK", "")], detail=hostile), lambda r: None)
            await pilot.pause(0.3)
            assert app.is_running, "a bracket in a modal title crashed the app"
            await pilot.press("escape")
            await pilot.pause(0.2)

            app.query_one("#context")  # the sidebar renders session titles verbatim
            app.query_one("#ctx-title").update(hostile)
            app.query_one("#ctx-cwd").update(hostile)
            await pilot.pause(0.2)
            assert app.is_running, "a bracket in the sidebar crashed the app"

    asyncio.run(run())


def test_prompt_history_round_trips_the_repl_file(tmp_path) -> None:
    """Recall reads and writes prompt_toolkit's format, so old history survives."""
    _textual()

    path = tmp_path / ".son_of_anton_history"
    path.write_text("\n# 2026-01-01 00:00:00.000000\n+older\n")

    hist = _tui.PromptHistory(path)
    assert hist._entries == ["older"]

    hist.record("first")
    hist.record("second\nline two")
    hist.record("second\nline two")  # a repeat must not stack up

    reloaded = _tui.PromptHistory(path)
    assert reloaded._entries == ["older", "first", "second\nline two"]

    # ↑ walks back, stopping at the oldest; ↓ walks forward and hands the
    # in-progress draft back at the end.
    assert reloaded.prev("draft") == "second\nline two"
    assert reloaded.prev("") == "first"
    assert reloaded.prev("") == "older"
    assert reloaded.prev("") is None, "there is nothing older to reach"
    assert reloaded.next("") == "first"
    assert reloaded.next("") == "second\nline two"
    assert reloaded.next("") == "draft", "past the newest entry the draft comes back"
    assert reloaded.next("") is None

    # No file (no backend attached) is fine: recall just stays in memory.
    memory = _tui.PromptHistory(None)
    memory.record("only here")
    assert memory.prev("") == "only here"


def test_up_arrow_recalls_previous_prompts() -> None:
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            prompt = app.query_one("#input")
            prompt.focus()
            for message in ("first message", "second message"):
                prompt.text = message
                await pilot.press("enter")
                await pilot.pause(0.2)
            assert prompt.text == ""

            await pilot.press("up")
            await pilot.pause(0.1)
            assert prompt.text == "second message"
            await pilot.press("up")
            await pilot.pause(0.1)
            assert prompt.text == "first message"
            await pilot.press("up")
            await pilot.pause(0.1)
            assert prompt.text == "first message", "the oldest entry is the floor"
            await pilot.press("down")
            await pilot.pause(0.1)
            assert prompt.text == "second message"
            await pilot.press("down")
            await pilot.pause(0.1)
            assert prompt.text == "", "past the newest entry the empty draft returns"

            # A recalled slash command must not leave the completion list open,
            # or the next ↑ would walk that list instead of the history.
            prompt.text = "/model"
            await pilot.press("enter")
            await pilot.pause(0.2)
            prompt.text = ""
            await pilot.press("up")
            await pilot.pause(0.2)
            assert prompt.text == "/model"
            assert app.query_one("#completer").display is False

            # Inside a multi-line draft the arrows still move the cursor.
            prompt.text = "line one\nline two"
            prompt.move_cursor(prompt.document.end)
            await pilot.press("up")
            await pilot.pause(0.1)
            assert prompt.text == "line one\nline two", "↑ mid-draft must not recall"
            assert prompt.cursor_location[0] == 0

    asyncio.run(run())


def test_selecting_in_the_feed_copies_and_keeps_the_prompt_focused() -> None:
    """Drag-select copies like a terminal does, and never steals the caret."""
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            post = lambda kind, **p: app.post_message(_tui.TuiEvent(kind, p))  # noqa: E731
            post("ansi", text="a line worth selecting")
            await pilot.pause(0.3)
            prompt = app.query_one("#input")
            assert app.focused is prompt

            feed = app.query_one("#feed")
            note = next(b for b in feed._blocks if b.kind == "note")
            y = feed._starts[note.idx]
            # note text is padded to column 3; drag across cells 3..22
            await pilot.mouse_down(feed, offset=(3, y))
            await pilot.hover(feed, offset=(22, y))
            await pilot.mouse_up(feed, offset=(22, y))
            await pilot.pause(0.3)

            assert app.clipboard.strip() == "a line worth select", (
                f"selection copied {app.clipboard.strip()!r}"
            )
            assert app.focused is prompt, "selecting stole focus from the prompt"

            # A plain click anywhere in the transcript keeps the caret too.
            await pilot.click("#feed")
            await pilot.pause(0.2)
            assert app.focused is prompt, "clicking the feed stole focus from the prompt"

    asyncio.run(run())


def test_skin_colour_mapping_is_idempotent_for_every_builtin_skin() -> None:
    """``_rich_color`` re-maps ``_skin_accent``'s output, so mapping must be stable.

    ``_to_ansi`` rejected its own ``ansi_<name>`` tokens, so every transcript
    colour came out empty and reasoning, tool rows and text all rendered in
    the default foreground.
    """
    from son_of_anton_cli.skin_engine import list_skins, load_skin

    for entry in list_skins():
        skin = load_skin(entry["name"])
        for key in ("ui_accent", "ui_thinking", "ui_tool", "ui_error"):
            once = _tui._to_ansi(skin.get_color(key, ""), "ansi_default")
            assert _tui._to_ansi(once, "") == once, (entry["name"], key, once)
            assert _tui._rich_color(once), f"{entry['name']}.{key} lost its colour"


def test_reasoning_tool_rows_and_answers_render_in_distinct_styles() -> None:
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause(0.2)
            feed = app.query_one("#feed")
            reasoning = feed.open_reasoning()
            reasoning.append("thinking")
            reasoning.finish()
            feed.changed(reasoning)
            tool = feed.open_tool("read_file")
            tool.finish("→", "Reading foo.py", False, 0.1)
            feed.changed(tool)
            answer = feed.open_markdown()
            answer.append("The answer.")
            feed.flush()
            await pilot.pause(0.2)

            def style_of(block, text: str):
                return next(seg.style for seg in block.lines[0] if text in seg.text)

            reasoning_style = style_of(reasoning, "reasoning")
            tool_label_style = style_of(tool, "Reading")
            answer_style = style_of(answer, "answer")
            assert reasoning_style and reasoning_style.color is not None, "reasoning lost its colour"
            assert len({str(reasoning_style), str(tool_label_style), str(answer_style)}) == 3, (
                reasoning_style, tool_label_style, answer_style,
            )

    asyncio.run(run())


def test_reasoning_folds_and_a_click_reopens_it() -> None:
    """Reasoning keeps the old Collapsible affordance in the virtual feed.

    The virtualization port rendered reasoning as bare lines, dropping the
    header/arrow and the click-to-expand interaction. The folded header must
    read ``▶ reasoning · N lines`` and a click on it must show the body again
    (``▼ reasoning``), then fold it back.
    """
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            post = lambda kind, **p: app.post_message(_tui.TuiEvent(kind, p))  # noqa: E731
            post("reasoning_start")
            post("reasoning_delta", text="let me think\nabout this")
            post("reasoning_end")
            await pilot.pause(0.3)

            feed = app.query_one("#feed")
            block = next(b for b in feed._blocks if b.kind == "reasoning")
            assert block.collapsed, "reasoning folds once it ends"
            folded = "".join(s.text for s in block.lines)
            assert "▶ reasoning · 2 lines" in folded, folded

            y = feed._starts[block.idx]
            await pilot.click(feed, offset=(5, y))
            await pilot.pause(0.3)
            assert not block.collapsed, "a click on the folded header must reopen it"
            expanded = "".join(s.text for s in block.lines)
            assert "▼ reasoning" in expanded and "let me think" in expanded, expanded

            await pilot.click(feed, offset=(5, y))
            await pilot.pause(0.3)
            assert block.collapsed, "a click on the header must fold it again"

    asyncio.run(run())


def test_a_click_unfolds_a_tool_row_cut_by_a_narrow_window() -> None:
    """A tool row is one line cut to the window; a click shows the whole call.

    In a narrow window a long command ended in ``…`` and there was no way to
    read the rest without resizing. A click must unfold the row to the full
    call, wrapped under the label clear of the icon, and a second click fold it.
    """
    _textual()
    command = "cd /home/e-work/Remix-MUSIC-Sim && ./run37Cl.sh && ./run87Rb.sh && echo batch-finished"

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(60, 30)) as pilot:
            await pilot.pause(0.2)
            post = lambda kind, **p: app.post_message(_tui.TuiEvent(kind, p))  # noqa: E731
            post("tool_start", name="terminal", label=command)
            post("tool_done", name="terminal", label=command, duration=1.5)
            await pilot.pause(0.3)

            feed = app.query_one("#feed")
            block = next(b for b in feed._blocks if b.kind == "tool")
            assert len(block.lines) == 1
            folded = block.lines[0].text
            assert folded.rstrip().endswith("…") and "batch-finished" not in folded, folded

            y = feed._starts[block.idx]
            await pilot.click(feed, offset=(5, y))
            await pilot.pause(0.3)
            assert len(block.lines) > 1, "a click on the row must unfold it"
            unfolded = [s.text for s in block.lines]
            # Wrapping turns spaces into line breaks; the call itself is all there.
            assert command.replace(" ", "") in "".join(unfolded).replace(" ", ""), unfolded
            assert all(line.startswith("  ") for line in unfolded[1:]), "continuation must hang under the label"

            await pilot.click(feed, offset=(5, y))
            await pilot.pause(0.3)
            assert len(block.lines) == 1, "a second click must fold it again"

    asyncio.run(run())


def test_transcript_paints_from_the_scroll_offset() -> None:
    """A Line API widget gets viewport rows and must add scroll_y itself.

    The virtual feed treated ``render_line``'s y as a content line, so the
    transcript always painted from line 0: the scrollbar, wheel and anchor all
    moved ``scroll_y`` but the screen never changed. Scrolling to the top and
    to the bottom must paint different first lines.
    """
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause(0.2)
            post = lambda kind, **p: app.post_message(_tui.TuiEvent(kind, p))  # noqa: E731
            for i in range(40):
                post("ansi", text=f"note line {i:02d}")
            await pilot.pause(0.6)
            feed = app.query_one("#feed")
            assert feed.max_scroll_y > 0, "the fixture must overflow the viewport"

            def plain(strip) -> str:
                return "".join(seg.text for seg in strip if not seg.control)

            assert feed.scroll_y == feed.max_scroll_y, "the feed should start anchored"
            at_bottom = plain(feed.render_line(0))

            feed.scroll_home(animate=False)
            await pilot.pause(0.3)
            assert feed.scroll_y == 0
            at_top = plain(feed.render_line(0))
            assert at_top != at_bottom, "painting ignored scroll_y"
            assert "█" in at_top, f"top of the transcript is not the wordmark: {at_top!r}"

    asyncio.run(run())


def test_page_keys_scroll_the_feed_with_the_prompt_focused() -> None:
    """PageUp/PageDown must reach the transcript, not the prompt's TextArea."""
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause(0.2)
            post = lambda kind, **p: app.post_message(_tui.TuiEvent(kind, p))  # noqa: E731
            for i in range(40):
                post("ansi", text=f"note line {i:02d}")
            await pilot.pause(0.6)
            feed = app.query_one("#feed")
            assert app.focused is app.query_one("#input")
            assert feed.max_scroll_y > 0
            assert feed.scroll_y == feed.max_scroll_y, "the feed should start anchored"

            await pilot.press("pageup")
            await pilot.pause(0.3)
            assert feed.scroll_y < feed.max_scroll_y, "pageup did not scroll the feed"
            assert feed._anchor_released, "pageup must release the follow anchor"

            await pilot.press("pagedown")
            await pilot.pause(0.3)
            assert feed.scroll_y > 0, "pagedown did not scroll the feed"

    asyncio.run(run())


def test_colon_q_quits_immediately_even_mid_turn(backend) -> None:
    """Quit must never be queued behind a running turn.

    A message submitted mid-turn is parked for the next turn, but :q (and the
    other quit spellings) must exit anyway — on_unmount then interrupts the
    in-flight turn.
    """
    _textual()
    b, _rec = backend
    b.detach()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            # Pretend a turn is in flight; the quit path must not queue :q.
            app._busy = "turn"
            prompt = app.query_one("#input")
            prompt.focus()
            prompt.text = ":q"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert not app.is_running, ":q was queued instead of quitting"
            assert app._queued_items == [], "quit must never enter the queue"

    asyncio.run(run())


def test_dirty_blocks_keep_painting_their_last_frame() -> None:
    """A block mutated between flushes must not blank out.

    render_line used to return a blank strip for any dirty block, so the
    streamed answer flashed text/blank every time anything repainted between
    the 0.12s flush ticks. Dirty blocks now paint their last rendered lines.
    """
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause(0.2)
            feed = app.query_one("#feed")
            block = feed.open_markdown()
            block.append("hello world")
            feed.changed(block)
            # render_line takes viewport rows, so subtract the scroll offset.
            y = feed._starts[block.idx] - int(feed.scroll_offset.y)

            def plain(strip) -> str:
                return "".join(seg.text for seg in strip if not seg.control)

            before = plain(feed.render_line(y))
            assert "hello world" in before

            block.append(" more")  # dirty until the next flush
            after = plain(feed.render_line(y))
            assert after == before, "a dirty block blanked instead of holding its frame"

    asyncio.run(run())


def test_user_turns_have_padding_rows_and_a_rail() -> None:
    """User messages keep the old `padding: 1 0 1 2` block shape.

    The virtualization port rendered them as a single flush line; the rail now
    runs through a blank row above and below the text.
    """
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause(0.2)
            feed = app.query_one("#feed")
            feed.add_user("commit and push")
            block = next(b for b in feed._blocks if b.kind == "user")
            assert block.height == 3, "user turn needs a padding row above and below"
            rows = [
                "".join(seg.text for seg in strip if not seg.control)
                for strip in block.lines
            ]
            assert rows[1].strip() == "▎  commit and push"
            assert rows[0].strip() == "▎" and rows[2].strip() == "▎"

            # A long message wraps with the rail and gutter on every line.
            feed.add_user("alpha " * 40)
            wrapped = next(b for b in reversed(feed._blocks) if b.kind == "user")
            assert wrapped.height >= 4, "wrapped message needs >1 body row"
            for strip in wrapped.lines[1:-1]:
                plain = "".join(seg.text for seg in strip)
                assert plain.startswith("▎  "), plain
                first = next(seg for seg in strip if not seg.control)
                assert first.style is not None
                assert first.style.color is not None, "rail lost on a wrapped line"

    asyncio.run(run())


def test_sidebar_hides_background_row_when_nothing_runs(backend) -> None:
    """"background: idle" read as "the session is idle" while mid-conversation.

    The row is about background work; with none, it hides instead of showing a
    misleading idle state.
    """
    _textual()
    b, _rec = backend
    b.detach()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.3)
            app._update_context()
            label = app.query_one("#ctx-bg-label")
            value = app.query_one("#ctx-bg")
            assert label.display is False, "empty background row should hide"
            assert value.display is False

            app._snapshot = lambda: {"active_background_processes": 2, "duration": "6m"}
            app._update_context()
            assert label.display is True
            assert "2 processes" in str(value.content)

    asyncio.run(run())


def test_modal_screens_return_their_answers() -> None:
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            got: dict = {}
            app.push_screen(_tui.ChoiceModal("Pick", [("once", "Allow once", ""), ("deny", "Deny", "")]), lambda r: got.__setitem__("choice", r))
            await pilot.pause(0.2)
            await pilot.press("2")
            await pilot.pause(0.2)
            assert got["choice"] == "deny"

            app.push_screen(_tui.ChoiceModal("Esc", [("a", "A", "")]), lambda r: got.__setitem__("esc", r))
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert got["esc"] is None

            app.push_screen(_tui.TextModal("Secret", "value", password=True), lambda r: got.__setitem__("text", r))
            await pilot.pause(0.2)
            await pilot.press("a", "b", "enter")
            await pilot.pause(0.2)
            assert got["text"] == "ab"

            app.push_screen(_tui.MultiChoiceModal("Multi", [("0", "A"), ("1", "B")]), lambda r: got.__setitem__("multi", r))
            await pilot.pause(0.2)
            await pilot.press("space", "down", "space", "enter")
            await pilot.pause(0.2)
            assert got["multi"] == ["0", "1"]

            rows = [(str(i), f"model-{i}", "") for i in range(30)]
            app.push_screen(_tui.ChoiceModal("Filter", rows, filterable=True), lambda r: got.__setitem__("filtered", r))
            await pilot.pause(0.2)
            await pilot.press("2", "5", "enter")
            await pilot.pause(0.2)
            assert got["filtered"] == "25"
            assert app.focused is app.query_one("#input"), "prompt regains focus after a modal"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Backend (real cli.SonOfAntonCLI under the temp home)
# ---------------------------------------------------------------------------

@pytest.fixture
def backend(monkeypatch):
    """A started DaemonBackend on a real soa-d in a throwaway home (a model
    endpoint nothing listens on: these tests never run a turn to completion)."""
    import shutil
    import signal
    import tempfile

    from son_of_anton_tui import daemon as _daemon
    from son_of_anton_tui.backend import DaemonBackend

    if not (os.environ.get("SOA_D_BIN") or shutil.which("soa-d")):
        pytest.fail("soa-d is not on PATH: the dev shell provides it")
    home = tempfile.mkdtemp(prefix="soad", dir="/tmp" if os.path.isdir("/tmp") else None)
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write(
            '[model]\ndefault = "test-model"\nprovider = "custom"\n\n'
            '[custom_providers.custom]\nbase_url = "http://127.0.0.1:9/v1"\nkey_env = "SOA_D_TEST_KEY"\n\n'
            '[custom_providers.custom.models."test-model"]\n'
        )
    with open(os.path.join(home, ".env"), "w", encoding="utf-8") as fh:
        fh.write("SOA_D_TEST_KEY=s3cret\n")
    os.chmod(os.path.join(home, ".env"), 0o600)
    monkeypatch.setenv("SON_OF_ANTON_HOME", home)
    b = DaemonBackend(home=home, cwd=os.getcwd())
    assert b.start(), "DaemonBackend.start() failed"
    try:
        yield b, None
    finally:
        b.finish()
        try:
            c = _daemon.connect(home, autostart=False, timeout=2)
            pid = c.hello["pid"]
            c.close()
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and os.path.exists(_daemon.socket_path(home)):
                time.sleep(0.05)
        except Exception:
            pass
        shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------------------
# App + backend, end to end (chat() stubbed to drive the real seams)
# ---------------------------------------------------------------------------

def test_provider_label_prefers_the_display_name_over_the_slug() -> None:
    _textual()
    # build_models_payload gives name="custom", slug="custom:custom"; the slug is
    # an addressing detail and must not be what the user reads.
    assert _tui._provider_label({"slug": "custom:custom", "name": "custom"}) == "custom"
    assert _tui._provider_label({"slug": "openai", "name": "custom", "label": "OpenAI"}) == "OpenAI"
    assert _tui._provider_label({"slug": "openai"}) == "openai"
    assert _tui._provider_label({}) == "?"


# ---------------------------------------------------------------------------
# Layout: the frame mirrors opencode's session route
# ---------------------------------------------------------------------------

def test_transcript_columns_match_opencode() -> None:
    """Text sits at column 3; a tool row's icon hangs in the two columns left of it.

    opencode indents every text part by 3 (routes/session/index.tsx TextPart) and
    gives inline tools a 2-cell icon column, so icons align just outside the text.
    """
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause(0.2)
            post = lambda kind, **p: app.post_message(_tui.TuiEvent(kind, p))  # noqa: E731
            post("assistant_start")
            post("assistant_delta", text="hello")
            post("assistant_end")
            post("tool_start", name="terminal", label="Running ls")
            post("tool_done", name="terminal", label="Running ls", duration=0.4)
            post("ansi", text="a note")
            await pilot.pause(0.6)

            feed = app.query_one("#feed")
            md = next(b for b in feed._blocks if b.kind == "markdown")
            note = next(b for b in feed._blocks if b.kind == "note")
            tool = next(b for b in feed._blocks if b.kind == "tool")
            assert "".join(s.text for s in md.lines[0]).startswith("   ")  # column 3 indent
            assert "".join(s.text for s in note.lines[0]).startswith("   ")
            rendered = "".join(s.text for s in tool.lines[0])
            assert rendered.startswith("$ "), rendered
            assert "Running ls" in rendered

            app._add_user_turn("hi")
            await pilot.pause(0.1)
            user = next(b for b in feed._blocks if b.kind == "user")
            # padding row, then rail (1 cell) + padding (2 cells) puts the
            # user text in column 3 of the row below it
            assert "".join(s.text for s in user.lines[1])[3:5] == "hi"

    asyncio.run(run())


def test_sidebar_overlays_on_narrow_terminals() -> None:
    """opencode keeps the panel reachable when narrow by floating it over content."""
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.2)
            panel = app.query_one("#context")
            assert panel.display is False, "auto-hidden below the width threshold"
            app.action_toggle_sidebar()
            await pilot.pause(0.2)
            assert panel.display is True
            assert panel.has_class("overlay"), "narrow sidebar should float over the transcript"
            # floating means it takes no width from the transcript column
            assert app._feed_width() == 100

    async def wide() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause(0.2)
            panel = app.query_one("#context")
            assert panel.display is True
            assert not panel.has_class("overlay"), "a wide sidebar sits beside the transcript"
            assert app._feed_width() == 150 - 42

    asyncio.run(run())
    asyncio.run(wide())


def test_status_row_and_prompt_meta(backend) -> None:
    """The prompt block carries identity; the row below carries place and action.

    Mirrors opencode's Prompt: agent/model inside the block, working directory on
    the left of the status row when idle, and the interrupt hint while busy.
    """
    _textual()
    b, _rec = backend
    b.detach()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause(0.3)
            meta = str(app.query_one("#prompt-meta-left").render())
            assert "default" in meta, meta    # permission mode, always shown
            assert "test-model" in meta, meta    # model, as opencode shows it here

            left = str(app.query_one("#status-left").render())
            right = str(app.query_one("#status-right").render())
            assert _tui._short_path(os.getcwd()) in left or left.startswith("~")
            assert "commands" in right

            app._busy = "turn"
            app._update_status()
            assert "interrupt" in str(app.query_one("#status-right").render())
            app._busy = None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Terminal-derived surfaces (opencode's "system" theme, ported)
# ---------------------------------------------------------------------------

def test_osc_replies_parse_in_every_shape_terminals_use() -> None:
    from son_of_anton_tui.palette import RGB, parse_osc_color

    # 16 bits per channel is what most terminals answer with.
    assert parse_osc_color("\x1b]11;rgb:1e1e/1e1e/2e2e\x1b\\") == RGB(30, 30, 46)
    # 8-bit and hex forms both appear in the wild.
    assert parse_osc_color("\x1b]11;rgb:1e/1e/2e\x07") == RGB(30, 30, 46)
    assert parse_osc_color("#1e1e2e") == RGB(30, 30, 46)
    # A 12-bit reply still scales onto 0-255 rather than overflowing.
    assert parse_osc_color("rgb:fff/fff/fff") == RGB(255, 255, 255)
    assert parse_osc_color("not a colour") is None
    assert parse_osc_color("") is None


def test_generated_ramp_keeps_the_terminal_hue_and_direction() -> None:
    from son_of_anton_tui.palette import RGB, gray_scale, luminance, polarity

    kitty = RGB(0x1E, 0x1E, 0x2E)
    assert polarity(kitty) == "dark"
    ramp = gray_scale(kitty, is_dark=True)
    # A dark terminal's surfaces climb away from the background...
    values = [luminance(ramp[i]) for i in range(1, 13)]
    assert values == sorted(values)
    assert luminance(ramp[2]) > luminance(kitty)
    # ...and keep the background's own tint rather than washing out to gray.
    assert ramp[2].b > ramp[2].r == ramp[2].g

    paper = RGB(0xF5, 0xF0, 0xE8)
    assert polarity(paper) == "light"
    light = gray_scale(paper, is_dark=False)
    assert luminance(light[2]) < luminance(paper), "light terminals darken instead"


def test_palette_never_paints_the_base_background() -> None:
    """opencode leaves `background` transparent so terminal transparency survives."""
    from son_of_anton_tui.palette import RGB, TerminalColors, build_palette

    palette = build_palette(TerminalColors(RGB(0x1E, 0x1E, 0x2E), RGB(0xCD, 0xD6, 0xF4)))
    assert "background" not in palette
    assert palette["panel"].startswith("#") and palette["surface"].startswith("#")
    assert palette["panel"] != palette["surface"], "panel and element must be distinguishable"
    # Text keeps the ansi token: it already follows the terminal, and blends that
    # read $foreground (markdown table keylines) need the token to resolve.
    assert "text" not in palette and "foreground" not in palette


def test_query_terminal_colors_round_trips_over_a_pty() -> None:
    """Exercise the real termios/select path against a terminal that answers."""
    import io
    import pty
    import threading

    from son_of_anton_tui.palette import RGB, query_terminal_colors

    master, slave = pty.openpty()
    replies = {b"11": b"\x1b]11;rgb:1e1e/1e1e/2e2e\x1b\\", b"10": b"\x1b]10;rgb:cdcd/d6d6/f4f4\x1b\\"}

    def terminal() -> None:
        pending = b""
        for _ in range(2):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    pending += os.read(master, 64)
                except OSError:
                    return
                if b"?" in pending:
                    break
            for code, reply in replies.items():
                if b"]" + code + b";?" in pending:
                    os.write(master, reply)
                    pending = b""
                    break

    thread = threading.Thread(target=terminal, daemon=True)
    thread.start()

    stdin = io.TextIOWrapper(io.FileIO(slave, "r+"), write_through=True)
    stdout = io.TextIOWrapper(io.FileIO(slave, "r+", closefd=False), write_through=True)
    saved = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = stdin, stdout
    try:
        colors = query_terminal_colors(timeout=3.0)
    finally:
        sys.stdin, sys.stdout = saved
        thread.join(timeout=1)
        os.close(master)

    assert colors is not None, "no reply parsed from the pty"
    assert colors.background == RGB(30, 30, 46)
    assert colors.foreground == RGB(205, 214, 244)


def test_query_terminal_colors_gives_up_without_a_tty() -> None:
    from son_of_anton_tui.palette import query_terminal_colors

    # pytest's captured stdio is not a terminal: no query, no hang, no guess.
    assert query_terminal_colors(timeout=0.01) is None


def test_app_paints_generated_surfaces_only_when_the_terminal_answered() -> None:
    _textual()
    from son_of_anton_tui.palette import RGB, TerminalColors

    async def with_colors() -> None:
        app = _tui.SonOfAntonTUIApp(terminal_colors=TerminalColors(RGB(0x1E, 0x1E, 0x2E), None))
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause(0.2)
            variables = app.get_css_variables()
            assert variables["panel"].startswith("#")
            assert variables["surface"].startswith("#")
            # the base background still belongs to the terminal
            assert "#" not in variables["background"]
            assert app.theme == "ansi-dark"
            app._add_user_turn("hi")
            await pilot.pause(0.1)
            user = next(b for b in app.query_one("#feed")._blocks if b.kind == "user")
            backgrounds = set()
            for strip in user.lines:
                for seg in strip:
                    if seg.style is not None and seg.style.bgcolor:
                        r, g, b = seg.style.bgcolor.get_truecolor()
                        backgrounds.add(f"#{r:02x}{g:02x}{b:02x}")
            assert any(bg.startswith("#2c2c43") for bg in backgrounds), (
                f"user rows sit on the generated panel: {backgrounds}"
            )

    async def without_colors() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause(0.2)
            variables = app.get_css_variables()
            # No answer: every surface stays transparent, rails do the work.
            assert "#" not in variables["panel"]
            assert "#" not in variables["surface"]

    asyncio.run(with_colors())
    asyncio.run(without_colors())


def test_generated_surfaces_stay_readable_under_default_text() -> None:
    """Panels must stay on the terminal background's side of the light/dark line.

    Text renders as ``ansi_default``, i.e. whatever foreground the terminal
    pairs with that background. A panel that crossed over — a near-white panel
    under a dark terminal's light text — would be unreadable, so the ramp has to
    stay close to the background it came from.
    """
    from son_of_anton_tui.palette import (
        RGB,
        TerminalColors,
        build_palette,
        luminance,
        polarity,
    )

    for background in (
        RGB(0x1E, 0x1E, 0x2E),  # kitty / catppuccin
        RGB(0x00, 0x00, 0x00),  # pure black
        RGB(0x00, 0x2B, 0x36),  # solarized dark
        RGB(0xFD, 0xF6, 0xE3),  # solarized light
        RGB(0xFF, 0xFF, 0xFF),  # pure white
    ):
        palette = build_palette(TerminalColors(background, None))
        base = luminance(background)
        for key in ("panel", "surface"):
            value = palette[key]
            shade = RGB(int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
            assert polarity(shade) == polarity(background), (
                f"{key} {value} crossed the light/dark line for background {background.hex}"
            )
            # Visibly distinct from the background, but not a different world.
            assert 3 <= abs(luminance(shade) - base) <= 60, (
                f"{key} {value} is {abs(luminance(shade) - base):.0f} from {background.hex}"
            )


def test_app_theme_polarity_follows_the_queried_background() -> None:
    """A light terminal must select the light ansi theme, not the COLORFGBG guess."""
    _textual()
    from son_of_anton_tui.palette import RGB, TerminalColors

    async def check(background: "RGB", expected: str) -> None:
        app = _tui.SonOfAntonTUIApp(terminal_colors=TerminalColors(background, None))
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.2)
            assert app.theme == expected, f"{background.hex} -> {app.theme}"

    asyncio.run(check(RGB(0x1E, 0x1E, 0x2E), "ansi-dark"))
    asyncio.run(check(RGB(0xFD, 0xF6, 0xE3), "ansi-light"))


def test_wordmark_never_outgrows_its_column_across_resizes() -> None:
    """Block letters must fit whatever width they are given, live.

    The regression: the variant was chosen from an app-level resize handler,
    which runs before the new layout exists, so a 109-column form could be left
    in a 108-column feed and wrap into rubble. Crossing the wide/stacked
    boundary and toggling the scrollbar are the cases that broke.
    """
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause(0.3)
            # overflow the transcript so the scrollbar claims its column
            for i in range(40):
                app.post_message(_tui.TuiEvent("ansi", {"text": f"filler {i}"}))
            await pilot.pause(0.4)
            feed = app.query_one("#feed")
            wordmark = feed._wordmark

            def widest() -> int:
                return max(
                    (len("".join(s.text for s in line)) for line in wordmark.lines),
                    default=0,
                )

            for width in (160, 113, 114, 121, 119, 100, 90, 200, 70, 160):
                await pilot.resize_terminal(width, 30)
                await pilot.pause(0.3)
                assert widest() <= feed._width, (
                    f"wordmark is {widest()} columns in a {feed._width}-column feed at term width {width}"
                )
                # The feed must track its real content width on its own resize —
                # the app-level handler sees pre-layout geometry and used to
                # leave the header one frame stale on every terminal resize.
                assert feed._width == app._feed_inner_width(), (
                    f"feed width {feed._width} drifted from the real {app._feed_inner_width()} at term width {width}"
                )
                # Every row shares one left pad derived from the widest row:
                # the art's rows taper, and per-line padding would shift the
                # narrower rows (the letter bottoms) right of the ones above.
                art = _tui._wordmark_for_width(feed._width)
                art_lines = art.splitlines()
                pad = max(0, (feed._width - max(len(line) for line in art_lines)) // 2)
                for i, (art_line, strip) in enumerate(zip(art_lines, wordmark.lines)):
                    rendered = "".join(s.text for s in strip).rstrip()
                    assert rendered == (" " * pad + art_line).rstrip(), (
                        f"wordmark row {i} misaligned: {rendered[:30]!r}"
                    )

    asyncio.run(run())


def test_transcript_rows_fit_the_feed_across_resizes(backend) -> None:
    """No transcript row is wider than the feed, text keeps clear of the
    scrollbar, and CLI output never re-wraps.

    Indented blocks (paragraphs, headings, notes, reasoning) wrapped at the
    full feed width and were indented afterwards, so every full line ran past
    the viewport and lost its last few characters to the crop. Once they fit,
    text still ran flush against the scrollbar; only frames (rules, the
    user-turn panel fill) may reach the right edge. The backend console the
    classic CLI output wraps to was sized from the app's resize, which fires
    before layout, so it trailed one resize behind.
    """
    _textual()
    from rich.text import Text

    from son_of_anton_tui import feed as _feed

    b, _rec = backend
    b.detach()
    words = "the sanitizer catches the invalid access and the process segfaults " * 4

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.3)
            feed = app.query_one("#feed")
            feed.add_user(words)
            feed.add_markdown(
                f"# {words}\n\n{words}\n\n- {words}\n\n> {words}\n\n---\n\n"
                f"| a | b |\n|---|---|\n| {words} | x |\n\n```\n{words}\n```\n"
            )
            tool = feed.open_tool("terminal")
            tool.label, tool.dirty = words, True
            feed.add_note(Text(words))
            feed.add_rich(Text(words))
            feed.open_reasoning().append(words)
            feed.flush()

            for width in (120, 70, 160, 50, 100):
                await pilot.resize_terminal(width, 30)
                await pilot.pause(0.3)
                for block in feed._blocks:
                    for strip in block.lines:
                        assert strip.cell_length <= feed._width, (
                            f"{block.kind} row is {strip.cell_length} cells in a "
                            f"{feed._width}-cell feed at term width {width}"
                        )
                        text = "".join(seg.text for seg in strip if not seg.control).rstrip()
                        if block.kind == "wordmark" or set(text.strip()) == {"─"}:
                            continue
                        assert len(text) <= feed._width - _feed._TEXT_MARGIN, (
                            f"{block.kind} text runs to column {len(text)} of a "
                            f"{feed._width}-cell feed at term width {width}: {text[-20:]!r}"
                        )

    asyncio.run(run())


def test_feed_follows_output_whenever_the_view_returns_to_the_bottom() -> None:
    """Auto-follow resumes on every way back to the bottom, and only there.

    ScrollView's scroll_y watcher drops the anchor check Widget's has, so
    wheeling back down never resumed following; a scroll that could not move
    (PgUp on a transcript that fits, PgDn at the bottom) released it for good;
    an animated PgDn landed on the bottom as it was when pressed; and a fold
    that dropped the view onto the bottom left it parked there.
    """
    _textual()
    from textual import events

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause(0.3)
            feed = app.query_one("#feed")
            count = 0

            async def output(lines: int = 5, settle: float = 0.3) -> None:
                nonlocal count
                for _ in range(lines):
                    app.post_message(_tui.TuiEvent("ansi", {"text": f"line {count}"}))
                    count += 1
                await pilot.pause(settle)

            def wheel(down: bool) -> None:
                cls = events.MouseScrollDown if down else events.MouseScrollUp
                x, y = feed.region.x + 5, feed.region.y + 5
                feed.post_message(cls(feed, 5, 5, 0, 1 if down else -1, 0, False, False, False, screen_x=x, screen_y=y))

            def following() -> bool:
                return feed.scroll_y >= feed.max_scroll_y

            assert feed.max_scroll_y == 0, "the fixture must start with a transcript that fits"
            await pilot.press("pageup")
            await pilot.pause(0.2)
            await output(80)
            assert following(), "PgUp on a transcript that fits stopped the follow"

            wheel(down=False)
            await pilot.pause(0.2)
            parked = feed.scroll_y
            await output()
            assert feed.scroll_y == parked and not following(), "output yanked a scrolled-up view"

            for _ in range(10):
                wheel(down=True)
            await pilot.pause(0.3)
            await output()
            assert following(), "wheeling back to the bottom did not resume following"

            await pilot.press("pagedown")
            await pilot.pause(0.2)
            await output()
            assert following(), "PgDn at the bottom stopped the follow"

            await pilot.press("pageup")
            await pilot.pause(1.0)
            assert feed.scroll_y + feed.scrollable_content_region.height == feed.max_scroll_y
            feed.scroll_page_down()  # animated; output lands before it arrives
            await output(settle=1.0)
            assert following(), "PgDn to the bottom parked short of output that streamed in"

            reasoning = feed.open_reasoning()
            reasoning.append("thinking\n" * 30)
            feed.flush()
            await pilot.pause(0.3)
            wheel(down=False)
            await pilot.pause(0.2)
            assert not following()
            reasoning.finish()  # folds 30 lines to one, dropping the view onto the bottom
            feed.changed(reasoning)
            await pilot.pause(0.3)
            await output()
            assert following(), "a fold onto the bottom left the view parked"

    asyncio.run(run())


def test_blocks_are_separated_the_way_opencode_separates_them() -> None:
    """A row after a block gets a blank line; consecutive tool rows stay tight."""
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause(0.2)
            post = lambda kind, **p: app.post_message(_tui.TuiEvent(kind, p))  # noqa: E731
            post("tool_start", name="terminal", label="Running ls")
            post("tool_done", name="terminal", label="Running ls", duration=0.1)
            post("tool_start", name="read_file", label="Reading a.py")
            post("tool_done", name="read_file", label="Reading a.py", duration=0.1)
            await pilot.pause(0.5)
            feed = app.query_one("#feed")
            tools = [b for b in feed._blocks if b.kind == "tool"]
            assert len(tools) == 2
            assert tools[1].before == 0, "consecutive tool rows should stay tight"

            # A note straight after a user block must not butt against it —
            # this is the "yo" / "Initializing agent..." pair from a real turn.
            post("ansi", text="a line before the turn")
            await pilot.pause(0.2)
            app._add_user_turn("yo")
            post("ansi", text="Initializing agent...")
            await pilot.pause(0.4)
            note = [b for b in feed._blocks if b.kind == "note"][-1]
            assert "Initializing" in "".join(s.text for s in note.lines), (
                "the note merged into the block above the user message"
            )
            assert note.before == 1, "a note following a user message needs air"

    asyncio.run(run())


def test_the_guard_honours_the_session_override() -> None:
    """The override has to reach the approval guard, not just the status line."""
    from tools.approval import (
        _get_approval_mode,
        _is_lockdown_enabled,
        clear_session,
        reset_current_session_key,
        set_current_session_key,
        set_session_permission_mode,
    )

    session = "test-session-permissions"
    token = set_current_session_key(session)
    try:
        baseline = _get_approval_mode()
        set_session_permission_mode(session, "yolo")
        assert _get_approval_mode() == "off"
        assert _is_lockdown_enabled() is False
        set_session_permission_mode(session, "lockdown")
        assert _get_approval_mode() == "manual"
        assert _is_lockdown_enabled() is True
        set_session_permission_mode(session, None)
        assert _get_approval_mode() == baseline, "clearing must fall back to config"
        assert _is_lockdown_enabled() is False
    finally:
        set_session_permission_mode(session, None)
        clear_session(session)
        reset_current_session_key(token)


def test_another_session_is_unaffected_by_an_override() -> None:
    """One session's yolo must never bypass approvals for a different session."""
    from tools.approval import (
        _get_approval_mode,
        clear_session,
        reset_current_session_key,
        set_current_session_key,
        set_session_permission_mode,
    )

    set_session_permission_mode("session-a", "yolo")
    token = set_current_session_key("session-b")
    try:
        assert _get_approval_mode() != "off", "session-a's yolo leaked into session-b"
    finally:
        reset_current_session_key(token)
        set_session_permission_mode("session-a", None)
        clear_session("session-a")


def test_session_yolo_override_cannot_bypass_the_unconditional_floors() -> None:
    """A session override must not widen what yolo already permits.

    It resolves to the same ``mode: off`` the config setting produces, so the
    hardline block and the sudo-stdin guard — which run before any mode check —
    still fire.
    """
    from tools.approval import (
        check_all_command_guards,
        clear_session,
        reset_current_session_key,
        set_current_session_key,
        set_session_permission_mode,
    )

    session = "test-session-floors"
    token = set_current_session_key(session)
    set_session_permission_mode(session, "yolo")
    try:
        for command in ("rm -rf /", "echo hunter2 | sudo -S whoami"):
            result = check_all_command_guards(command, "local")
            assert result.get("blocked") or result.get("resolved") is None, (
                f"{command!r} was not stopped under a session yolo override: {result}"
            )
            assert result.get("choice") != "allow"
    finally:
        set_session_permission_mode(session, None)
        clear_session(session)
        reset_current_session_key(token)


# ---------------------------------------------------------------------------
# External editor: the terminal has to be handed over, not shared
# ---------------------------------------------------------------------------

def _fake_editor() -> str:
    """An $EDITOR that appends a line, so we can prove it ran and was read back."""
    return f"{sys.executable} -c \"import sys;open(sys.argv[1],'a').write('EDITED\\n')\""


def test_composing_in_an_editor_suspends_the_app(backend, monkeypatch) -> None:
    """A child that draws its own screen must not share the tty with the app.

    Both would write to it and both would read stdin, which is what leaves
    stray escape characters in the transcript.
    """
    _textual()
    b, _rec = backend
    b.detach()
    monkeypatch.setenv("EDITOR", _fake_editor())
    suspends: list = []

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.3)
            real = app.run_suspended
            app.run_suspended = lambda fn: (suspends.append(True), real(fn))[1]

            app.query_one("#input").focus()
            app._prompt.text = "draft "
            await pilot.press("ctrl+g")
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and app._busy is not None:
                await pilot.pause(0.1)
            await pilot.pause(0.4)

            assert suspends, "the editor ran without suspending the app"
            turns = ["".join(s.text for s in b.lines) for b in app.query_one("#feed")._blocks if b.kind == "user"]
            assert any("EDITED" in t for t in turns), f"composed text was not sent: {turns}"
            assert any("draft" in t for t in turns), "the draft was not carried into the editor"

    asyncio.run(run())


def test_editor_handoff_survives_a_driver_that_cannot_suspend(backend, monkeypatch) -> None:
    """Headless/unsupported drivers still compose — they just don't suspend."""
    _textual()
    from textual.app import SuspendNotSupported

    b, _rec = backend
    b.detach()
    monkeypatch.setenv("EDITOR", _fake_editor())

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.3)

            def boom():
                raise SuspendNotSupported("no")

            monkeypatch.setattr(type(app), "suspend", lambda self: boom())
            composed = app.run_suspended(lambda: b._compose_in_editor("seed "))
            assert "EDITED" in composed and "seed" in composed

    asyncio.run(run())


def test_an_empty_editor_gives_the_draft_back(backend, monkeypatch) -> None:
    _textual()
    b, _rec = backend
    b.detach()
    # An editor that wipes the file: nothing composed, so nothing should be sent.
    monkeypatch.setenv("EDITOR", f"{sys.executable} -c \"import sys;open(sys.argv[1],'w').close()\"")

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.3)
            app.query_one("#input").focus()
            app._prompt.text = "keep me"
            await pilot.press("ctrl+g")
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and app._busy is not None:
                await pilot.pause(0.1)
            await pilot.pause(0.4)
            assert app._prompt.text == "keep me", "an empty save must not eat the draft"
            assert not [b for b in app.query_one("#feed")._blocks if b.kind == "user"]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Lifecycle the old input loop used to drive
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# /commit
# ---------------------------------------------------------------------------

def test_commit_command_is_registered() -> None:
    from son_of_anton_cli.commands import resolve_command

    cmd = resolve_command("commit")
    assert cmd is not None and cmd.name == "commit"
    assert cmd.cli_only, "committing is an interactive action, not a chat-platform one"


def test_compaction_renders_as_a_divider_then_what_it_remembers() -> None:
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.2)
            app.post_message(
                _tui.TuiEvent(
                    "compaction",
                    {"text": "## Objective\n- keep going", "stats": {"before_messages": 40, "after_messages": 6}},
                )
            )
            await pilot.pause(0.6)
            feed = app.query_one("#feed")
            kinds = [b.kind for b in feed._blocks]
            assert kinds.count("markdown") == 1, "the summary is a markdown block, not chrome"
            notes = "".join(s.text for b in feed._blocks if b.kind == "note" for s in b.lines)
            assert "6 of 40 messages kept" in notes
            assert kinds.index("markdown") < kinds.index("note", kinds.index("markdown")), (
                "divider, summary, then the stats line"
            )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Transcript scaling: virtual feed, deferred merges, buffered streaming
# ---------------------------------------------------------------------------

def test_feed_holds_every_row_of_a_marathon_session() -> None:
    """The virtual feed keeps all rows — no cap, no trim, unbounded scrollback."""
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            for i in range(3000):
                app._add_user_turn(f"turn {i}")
            await pilot.pause(0.3)
            feed = app.query_one("#feed")
            turns = [b for b in feed._blocks if b.kind == "user"]
            assert len(turns) == 3000, "nothing is trimmed; scrollback is unbounded"
            assert "turn 0" in "".join(s.text for s in turns[0].lines)
            assert "turn 2999" in "".join(s.text for s in turns[-1].lines)
            assert feed._total == sum(b.before + b.height for b in feed._blocks)

    asyncio.run(run())


def test_note_merge_defers_rendering_until_flush() -> None:
    """A flood of lines costs one re-render per frame, not one per line."""
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app._note("first")
            app._note("second")
            note = [b for b in app.query_one("#feed")._blocks if b.kind == "note"][-1]
            assert len(note.texts) == 2, "both lines merge into one block"
            assert note.dirty, "pending lines render only on flush"
            app._tick_spinner()
            assert not note.dirty
            joined = "".join(s.text for s in note.lines)
            assert "first" in joined and "second" in joined
            app._tick_spinner()  # idempotent once flushed
            assert joined.count("second") == 1

    asyncio.run(run())


def test_note_merge_caps_the_block_and_flushes_on_replacement() -> None:
    """Blocks stop growing at the cap; the previous block is flushed on switch."""
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            from son_of_anton_tui import feed as _feed

            for i in range(_feed._NOTE_MERGE_MAX_LINES + 5):
                app._note(f"line {i}")
            notes = [b for b in app.query_one("#feed")._blocks if b.kind == "note"]
            assert len(notes) == 2, "a fresh block starts after the cap"
            assert len(notes[-1].texts) == 5
            app._tick_spinner()
            assert "line 0" not in "".join(s.text for s in notes[-1].lines)

    asyncio.run(run())


def test_assistant_deltas_accumulate_and_render_on_the_flush_tick() -> None:
    """Deltas buffer into the markdown block; the frame tick renders them."""
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause(0.2)
            post = lambda kind, **p: app.post_message(_tui.TuiEvent(kind, p))  # noqa: E731
            post("assistant_start")
            await pilot.pause(0.05)
            post("assistant_delta", text="alpha")
            await pilot.pause(0.02)  # process the delta; the frame tick has not flushed
            block = app._md_block
            assert block is not None and block.source == "alpha", "deltas buffer, renders are batched"
            assert "alpha" in app._transcript, "the transcript records every delta as it lands"

            post("assistant_delta", text="beta")
            await pilot.pause(0.4)  # the 0.12s spinner tick flushes
            assert block.source == "alphabeta"
            assert not block.dirty
            assert "alphabeta" in "".join(s.text for s in block.lines)

            post("assistant_end")
            await pilot.pause(0.2)
            assert app._md_block is None

    asyncio.run(run())


def test_the_feed_is_a_single_widget_with_no_children() -> None:
    """The Line API contract: content lives in blocks, never in child widgets."""
    _textual()

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            for i in range(50):
                app._note(f"line {i}")
            await pilot.pause(0.3)
            feed = app.query_one("#feed")
            assert len(feed.children) == 0, "the virtual feed must not accumulate children"
            assert feed._total >= 50

    asyncio.run(run())


def test_status_snapshot_is_reused_within_a_refresh_cycle(backend) -> None:
    """The chrome paths share one snapshot build per short window."""
    _textual()
    b, _rec = backend
    b.detach()
    calls: list = []
    original = b.status_snapshot

    def counting():
        calls.append(1)
        return original()

    b.status_snapshot = counting

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause(0.3)
            calls.clear()
            app._snap_cache = None
            app._update_status()  # status row + meta row both read the snapshot
            app._update_context()  # and so does the sidebar
            assert len(calls) == 1, f"three reads, one build: {len(calls)}"
            app._update_status()
            assert len(calls) == 1, "a read inside the cache window rebuilt the snapshot"

    asyncio.run(run())


def test_prompt_history_loads_only_the_tail_of_huge_files(tmp_path) -> None:
    """The history file grows forever; the load must not read all of it."""
    _textual()
    path = tmp_path / ".son_of_anton_history"
    path.write_bytes(
        b"# 2020-01-01 00:00:00.000000\n+old entry\n"
        + b"junk\n" * 200_000  # ~1 MB, well past the 512 KB cap
        + b"# 2026-01-01 00:00:00.000000\n+recent entry\n"
    )

    hist = _tui.PromptHistory(path)
    assert hist._entries == ["recent entry"], "the tail survives; the pre-cap head does not"
