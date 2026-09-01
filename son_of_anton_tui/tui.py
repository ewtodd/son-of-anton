"""Phase-2 spike: a Textual front-end for Son of Anton.

NOT yet wired into the live AIAgent loop.  This is the layout iteration from
``TUI_AESTHETICS.md`` (Phase 2): a real application frame rather than a REPL.

  1. streaming markdown rendering (Textual's ``Markdown`` widget)
  2. a left context panel (model / context / mode / session / toolset counts)
  3. a chat transcript that separates user and assistant turns
  4. a status line + input dock + header/footer
  5. terminal-native palette mapped from the existing skin engine

The chrome is terminal-native: Textual's ``ansi-dark`` / ``ansi-light`` theme
is selected by the detected terminal polarity, and every surface token
(background, foreground, panel, surface, text) resolves to the terminal's own
defaults (``ansi_default``) rather than a hardcoded hex.  Only the accents
(primary / secondary / success / warning / error) are taken from the active
Son of Anton skin, remapped onto the terminal's ANSI palette.

It stays self-contained (runs with only ``textual`` installed) so it can be
exercised in a throwaway venv; if the skin engine isn't importable it degrades
to the ``ansi-*`` theme's own defaults.  ``textual`` lives in the opt-in ``tui``
extra, so importing this module must never hard-fail on a lean install.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

# `textual` is an opt-in dependency (extra `tui` / lazy feature `tui.textual`).
# Guard the module-level import so importing this module never hard-fails.
try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Header, Input, Markdown, Static

    _TEXTUAL_AVAILABLE = True
except Exception:  # pragma: no cover - import-time guard
    App = None  # type: ignore
    ComposeResult = None  # type: ignore
    Horizontal = Vertical = VerticalScroll = None  # type: ignore
    Footer = Header = Input = Markdown = Static = None  # type: ignore
    _TEXTUAL_AVAILABLE = False

_DEFAULT_AGENT = "Son of Anton Agent"

# The 16 ANSI palette names Textual maps to the terminal's own colors.
_ANSI_NAMES = {
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "gray", "grey", "default",
}


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
        if word in _ANSI_NAMES and word != "default":
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
    light terminal.  Otherwise we default to dark, which is the app's authored
    polarity and the common case.  Accent defaults differ slightly between the
    two ANSI themes; backgrounds/foregrounds are ``ansi_default`` either way.
    """
    fgbg = os.environ.get("COLORFGBG", "")
    if fgbg:
        try:
            parts = fgbg.split(";")
            if len(parts) >= 2:
                # Dim indexes 0-7 = dark palette; bright 8-15 = light palette.
                return "light" if int(parts[-1]) >= 8 else "dark"
        except (ValueError, IndexError):
            pass
    return "dark"


def _theme_name() -> str:
    """Name of the terminal-following Textual theme for the active skin."""
    return f"ansi-{_polarity()}"


def is_available() -> bool:
    """Return True when Textual is installed (the ``tui`` extra is present)."""
    return _TEXTUAL_AVAILABLE


if _TEXTUAL_AVAILABLE:

    class SonOfAntonTUIApp(App):
        """Textual scaffold: a context panel, a streaming transcript, and a dock."""

        TITLE = _DEFAULT_AGENT
        SUB_TITLE = "Textual front-end"

        # Terminal-native chrome: every surface token resolves to the
        # terminal's own defaults (ansi_default) via the ansi-* theme.  Only
        # the accents ride the active Son of Anton skin.
        CSS = """
        Screen {
            background: $background;
        }
        #context {
            width: 34;
            padding: 0 1;
            border-right: solid $primary;
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
        #feed {
            padding: 0 2;
        }
        #feed .user {
            color: $text;
            background: $primary 12%;
            border-left: solid $primary;
            padding: 0 1;
            margin-top: 1;
        }
        #feed Markdown {
            background: transparent;
            margin-top: 1;
            padding: 0 1;
        }
        #statusline {
            height: 1;
            padding: 0 1;
            color: $text-muted;
            background: $background;
        }
        #cmd {
            dock: bottom;
            height: 3;
            border-top: solid $primary;
            background: $background;
        }
        #cmd Input {
            height: 3;
            padding: 0 1;
        }
        Footer {
            background: $background;
        }
        """

        BINDINGS = [("ctrl+q", "quit", "Quit"), ("ctrl+l", "clear", "Clear")]

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
                with VerticalScroll(id="feed"):
                    yield Static("", id="feed-anchor")
            yield Static(" ready", id="statusline")
            with Vertical(id="cmd"):
                yield Input(
                    placeholder="Send a message… (Tab to toggle focus, ctrl+q quit)",
                    id="input",
                )
            yield Footer()

        def on_mount(self) -> None:
            self._feed = self.query_one("#feed", VerticalScroll)
            self._statusline = self.query_one("#statusline", Static)
            self._input = self.query_one("#input", Input)
            # Focus the input immediately: the scrollable feed is the first
            # focusable widget in DOM order, so Textual otherwise parks focus
            # there and keystrokes never reach the input.
            self._input.focus()
            # Opening assistant message, streamed in chunks.
            self.run_worker(self._stream_assistant(_WELCOME), group="demo", exclusive=True)

        async def _append_user(self, text: str) -> None:
            await self._feed.mount(
                Static(f"[b]you[/b]  {text}", classes="user")
            )
            self._feed.scroll_end(animate=False)

        async def _stream_assistant(self, chunks: list[str], *, reply: str = "") -> None:
            """Mount an assistant Markdown block and stream chunks into it.

            ``chunks`` append to this block; ``reply`` (if given) is the canned
            assistant turn that quickly replaces the empty streaming block.
            ``_transcript`` always accumulates the full assistant history (for
            recap/tests), while the on-screen block shows only its own content.
            """
            md = Markdown("")
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

        async def on_input_submitted(self, event: Input.Submitted) -> None:
            value = (event.value or "").strip()
            if not value:
                event.input.value = ""
                return
            await self._append_user(value)
            event.input.value = ""
            self._set_status("thinking…")
            await self._stream_assistant([], reply=_CANNED_REPLY)

        def action_clear(self) -> None:
            """Ctrl+L — clear the transcript (and reset the accumulator)."""
            from textual.widgets import Markdown as _MD

            for w in list(self._feed.children):
                w.remove()
            self._transcript = ""
            self._feed.mount(_MD(""))

else:
    # Textual absent on a lean install: expose a consistent None so callers
    # can check availability without an AttributeError.
    SonOfAntonTUIApp = None  # type: ignore


_WELCOME = [
    "## Son of Anton\n\n",
    "This is the **Textual** front-end — a real app frame, not a REPL.\n\n",
    "- streaming markdown\n",
    "- a left context panel\n",
    "- user / assistant turns\n",
    "\n```python\nprint('fenced code with syntax highlighting')\n```\n",
    "\n| k | v |\n|---|---|\n| a | 1 |\n| b | 2 |\n",
    "\n> A blockquote with a link and math source: "
    r"`\int_0^\infty e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}`" "\n",
    "\nType below and press Enter to see the turn pattern.\n",
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
