"""Phase-2 spike: a Textual front-end for Son of Anton (opencode-inspired).

NOT yet wired into the live AIAgent loop.  This is the layout iteration from
``TUI_AESTHETICS.md`` (Phase 2): a real application frame rather than a REPL,
mimicking opencode's terminal layout as closely as Textual allows.

Layout (mirrors opencode):
  * the transcript + prompt occupy the main column on the LEFT;
  * a 42-col context panel sits on the RIGHT;
  * the right panel only renders when the terminal is wide enough (opencode's
    threshold is width > 120) — below that it disappears and the transcript
    takes the full width;
  * a multi-line prompt editor (TextArea) that grows with your typing, where
    Enter submits and Shift+Enter inserts a newline;
  * the ASCII "SON OF ANTON" wordmark is shown at startup ("at least in the
    beginning!").

Chrome is terminal-native: Textual's ``ansi-dark`` / ``ansi-light`` theme is
selected from the detected terminal polarity, so every surface token
(background, foreground, panel, surface, text) resolves to the terminal's own
defaults (``ansi_default``) rather than a hardcoded hex.  Only the accents ride
the active Son of Anton skin, remapped onto the terminal's ANSI palette.

The transcript renders **without syntax highlighting**: fenced code blocks are
shown as plain text (opencode-style), keeping only the markdown structure.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

# `textual` is an opt-in dependency (extra `tui` / lazy feature `tui.textual`).
# Guard the module-level import so importing this module never hard-fails.
try:
    from textual.app import App, ComposeResult
    from textual import events, on
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.content import Content
    from textual.message import Message
    from textual.widgets import Footer, Header, Markdown, Static, TextArea

    _TEXTUAL_AVAILABLE = True
except Exception:  # pragma: no cover - import-time guard
    App = None  # type: ignore
    ComposeResult = None  # type: ignore
    events = None  # type: ignore
    on = None  # type: ignore
    Horizontal = Vertical = VerticalScroll = None  # type: ignore
    Content = None  # type: ignore
    Message = None  # type: ignore
    Footer = Header = Markdown = Static = TextArea = None  # type: ignore
    _TEXTUAL_AVAILABLE = False

_DEFAULT_AGENT = "Son of Anton Agent"

# opencode: the right panel only appears when the terminal is wide and is 42
# columns wide (see packages/tui/src/routes/session/index.tsx: `width > 120`,
# `contentWidth ... (sidebarVisible() ? 42 : 0)`).
SIDEBAR_THRESHOLD = 120
SIDEBAR_WIDTH = 42


def _to_ansi(value: str, default: str) -> str:
    """Map a skin token onto a terminal-following Textual color.

    The Son of Anton default skin stores terminal-native tokens (``yellow``,
    ``dim yellow``, ``cyan``, ``default``, ...).  For Textual we want those to
    resolve through the *terminal's* ANSI palette — the ``ansi_<color>``
    reference — not a fixed RGB name.  ``ansi_default`` keeps the terminal's
    default foreground, so ``default`` / empty / attribute-only tokens land on
    whatever the terminal uses for text.  A genuine hex value from a custom
    skin passes through untouched (that is the user's explicit choice).
    """
    v = (value or "").strip().lower()
    if not v:
        return default
    for word in v.replace(",", " ").split():
        if word in {"black", "red", "green", "yellow", "blue", "magenta", "cyan",
                    "white", "gray", "grey"}:
            return f"ansi_{word}"
        if word == "default":
            return "ansi_default"
    if v.startswith("#") and len(v) in (4, 7):
        return v
    return default


def _skin_accent(key: str, default: str) -> str:
    """Resolve one skin color token to a Textual ``ansi_*`` value (safe)."""
    try:
        from son_of_anton_cli.skin_engine import get_active_skin

        return _to_ansi(get_active_skin().get_color(key, ""), default)
    except Exception:
        return default


def _polarity() -> str:
    """Best-effort detect the terminal polarity (``dark`` / ``light``).

    Textual does not auto-detect the terminal background, so we infer it from
    the environment.  ``COLORFGBG`` (exported by several terminals) is
    ``<fg>;<bg>`` of 16-color indices; a bright background index (>= 8) means a
    light terminal.  Otherwise we default to dark.  Accent defaults differ
    slightly between the two ANSI themes; backgrounds/foregrounds are
    ``ansi_default`` either way.
    """
    fgbg = os.environ.get("COLORFGBG", "")
    if fgbg:
        try:
            parts = fgbg.split(";")
            if len(parts) >= 2:
                return "light" if int(parts[-1]) >= 8 else "dark"
        except (ValueError, IndexError):
            pass
    return "dark"


def _theme_name() -> str:
    """Name of the terminal-following Textual theme for the active skin."""
    return f"ansi-{_polarity()}"


def _wordmark(feed_w: int) -> str:
    """Return the ASCII wordmark that fits a *feed* column of ``feed_w``.

    Picks the wide form -> the stacked form -> a compact one-liner so the
    block letters never wrap into rubble.  The width we must fit is the feed
    column (``width - SIDEBAR_WIDTH`` when the right panel is shown), not the
    full terminal width — the old bug was choosing by the full width and then
    dropping a 109-col logo into a ~79-col feed.
    """
    try:
        from son_of_anton_cli.banner import (
            SON_OF_ANTON_AGENT_LOGO_WIDE as _WIDE,
            SON_OF_ANTON_AGENT_LOGO_STACKED as _STACKED,
        )
    except Exception:
        return "SON OF ANTON"
    # #feed has `padding: 0 2`, so the usable width is feed_w - 4.
    avail = feed_w - 4
    if avail >= 109:  # the wide wordmark is 109 cols
        return _WIDE
    if avail >= 52:   # the stacked wordmark is 52 cols
        return _STACKED
    return "SON OF ANTON"


def is_available() -> bool:
    """Return True when Textual is installed (the ``tui`` extra is present)."""
    return _TEXTUAL_AVAILABLE


if _TEXTUAL_AVAILABLE:
    from textual.widgets._markdown import MarkdownFence  # type: ignore

    class _PlainFence(MarkdownFence):
        """A fenced code block rendered as plain text (no syntax highlighting).

        Mirrors opencode: the transcript keeps markdown structure but code is
        uncoloured, not rainbowed by a Pygments theme.
        """

        @classmethod
        def highlight(cls, code: str, language: str, ansi: bool = False, dark: bool = False) -> Content:
            return Content(code)

    class PlainMarkdown(Markdown):
        """Markdown that renders fenced code without syntax highlighting."""

        BLOCKS = {**Markdown.BLOCKS, "fence": _PlainFence, "code_block": _PlainFence}

    class PromptArea(TextArea):
        """A multi-line prompt editor: Enter submits, Shift+Enter newlines."""

        class Submitted(Message):
            """Posted when the user presses Enter to submit the prompt."""

            def __init__(self, prompt: "PromptArea", value: str) -> None:
                super().__init__()
                self.prompt = prompt
                self.value = value

        async def _on_key(self, event: events.Key) -> None:
            """Enter submits; Shift+Enter inserts a newline.

            TextArea's own ``_on_key`` consumes ``enter`` as a newline before
            bindings get a chance, so we intercept it here (opencode's prompt
            editor submits on Enter too).
            """
            key = event.key or ""
            if key == "enter":
                event.stop()
                event.prevent_default()
                self.post_message(self.Submitted(self, self.text.rstrip("\n")))
                return
            if key.endswith("+enter"):
                event.stop()
                event.prevent_default()
                self.insert("\n")
                return
            await super()._on_key(event)

    class SonOfAntonTUIApp(App):
        """opencode-inspired frame: right panel, transcript, and a prompt dock."""

        TITLE = _DEFAULT_AGENT
        SUB_TITLE = "Textual front-end"

        CSS = """
        Screen {
            background: $background;
        }
        Horizontal#split {
            width: 1fr;
            height: 1fr;
        }
        Vertical#content {
            width: 1fr;
        }
        VerticalScroll#feed {
            width: 1fr;
            height: 1fr;
            padding: 0 2;
            scrollbar-size-vertical: 1;
            scrollbar-color: $text-muted;
            scrollbar-color-hover: $primary;
            scrollbar-color-active: $primary;
            scrollbar-background: transparent;
        }
        #statusline {
            height: 1;
            padding: 0 1;
            color: $text-muted;
            background: $background;
        }
        #cmd {
            height: auto;
            max-height: 10;
            border-top: solid $primary;
            background: $background;
        }
        #cmd PromptArea {
            height: auto;
            min-height: 3;
            max-height: 10;
            padding: 0 1;
            background: $background;
        }
        Vertical#context {
            width: 42;
            padding: 0 1;
            border-left: solid $primary;
            background: $background;
        }
        #context .label {
            color: $text-muted;
            text-style: bold;
            margin-top: 1;
        }
        #context .kv {
            color: $text;
            margin-bottom: 0;
        }
        #context .value {
            color: $primary;
        }
        #context .muted {
            color: $text-muted;
        }
        #feed .wordmark {
            text-style: bold;
            color: $primary;
            margin: 1 0;
        }
        #feed .user {
            color: $text;
            padding: 0 1;
            margin-top: 1;
        }
        #feed PlainMarkdown {
            background: transparent;
            margin-top: 1;
            padding: 0 1;
        }
        Footer {
            background: $background;
        }
        """

        BINDINGS = [
            ("ctrl+q", "quit", "Quit"),
            ("ctrl+l", "clear", "Clear"),
        ]

        def __init__(self, agent_name: str = _DEFAULT_AGENT, **kwargs: Any) -> None:
            self.agent_name = agent_name
            # Resolve before super().__init__: Textual reads get_css_variables()
            # during init and we want the accent palette and ansi theme already
            # decided.  Set the theme right after so waiters refresh with it.
            self._theme = _theme_name()
            self._transcript = ""  # concatenated assistant text (for tests/recap)
            self._status = "ready"
            super().__init__(**kwargs)
            self.theme = self._theme

        def get_css_variables(self) -> dict[str, str]:
            """Overlay the active skin's accents on the ansi theme.

            The ansi-* theme already gives terminal-native backgrounds,
            foregrounds and panels; we only override the accent tokens with the
            skin's (remapped onto the terminal palette).  We deliberately do
            NOT force ``background`` / ``panel`` / ``surface`` / ``text`` so the
            chrome keeps riding the terminal's defaults.
            """
            try:
                vars_ = dict(super().get_css_variables())
            except Exception:
                vars_ = {}
            vars_.update(
                {
                    "primary": _skin_accent("ui_accent", "ansi_yellow"),
                    "secondary": _skin_accent("ui_thinking", "ansi_cyan"),
                    "success": _skin_accent("ui_ok", "ansi_green"),
                    "warning": _skin_accent("ui_warn", "ansi_yellow"),
                    "error": _skin_accent("ui_error", "ansi_red"),
                    "accent": _skin_accent("ui_accent", "ansi_yellow"),
                }
            )
            return vars_

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal(id="split"):
                with Vertical(id="content"):
                    with VerticalScroll(id="feed"):
                        yield Static("", id="feed-anchor")
                    yield Static(" ready", id="statusline")
                    with Vertical(id="cmd"):
                        yield PromptArea(
                            placeholder="Send a message… (Enter submits, Shift+Enter newline, ctrl+q / :q quit)",
                            id="input",
                        )
                with Vertical(id="context"):
                    yield Static("Session", classes="label")
                    yield Static(f" {self.agent_name}", classes="value")
                    yield Static("", classes="kv")
                    yield Static("Model", classes="label")
                    yield Static(" deepseek-v4-api", classes="kv value")
                    yield Static("Context", classes="label")
                    yield Static(" 128K", classes="kv value")
                    yield Static("Mode", classes="label")
                    yield Static(" standard", classes="kv value")
                    yield Static("Working dir", classes="label")
                    yield Static(f" {os.getcwd()}", classes="kv muted")
                    yield Static("", classes="kv")
                    yield Static("Tools", classes="label")
                    yield Static(" wired", classes="kv value")
            yield Footer()

        def on_mount(self) -> None:
            self._feed = self.query_one("#feed", VerticalScroll)
            self._statusline = self.query_one("#statusline", Static)
            self._prompt = self.query_one("#input", PromptArea)
            # Focus the prompt immediately: the scrollable feed is the first
            # focusable widget in DOM order, so Textual otherwise parks focus
            # there and keystrokes never reach the prompt.
            self._prompt.focus()
            self._apply_sidebar(self.size.width)
            # Opening frame: wordmark, then the streaming welcome.
            self._show_wordmark()
            self.run_worker(self._stream_assistant(_WELCOME), group="demo", exclusive=True)

        def on_resize(self, event: Any) -> None:
            self._apply_sidebar(event.size.width)
            self._show_wordmark()

        def _apply_sidebar(self, width: int) -> None:
            """Show the right context panel only when the terminal is wide."""
            try:
                self.query_one("#context", Vertical).display = (
                    "block" if width > SIDEBAR_THRESHOLD else "none"
                )
            except Exception:
                pass

        def _wordmark_text(self, feed_w: int) -> str:
            lines = _wordmark(feed_w).splitlines()
            if not lines:
                return ""
            avail = max(0, feed_w - 4)  # #feed padding: 0 2
            width = max(len(line) for line in lines)
            pad = max(0, (avail - width) // 2)
            return "\n".join(f"{' ' * pad}{line}" for line in lines)

        def _show_wordmark(self) -> None:
            """Render (or re-render) the wordmark at the top of the feed.

            Recomputes the variant from the current feed width so it switches
            between the wide / stacked forms live as the window is resized.
            """
            if getattr(self, "_feed", None) is None:
                return
            if getattr(self, "_wordmark_widget", None) is None:
                self._wordmark_widget = Static("", id="wordmark", classes="wordmark")
                self._feed.mount(self._wordmark_widget)
            feed_w = self.size.width - (
                SIDEBAR_WIDTH if self.size.width > SIDEBAR_THRESHOLD else 0
            )
            self._wordmark_widget.update(self._wordmark_text(feed_w))

        async def _append_user(self, text: str) -> None:
            # Highlight the user's message by the *prompt symbol*-less prefix
            # but keep it uncoloured (no syntax highlighting).
            await self._feed.mount(Static(f"[b]you[/b]  {text}", classes="user"))
            self._feed.scroll_end(animate=False)

        async def _stream_assistant(self, chunks: list[str], *, reply: str = "") -> None:
            """Mount an assistant Markdown block and stream chunks into it.

            ``chunks`` append to this block; ``reply`` (if given) is the canned
            assistant turn that quickly replaces the empty streaming block.
            ``_transcript`` always accumulates the full assistant history (for
            recap/tests), while the on-screen block shows only its own content.
            """
            md = PlainMarkdown("")
            await self._feed.mount(md)
            self._feed.scroll_end(animate=False)
            block = ""
            for chunk in chunks:
                block += chunk
                self._transcript += chunk
                md.update(block)
                self._set_status(f"working · {len(self._transcript)} chars")
                await asyncio.sleep(0.12)
                self._feed.scroll_end(animate=True)
            if reply:
                self._transcript += reply
                md.update(reply)
            self._set_status("ready")
            self._feed.scroll_end(animate=True)

        def _set_status(self, text: str) -> None:
            self._status = text
            self._statusline.update(f" {text}")

        @on(PromptArea.Submitted)
        async def _handle_submit(self, event: Any) -> None:
            value = (getattr(event, "value", "") or "").strip()
            if value == ":q" or value == ":quit":
                self.exit()
                return
            if not value:
                self._prompt.text = ""
                return
            await self._append_user(value)
            self._prompt.text = ""
            self._set_status("thinking…")
            await self._stream_assistant([], reply=_CANNED_REPLY)

        def action_clear(self) -> None:
            """Ctrl+L — clear the transcript (and reset the accumulator)."""
            for w in list(self._feed.children):
                w.remove()
            self._transcript = ""
            self._feed.mount(PlainMarkdown(""))

else:
    # Textual absent on a lean install: expose a consistent None so callers
    # can check availability without an AttributeError.
    SonOfAntonTUIApp = None  # type: ignore


_WELCOME = [
    "## Son of Anton\n\n",
    "This is the **Textual** front-end — a real app frame, not a REPL.\n\n",
    "- streaming markdown\n",
    "- a right-hand context panel (wide terminals)\n",
    "- multi-line prompt (Enter submits, Shift+Enter newline)\n",
    "\n```python\nprint('fenced code renders as PLAIN text — no syntax highlighting')\n```\n",
    "\n| k | v |\n|---|---|\n| a | 1 |\n| b | 2 |\n",
    "\n> A blockquote with a link and math source: "
    r"`\int_0^\infty e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}`" "\n",
    "\nType below and press Enter to see the turn pattern. `:q` quits.\n",
]

_CANNED_REPLY = (
    "\n\n## A canned reply\n\n"
    "This is where a real **assistant** turn lands. When the agent loop is wired in, "
    "this block streams the live response instead of this placeholder.\n\n"
    "- markdown renders inline\n"
    "- math stays as source (`\\int ...`) unless we opt into the image path\n"
)


def _run_demo() -> None:
    """Headless demo path (used by ``python -m son_of_anton_tui.tui --demo``)."""
    if not _TEXTUAL_AVAILABLE:
        print("Textual is not installed. Install the `tui` extra: pip install -e '.[tui]'")
        return
    SonOfAntonTUIApp().run()


if __name__ == "__main__":
    _run_demo()
