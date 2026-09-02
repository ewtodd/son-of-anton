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

The backend half (``son_of_anton_tui.backend.TextualBackend``) is tested
against the real ``cli.SonOfAntonCLI`` under the temp ``SON_OF_ANTON_HOME``
conftest provides: the streaming seams must turn the classic CLI's callbacks
into typed events, and the queue-based prompts must unblock when answered from
another thread — the contract the app relies on.
"""

from __future__ import annotations

import asyncio
import os
import queue
import sys
import threading
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
            assert app.query_one("#wordmark").has_class("wordmark")
            assert app.theme.startswith("ansi-"), f"not an ansi theme: {app.theme}"
            vars_ = app.get_css_variables()
            for key in ("background", "panel", "surface", "text", "foreground"):
                assert "#" not in vars_.get(key, ""), f"{key} hardcodes a hex: {vars_.get(key)!r}"
            assert _tui.PlainMarkdown.BLOCKS["fence"] is _tui._PlainFence
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

            feed = app.query_one("#feed").children
            kinds = [type(w).__name__ for w in feed]
            assert kinds.count("ReasoningBlock") == 1
            assert kinds.count("ToolLine") == 1, "tool_gen + tool_start + tool_done must share one row"
            assert kinds.count("PlainMarkdown") == 1
            reasoning = next(w for w in feed if isinstance(w, _tui.ReasoningBlock))
            assert reasoning.collapsed, "reasoning folds once the answer starts"
            assert "2 lines" in reasoning.title
            tool = next(w for w in feed if isinstance(w, _tui.ToolLine))
            assert tool.done
            notes = [w for w in feed if isinstance(w, _tui.NoteLine) and w.text is not None]
            assert any("note one\nnote two" in w.text.plain for w in notes), "consecutive lines merge into one block"
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
            assert "/model" in app._completion_cmds
            first = app._completion_cmds[0]
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert prompt.text == first + " "
            assert app.query_one("#completer").display is False
            prompt.text = ""
            await pilot.press("h", "i")
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert [w for w in app.query_one("#feed").children if isinstance(w, _tui.UserTurn)]
            prompt.text = ":q"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert not app.is_running, ":q did not quit the app"

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
            block = next(w for w in app.query_one("#feed").children if isinstance(w, _tui.ReasoningBlock))
            assert hostile in block._buffer

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

            note = next(w for w in app.query_one("#feed").children if isinstance(w, _tui.NoteLine))
            await pilot.mouse_down(note, offset=(0, 0))
            await pilot.hover(note, offset=(12, 0))
            await pilot.mouse_up(note, offset=(12, 0))
            await pilot.pause(0.3)

            assert app.clipboard.strip(), "highlighting did not copy anything"
            assert app.clipboard.strip() in "a line worth selecting"
            assert app.focused is prompt, "selecting stole focus from the prompt"

            # A plain click anywhere in the transcript keeps the caret too.
            await pilot.click("#feed")
            await pilot.pause(0.2)
            assert app.focused is prompt, "clicking the feed stole focus from the prompt"

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

class _Recorder:
    def __init__(self) -> None:
        self.events: list = []

    def emit(self, kind: str, **payload) -> None:
        self.events.append((kind, payload))

    def kinds(self) -> list:
        return [k for k, _ in self.events]


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    # No startup prewarms: they fetch and import in background threads that write
    # under the temp SON_OF_ANTON_HOME, and a late write races its cleanup.
    monkeypatch.setenv("SON_OF_ANTON_DEFER_AGENT_STARTUP", "1")
    from son_of_anton_tui.backend import TextualBackend

    b = TextualBackend(model="test-model", provider="openai", api_key="sk-test", base_url="https://example.invalid/v1")
    rec = _Recorder()
    b.attach(rec, feed_width=90)
    assert b.prepare_interactive_state()
    rec.events.clear()  # drop startup chrome; tests assert on what they trigger
    try:
        yield b, rec
    finally:
        b.detach()
        b._release_active_session()
        db = getattr(b, "_session_db", None)
        if db is not None:
            db.close()


def test_backend_streaming_seams_emit_typed_events(backend) -> None:
    b, rec = backend
    b._reset_stream_state()
    b._stream_delta("<think>secret thoughts\n</think>")
    b._stream_delta("Hello **world**")
    b._stream_delta(None)  # intermediate turn boundary
    b._on_tool_gen_start("terminal")
    b._on_tool_progress("tool.started", function_name="terminal", preview="ls", function_args={"command": "ls"})
    b._on_tool_progress("tool.completed", function_name="terminal", duration=0.4, result="ok")
    b._stream_delta("after tools")
    b._flush_stream()

    kinds = [k for k in rec.kinds() if k != "refresh"]
    assert kinds == [
        "reasoning_start", "reasoning_delta", "reasoning_end",
        "assistant_start", "assistant_delta", "assistant_end",
        "tool_gen", "tool_start", "tool_done",
        "assistant_start", "assistant_delta", "assistant_end",
    ]
    payloads = dict((k, p) for k, p in rec.events)
    assert payloads["reasoning_delta"]["text"] == "secret thoughts\n"
    assert payloads["tool_start"]["label"] == "ls"
    # the app draws its own icon column, so completion carries the phrase
    assert payloads["tool_done"]["label"] == "Running ls"
    assert payloads["tool_done"]["duration"] == 0.4
    # reasoning-tag content never reaches the assistant stream
    assert all("secret" not in p.get("text", "") for k, p in rec.events if k == "assistant_delta")


def test_backend_reroutes_every_output_funnel(backend) -> None:
    b, rec = backend
    import cli

    cli._cprint("\x1b[2mvia cprint\x1b[0m")
    cli.ChatConsole().print("[bold]markup[/bold]")
    cli.ChatConsole().print("[yellow]" + "─" * 40)  # the classic turn rule is dropped
    b.console.print("[red]console[/red]")
    from son_of_anton_cli import banner, callbacks

    banner.cprint("via banner")
    callbacks.cprint("via callbacks")

    ansi = [p["text"] for k, p in rec.events if k == "ansi"]
    assert any("via cprint" in t for t in ansi)
    assert any("console" in t for t in ansi)
    assert any("via banner" in t for t in ansi)
    assert any("via callbacks" in t for t in ansi)
    rich = [p["renderable"] for k, p in rec.events if k == "rich"]
    assert len(rich) == 1 and rich[0].plain == "markup"


def test_backend_prompts_block_until_answered_from_another_thread(backend) -> None:
    b, rec = backend

    def answer() -> None:
        def wait_for(attr):
            for _ in range(100):
                state = getattr(b, attr)
                if state:
                    return state
                time.sleep(0.02)
            raise AssertionError(f"{attr} never appeared")

        wait_for("_slash_confirm_state")["response_queue"].put("always")
        wait_for("_tui_text_prompt_state")["response_queue"].put("typed")
        wait_for("_tui_picker_state")["response_queue"].put(2)
        wait_for("_secret_state")["response_queue"].put("")

    threading.Thread(target=answer, daemon=True).start()
    assert b._prompt_text_input_modal(title="t", detail="d", choices=[("once", "Once", ""), ("always", "Always", "")]) == "always"
    assert b._slash_confirm_state is None
    assert b._prompt_text_input("name? ") == "typed"
    assert b._run_curses_picker("pick", ["a", "b", "c"]) == 2
    result = b._secret_capture_callback("MY_KEY", "paste the key")
    assert result["skipped"] is True and result["reason"] == "cancelled"
    assert "refresh" in rec.kinds()


def test_backend_cancel_pending_prompts_unblocks_everything(backend) -> None:
    b, rec = backend
    q: "queue.Queue" = queue.Queue()
    b._slash_confirm_state = {"response_queue": q, "choices": [], "title": "", "detail": ""}
    b._tui_picker_state = {"response_queue": q, "items": [], "title": ""}
    b.cancel_pending_prompts()
    assert q.get_nowait() == "cancel"
    assert q.get_nowait() is None
    assert b._slash_confirm_state is None and b._tui_picker_state is None


def test_backend_slash_commands_and_status(backend) -> None:
    b, rec = backend
    assert b.run_slash("/mode physics") is True
    assert b._agent_mode == "physics"
    assert b.run_slash("/exit") is False
    snap = b.status_snapshot()
    assert snap["model_short"] == "test-model"
    entries = b.palette_entries()
    assert any(cmd == "/model" for cmd, _cat, _desc in entries)


# ---------------------------------------------------------------------------
# App + backend, end to end (chat() stubbed to drive the real seams)
# ---------------------------------------------------------------------------

def test_app_runs_a_turn_and_answers_the_backend_modals(backend) -> None:
    _textual()
    b, _rec = backend
    b.detach()
    seen: dict = {}

    def fake_chat(message, images=None):
        b._reset_stream_state()
        b._stream_reasoning_delta("pondering\n")
        b._stream_delta("Sure — running a command.\n")
        b._stream_delta(None)
        b._on_tool_progress("tool.started", function_name="terminal", preview="rm -rf /tmp/x", function_args={"command": "rm -rf /tmp/x"})
        seen["approval"] = b._approval_callback("rm -rf /tmp/x", "Recursive delete")
        b._on_tool_progress("tool.completed", function_name="terminal", duration=0.2, result="done")
        seen["clarify"] = b._clarify_callback("Which flavour?", ["vanilla", "chocolate"])
        seen["sudo"] = b._sudo_password_callback()
        print("a bare print from the worker")
        b._stream_delta(f"All done: **{seen['clarify']}**\n")
        b._flush_stream()
        return "All done"

    b.chat = fake_chat

    async def wait_for(pilot, pred, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await pilot.pause(0.1)
            if pred():
                return True
        return False

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause(0.3)
            prompt = app.query_one("#input")
            prompt.focus()
            prompt.text = "hello"
            await pilot.press("enter")
            assert await wait_for(pilot, lambda: isinstance(app.screen, _tui.ChoiceModal)), "approval modal never opened"
            assert "run a command" in app.screen._title
            await pilot.press("2")  # allow for this session
            assert await wait_for(pilot, lambda: isinstance(app.screen, _tui.ChoiceModal) and "input" in app.screen._title), "clarify modal never opened"
            await pilot.press("down", "enter")  # chocolate
            assert await wait_for(pilot, lambda: isinstance(app.screen, _tui.TextModal)), "sudo modal never opened"
            await pilot.press("escape")
            assert await wait_for(pilot, lambda: app._busy is None), "turn never finished"
            await pilot.pause(0.3)

            assert seen == {"approval": "session", "clarify": "chocolate", "sudo": ""}
            feed = app.query_one("#feed").children
            kinds = [type(w).__name__ for w in feed]
            assert "UserTurn" in kinds and "ReasoningBlock" in kinds and "ToolLine" in kinds
            assert kinds.count("PlainMarkdown") == 2
            assert "All done: **chocolate**" in app._transcript
            notes = [w.text.plain for w in feed if isinstance(w, _tui.NoteLine) and w.text is not None]
            assert any("a bare print from the worker" in n for n in notes), "print() from the worker is captured"
            assert any("Approval: rm -rf /tmp/x" in n for n in notes), "the persisted prompt summary lands in the feed"
            assert app.focused is prompt

            prompt.text = "/verbose"
            await pilot.press("enter")
            assert await wait_for(pilot, lambda: app._busy is None)
            await pilot.pause(0.3)
            notes = [w.text.plain for w in feed if isinstance(w, _tui.NoteLine) and w.text is not None]
            assert any("Tool progress" in n for n in notes), "slash command output reaches the feed"

    asyncio.run(run())


def test_ctrl_c_interrupts_a_running_turn_and_quitting_unwinds_it(backend) -> None:
    """A turn in flight must be interruptible, and quitting must not wait on it.

    Textual runs threaded workers on asyncio's default executor and the loop joins
    those threads at shutdown, so an un-interrupted turn would stall the quit.
    """
    _textual()
    b, _rec = backend
    b.detach()
    released = threading.Event()
    interrupted = threading.Event()

    def fake_chat(message, images=None):
        b._stream_delta("working…")
        released.wait(10)
        b._flush_stream()
        return "stopped"

    def fake_interrupt() -> bool:
        interrupted.set()
        released.set()
        return True

    b.chat = fake_chat
    b.interrupt_turn = fake_interrupt

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            prompt = app.query_one("#input")
            prompt.focus()
            prompt.text = "long task"
            await pilot.press("enter")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and app._busy != "turn":
                await pilot.pause(0.05)
            assert app._busy == "turn", "turn never started"

            await pilot.press("ctrl+c")
            assert interrupted.wait(5), "ctrl+c did not reach the backend"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and app._busy is not None:
                await pilot.pause(0.05)
            assert app._busy is None, "turn did not unwind after the interrupt"

            # A second turn, quit while it runs: on_unmount interrupts it.
            interrupted.clear()
            released.clear()
            prompt.text = "another long task"
            await pilot.press("enter")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and app._busy != "turn":
                await pilot.pause(0.05)
            app.exit()
        assert interrupted.is_set(), "quitting mid-turn did not interrupt the agent"

    asyncio.run(run())


def test_typing_mid_turn_steers_the_running_agent(backend) -> None:
    """A message typed while a turn runs must reach the agent, not the void.

    ``display.busy_input_mode`` picks where it lands: "interrupt" (the default)
    hands it to chat()'s interrupt monitor so the agent turns on a dime, while
    "queue" parks it for the next turn. Steering was dead for a while for an
    unrelated reason — the interrupt fired, then rendering the interrupt notice
    hit an undeclared ``wcwidth`` (see tests/test_markdown_tables.py).
    """
    _textual()
    b, _rec = backend
    b.detach()
    released = threading.Event()

    def fake_chat(message, images=None):
        released.wait(10)
        return "done"

    b.chat = fake_chat

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            prompt = app.query_one("#input")
            prompt.focus()
            prompt.text = "long task"
            await pilot.press("enter")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and app._busy != "turn":
                await pilot.pause(0.05)
            assert app._busy == "turn", "turn never started"

            b.busy_input_mode = "interrupt"
            prompt.text = "actually, do it the other way"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert b._interrupt_queue.get_nowait() == "actually, do it the other way"
            assert prompt.text == "", "steering must clear the prompt"

            b.busy_input_mode = "queue"
            prompt.text = "afterwards, run the tests"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert b._pending_input.get_nowait() == "afterwards, run the tests"
            assert app._queued == 1

            released.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and app._busy is not None:
                await pilot.pause(0.05)
            assert app._busy is None, "turn did not unwind"

    asyncio.run(run())


def test_provider_label_prefers_the_display_name_over_the_slug() -> None:
    _textual()
    # build_models_payload gives name="custom", slug="custom:custom"; the slug is
    # an addressing detail and must not be what the user reads.
    assert _tui._provider_label({"slug": "custom:custom", "name": "custom"}) == "custom"
    assert _tui._provider_label({"slug": "openai", "name": "custom", "label": "OpenAI"}) == "OpenAI"
    assert _tui._provider_label({"slug": "openai"}) == "openai"
    assert _tui._provider_label({}) == "?"


def test_model_picker_advances_through_both_stages(backend, monkeypatch) -> None:
    """Picking a provider must load its models, and picking a model must switch.

    The picker is the one modal whose selection re-enters the backend to advance
    its own state machine, so the callback has to actually run that work rather
    than merely create it.
    """
    _textual()
    b, _rec = backend
    b.detach()

    switched: dict = {}

    class _Result:
        success = True
        new_model = "gpt-b"
        target_provider = "openai"

    def fake_switch_model(**kwargs):
        switched.update(kwargs)
        return _Result()

    import son_of_anton_cli.model_switch as _ms

    monkeypatch.setattr(_ms, "switch_model", fake_switch_model)
    applied: list = []
    b._confirm_and_apply_model_switch_result = lambda *a, **kw: applied.append(a)

    b._model_picker_state = {
        "stage": "provider",
        "providers": [
            {"slug": "openai", "label": "OpenAI", "models": ["gpt-a", "gpt-b"], "is_current": True},
            {"slug": "deepseek", "label": "DeepSeek", "models": ["ds-1"]},
        ],
        "selected": 0,
        "current_model": "gpt-a",
        "current_provider": "OpenAI",
        "user_provs": None,
        "custom_provs": None,
        "filter": "",
    }

    async def wait_for(pilot, pred, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await pilot.pause(0.1)
            if pred():
                return True
        return False

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(140, 40)) as pilot:
            assert await wait_for(pilot, lambda: isinstance(app.screen, _tui.ChoiceModal)), "provider modal never opened"
            assert "Switch model" in app.screen._title
            await pilot.press("1")  # OpenAI

            assert await wait_for(
                pilot, lambda: (b._model_picker_state or {}).get("stage") == "model"
            ), "picking a provider did not advance the picker to its model stage"
            assert await wait_for(
                pilot,
                lambda: isinstance(app.screen, _tui.ChoiceModal) and "Models" in app.screen._title,
            ), "model list never opened"
            assert app.screen._title.endswith("OpenAI"), f"model stage mislabels the provider: {app.screen._title}"
            assert b._model_picker_state["model_list"] == ["gpt-a", "gpt-b"]
            # the model in use is marked, so a switch shows what it is switching from
            assert [c[1] for c in app.screen._choices][:2] == ["gpt-a  ●", "gpt-b"]

            # The model list filters as you type (long lists), so it carries no
            # number gutter: narrow, then Enter takes the highlighted row.
            assert "type to filter" in str(app.screen.query_one(".dialog-hint").render())
            await pilot.press("g", "p", "t", "-", "b")
            await pilot.pause(0.2)
            await pilot.press("enter")
            assert await wait_for(pilot, lambda: bool(switched)), "picking a model never reached switch_model"
            assert switched["raw_input"] == "gpt-b"
            assert switched["explicit_provider"] == "openai"
            assert await wait_for(pilot, lambda: b._model_picker_state is None), "picker did not close"
            assert applied, "the switch result was never applied"
            assert await wait_for(pilot, lambda: app._busy is None)

    asyncio.run(run())


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
            md = feed.query_one(_tui.PlainMarkdown)
            note = [w for w in feed.children if isinstance(w, _tui.NoteLine)][-1]
            tool = feed.query_one(_tui.ToolLine)
            assert md.styles.padding.left == 3
            assert note.styles.padding.left == 3
            assert tool.styles.padding.left == 0
            # icon column, then the label at column 2
            rendered = str(tool.render())
            assert rendered.startswith("$ "), rendered
            assert "Running ls" in rendered

            user = _tui.UserTurn("hi")
            await feed.mount(user)
            await pilot.pause(0.1)
            # rail (1) + padding (2) puts user text in the same column as the rest
            assert user.styles.padding.left == 2
            assert user.styles.border_left[0] == "wide"

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
            assert "auto" in meta, meta          # agent mode
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
            user = _tui.UserTurn("hi")
            await app.query_one("#feed").mount(user)
            await pilot.pause(0.1)
            assert user.styles.background.hex.lower().startswith("#2c2c43")

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


def test_startup_output_is_replayed_once_the_app_mounts(monkeypatch) -> None:
    """Warnings printed before the frame exists must not vanish.

    Credential checks and the tirith advisory run during startup, before the app
    has mounted and bound its sink.
    """
    monkeypatch.setenv("SON_OF_ANTON_DEFER_AGENT_STARTUP", "1")
    from son_of_anton_tui.backend import TextualBackend

    b = TextualBackend(model="test-model", provider="openai", api_key="sk-test", base_url="https://example.invalid/v1")
    try:
        b.emit("ansi", text="a warning from startup")
        b.emit("refresh")  # transient: not worth replaying later
        rec = _Recorder()
        b.attach(rec, feed_width=80)
        assert [k for k, _ in rec.events] == ["ansi"]
        assert rec.events[0][1]["text"] == "a warning from startup"
        # the buffer is drained, not replayed forever
        rec.events.clear()
        b.detach()
        b.attach(rec, feed_width=80)
        assert rec.events == []
    finally:
        b.detach()


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
            wordmark = app.query_one("#wordmark", _tui.Wordmark)

            def widest() -> int:
                return max((len(line) for line in str(wordmark.render()).splitlines()), default=0)

            for width in (160, 113, 114, 121, 119, 100, 90, 200, 70, 160):
                await pilot.resize_terminal(width, 30)
                await pilot.pause(0.3)
                assert widest() <= wordmark.size.width, (
                    f"wordmark is {widest()} columns in a {wordmark.size.width}-column feed at term width {width}"
                )

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
            tools = app.query(_tui.ToolLine).nodes
            assert len(tools) == 2
            assert tools[1].styles.margin.top == 0, "consecutive tool rows should stay tight"

            # A note straight after a user block must not butt against it —
            # this is the "yo" / "Initializing agent..." pair from a real turn.
            feed = app.query_one("#feed")
            post("ansi", text="a line before the turn")
            await pilot.pause(0.2)
            app._add_user_turn("yo")
            post("ansi", text="Initializing agent...")
            await pilot.pause(0.4)
            note = [w for w in feed.children if isinstance(w, _tui.NoteLine)][-1]
            assert "Initializing" in note.text.plain, "the note merged into the block above the user message"
            assert note.styles.margin.top == 1, "a note following a user message needs air"

    asyncio.run(run())


def test_shift_tab_cycles_permission_modes(backend) -> None:
    """shift+tab walks default → ask → lockdown → yolo → default."""
    from son_of_anton_tui.backend import PERMISSION_MODES

    b, _rec = backend
    assert b.permission_mode() == "default"
    seen = [b.cycle_permission_mode() for _ in range(len(PERMISSION_MODES))]
    assert seen == ["ask", "lockdown", "yolo", "default"], seen


def test_permission_cycle_is_session_scoped_and_never_writes_config(backend) -> None:
    """The cycle must not outlive its session, nor edit the user's profile.

    ``/perm`` is the persistent setting; shift+tab is a session override, so a
    new session — and the next process — starts from the configured mode again.
    """
    from son_of_anton_cli.config import get_config_path
    from tools.approval import get_session_permission_mode

    b, _rec = backend
    config = get_config_path()
    before = config.read_bytes() if config.exists() else None

    b.cycle_permission_mode()
    b.cycle_permission_mode()
    assert b.permission_mode() == "lockdown"
    assert get_session_permission_mode(b.session_id) == "lockdown"

    after = config.read_bytes() if config.exists() else None
    assert after == before, "cycling permission modes must not touch config.yaml"

    # A different session is unaffected: the override is keyed by session id.
    assert get_session_permission_mode("some-other-session") is None


def test_a_new_session_starts_back_on_the_configured_mode(backend) -> None:
    from tools.approval import get_session_permission_mode

    b, _rec = backend
    b.cycle_permission_mode()  # ask
    old_session = b.session_id
    assert get_session_permission_mode(old_session) == "ask"

    b.new_session(silent=True)
    assert b.session_id != old_session, "new_session should mint a fresh id"
    assert get_session_permission_mode(b.session_id) is None
    assert get_session_permission_mode(old_session) is None, "the old override was not released"
    assert b.permission_mode() == "default"


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


def test_shift_tab_is_bound_and_reaches_the_backend(backend) -> None:
    _textual()
    b, _rec = backend
    b.detach()
    called: list = []
    b.cycle_permission_mode = lambda: called.append(True) or "ask"

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause(0.3)
            app.query_one("#input").focus()
            await pilot.press("shift+tab")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not called:
                await pilot.pause(0.1)
            assert called, "shift+tab never reached the backend"

    asyncio.run(run())


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
            turns = [str(w.render()) for w in app.query_one("#feed").children if isinstance(w, _tui.UserTurn)]
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
            assert not [w for w in app.query_one("#feed").children if isinstance(w, _tui.UserTurn)]

    asyncio.run(run())


def test_a_running_front_end_is_visible_to_the_low_level_guards(backend) -> None:
    """approval / lazy-install must know not to prompt on a tty someone else owns."""
    from son_of_anton_constants import is_frontend_active

    b, _rec = backend  # the fixture attaches a sink
    assert is_frontend_active() is True
    b.detach()
    assert is_frontend_active() is False


# ---------------------------------------------------------------------------
# Lifecycle the old input loop used to drive
# ---------------------------------------------------------------------------

def test_idle_and_post_turn_hooks_run(backend, monkeypatch) -> None:
    """The behaviours the deleted REPL loop drove must still fire.

    Config-watch, background notifications and /loop ticks ran on idle; the
    turn footer, interrupt drain, goal judge and loop completion ran after a
    turn. Losing them to the interface swap would be a silent regression.
    """
    b, _rec = backend
    called: list = []

    for name in (
        "_check_config_mcp_changes",
        "_drain_process_notifications",
        "_maybe_fire_loop_tick",
        "_turn_summary_emit",
        "_drain_interrupt_queue_to_pending_input",
        "_maybe_continue_goal_after_turn",
        "_maybe_complete_loop_tick_after_turn",
    ):
        monkeypatch.setattr(b, name, lambda *a, _n=name, **k: called.append(_n))

    b.idle_tick()
    assert called == ["_check_config_mcp_changes", "_drain_process_notifications", "_maybe_fire_loop_tick"]

    called.clear()
    b.chat = lambda message, images=None: "ok"
    b.run_turn("hi")
    assert called[:4] == [
        "_turn_summary_emit",
        "_drain_interrupt_queue_to_pending_input",
        "_maybe_continue_goal_after_turn",
        "_maybe_complete_loop_tick_after_turn",
    ], called
    assert "_drain_process_notifications" in called

    # A turn in flight owns the floor; idle work must not interleave with it.
    called.clear()
    b._agent_running = True
    b.idle_tick()
    assert called == []
    b._agent_running = False


def test_a_failing_hook_does_not_break_the_turn(backend, monkeypatch) -> None:
    b, _rec = backend
    monkeypatch.setattr(b, "_turn_summary_emit", lambda: 1 / 0)
    b.chat = lambda message, images=None: "still fine"
    assert b.run_turn("hi") == "still fine"


# ---------------------------------------------------------------------------
# /commit
# ---------------------------------------------------------------------------

def test_commit_command_is_registered() -> None:
    from son_of_anton_cli.commands import resolve_command

    cmd = resolve_command("commit")
    assert cmd is not None and cmd.name == "commit"
    assert cmd.cli_only, "committing is an interactive action, not a chat-platform one"


def test_commit_prompt_pins_the_configured_author(backend, monkeypatch) -> None:
    """A configured identity pins the AUTHOR; the committer stays as-is.

    The whole point: the log reads "authored by the configured account,
    committed by you" — so the prompt must instruct `--author=` and must keep
    the committer on the repository's already-configured identity.
    """
    b, _rec = backend

    monkeypatch.setattr(
        b, "_commit_identity", lambda: ("son-of-anton-bot", "bot@users.noreply.github.com")
    )
    prompt = b.build_commit_prompt()
    assert "review all uncommitted git changes" in prompt
    assert "matching the existing style" in prompt
    assert "son-of-anton-bot <bot@users.noreply.github.com>" in prompt
    # The account is applied as the author, via --author, not as the committer.
    assert 'git commit --author="son-of-anton-bot <bot@users.noreply.github.com>"' in prompt
    assert "Author the commit as" in prompt
    assert "committer" in prompt
    assert "GIT_COMMITTER_NAME" in prompt

    # A different identity flows through unchanged.
    monkeypatch.setattr(b, "_commit_identity", lambda: ("Ada", "ada@example.com"))
    prompt = b.build_commit_prompt()
    assert 'git commit --author="Ada <ada@example.com>"' in prompt


def test_no_identity_is_shipped_as_a_default(backend) -> None:
    """Out of the box we commit as nobody in particular.

    Baking a name into the defaults would attribute other people's commits to
    an account they never chose; the identity is opt-in per install.
    """
    from son_of_anton_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["git"] == {"author_name": "", "author_email": ""}

    b, _rec = backend
    assert b._commit_identity() == ("", "")
    assert "do not override author" in b.build_commit_prompt()


def test_a_configured_identity_is_picked_up(backend, monkeypatch, son_of_anton_home) -> None:
    """Setting both keys in config.yaml is what turns the override on."""
    import yaml

    from son_of_anton_cli.config import load_config_readonly

    config_path = son_of_anton_home / "config.yaml"
    existing = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    existing = existing or {}
    existing["git"] = {"author_name": "a-bot", "author_email": "bot@example.invalid"}
    config_path.write_text(yaml.safe_dump(existing))
    load_config_readonly.cache_clear() if hasattr(load_config_readonly, "cache_clear") else None

    b, _rec = backend
    assert b._commit_identity() == ("a-bot", "bot@example.invalid")
    assert "a-bot <bot@example.invalid>" in b.build_commit_prompt()


def test_half_an_identity_is_not_used(backend, monkeypatch) -> None:
    """A name with no email (or vice versa) must not produce a broken author."""
    b, _rec = backend
    for half in ({"author_name": "a-bot"}, {"author_email": "bot@example.invalid"}):
        monkeypatch.setattr(
            "son_of_anton_cli.config.load_config_readonly", lambda _h=half: {"git": _h}
        )
        assert b._commit_identity() == ("", "")


def test_blank_identity_leaves_git_alone(backend, monkeypatch) -> None:
    """Blanking the config must not emit an empty author — it defers to git."""
    b, _rec = backend
    monkeypatch.setattr(b, "_commit_identity", lambda: ("", ""))
    prompt = b.build_commit_prompt()
    assert "do not override author" in prompt
    assert "<>" not in prompt and "Commit using this account" not in prompt


def test_commit_passes_extra_instructions_through(backend) -> None:
    b, _rec = backend
    prompt = b.build_commit_prompt("only the TUI files")
    assert "only the TUI files" in prompt


def test_commit_command_queues_a_turn_rather_than_committing_itself(backend) -> None:
    """/commit hands the work to the agent as a normal turn.

    Reviewing a diff and matching a repo's style is the model's job, and it
    already has the terminal tool — so this adds no model-tool surface, and the
    commit goes through the agent's usual approvals.
    """
    b, _rec = backend
    b._pending_agent_seed = None
    assert b.run_slash("/commit hold the version bump") is True
    seed = b._pending_agent_seed
    assert seed and "uncommitted git changes" in seed
    assert "hold the version bump" in seed


def test_commit_reaches_the_agent_through_the_app(backend) -> None:
    _textual()
    b, _rec = backend
    b.detach()
    sent: dict = {}
    b.chat = lambda message, images=None: sent.setdefault("msg", message) and "ok"

    async def run() -> None:
        app = _tui.SonOfAntonTUIApp(backend=b)
        async with app.run_test(size=(130, 32)) as pilot:
            await pilot.pause(0.3)
            app.query_one("#input").focus()
            app._prompt.text = "/commit"
            await pilot.press("enter")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and "msg" not in sent:
                await pilot.pause(0.1)
            assert "msg" in sent, "/commit never reached the agent"
            assert "uncommitted git changes" in sent["msg"]
            assert b._pending_agent_seed is None, "the one-shot seed was not consumed"

    asyncio.run(run())

