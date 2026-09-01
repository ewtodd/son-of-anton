"""Phase-2 spike: a Textual front-end for Son of Anton.

NOT yet wired into the live AIAgent loop.  This is the layout iteration from
``TUI_AESTHETICS.md`` (Phase 2): a real application frame rather than a REPL.

  1. streaming markdown rendering (Textual's ``Markdown`` widget)
  2. a left context panel (model / context / mode / session / toolset counts)
  3. a chat transcript that separates user and assistant turns
  4. a status line + input dock + header/footer
  5. terminal-native palette mapped from the existing skin engine

It stays self-contained (runs with only ``textual`` installed) so it can be
exercised in a throwaway venv; if the skin engine isn't importable it degrades
to Textual's default colors.  ``textual`` lives in the opt-in ``tui`` extra, so
importing this module must never hard-fail on a lean install.
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

# Textual-parseable color names; the skin's terminal-native tokens map onto these.
_KNOWN_COLORS = {
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "gray", "grey", "bright_black", "bright_red", "bright_green", "bright_yellow",
    "bright_blue", "bright_magenta", "bright_cyan", "bright_white", "default",
}


def _to_textual_color(value: str, fallback: str) -> str:
    """Return a Textual-parseable color for a skin token, else ``fallback``."""
    v = (value or "").strip()
    low = v.lower()
    if not v:
        return fallback
    # Hex or known name passes through; attribute tokens map to a sane default.
    if v.startswith("#") and len(v) in (4, 7):
        return v
    if low in _KNOWN_COLORS:
        return low if low != "default" else fallback
    return fallback


def _skin_palette() -> dict[str, str]:
    """Resolve terminal-native color tokens from the active skin (Textual-safe).

    Falls back to Textual's named ANSI colors (which follow the terminal theme)
    when the skin engine isn't importable.
    """
    try:
        from son_of_anton_cli.skin_engine import get_active_skin

        skin = get_active_skin()
        return {
            "accent": _to_textual_color(skin.get_color("ui_accent", "yellow"), "yellow"),
            "thinking": _to_textual_color(skin.get_color("ui_thinking", "cyan"), "cyan"),
            "dim": _to_textual_color(skin.get_color("banner_dim", ""), "gray"),
            "body": _to_textual_color(skin.get_color("banner_text", ""), "white"),
            "surface": _to_textual_color(skin.get_color("status_bar_bg", ""), "#101010"),
            "ok": _to_textual_color(skin.get_color("ui_ok", ""), "green"),
            "warn": _to_textual_color(skin.get_color("ui_warn", ""), "yellow"),
            "error": _to_textual_color(skin.get_color("ui_error", ""), "red"),
        }
    except Exception:
        return {
            "accent": "yellow", "thinking": "cyan", "dim": "gray",
            "body": "white", "surface": "#101010",
            "ok": "green", "warn": "yellow", "error": "red",
        }


def is_available() -> bool:
    """Return True when Textual is installed (the ``tui`` extra is present)."""
    return _TEXTUAL_AVAILABLE


if _TEXTUAL_AVAILABLE:

    class SonOfAntonTUIApp(App):
        """Textual scaffold: a context panel, a streaming transcript, and a dock."""

        TITLE = _DEFAULT_AGENT
        SUB_TITLE = "Textual front-end"

        CSS = """
        Screen {
            background: $panel;
        }
        #context {
            width: 34;
            padding: 0 1;
            border-right: solid $primary;
            background: $panel;
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
            background: $boost;
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
            background: $panel;
        }
        #cmd {
            dock: bottom;
            height: 3;
            border-top: solid $primary;
            background: $panel;
        }
        #cmd Input {
            height: 3;
            padding: 0 1;
        }
        Footer {
            background: $panel;
        }
        """

        BINDINGS = [("ctrl+q", "quit", "Quit"), ("ctrl+l", "clear", "Clear")]

        def __init__(self, agent_name: str = _DEFAULT_AGENT, **kwargs: Any) -> None:
            self.agent_name = agent_name
            # Set BEFORE super().__init__: Textual reads get_css_variables() from
            # App.__init__, which needs the resolved palette already present.
            self._palette = _skin_palette()
            self._transcript = ""  # concatenated assistant text (for tests/recap)
            self._status = "ready"
            super().__init__(**kwargs)

        def get_css_variables(self) -> dict[str, str]:
            """Inject the skin palette over Textual's design tokens."""
            try:
                vars_ = dict(super().get_css_variables())
            except Exception:
                vars_ = {}
            p = self._palette
            vars_.update(
                {
                    "primary": p["accent"],
                    "secondary": p["thinking"],
                    "text": p["body"],
                    "text-muted": p["dim"],
                    "panel": p["surface"],
                    "boost": p["surface"],
                    "success": p["ok"],
                    "warning": p["warn"],
                    "error": p["error"],
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
                    yield Static("Tools", classes="label")
                    yield Static(" 12 wired · 8 available", classes="kv value")
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
