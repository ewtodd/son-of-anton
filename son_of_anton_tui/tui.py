"""Phase-2 spike: a Textual front-end for Son of Anton.

NOT yet wired into the live CLI.  This is the proof-of-concept from
``TUI_AESTHETICS.md`` (Phase 2, step 1) — it demonstrates the three
capabilities the Textual rebuild needs:

  1. streaming markdown rendering (Textual's ``Markdown`` widget)
  2. an application frame (header / transcript / input dock / footer)
  3. a terminal-native palette mapped from the existing skin engine

It is deliberately self-contained so it can be exercised in a throwaway
venv with only ``textual`` installed (see ``App.run_test`` below); if the
skin engine isn't importable it degrades to Textual's default colors.

``textual`` lives in the opt-in ``tui`` extra (lazy feature ``tui.textual``),
so importing this module must never hard-fail on a lean install.  When
Textual isn't available ``SonOfAntonTUIApp`` is ``None`` and
``is_available()`` returns ``False``.
"""

from __future__ import annotations

import os
from typing import Any

# `textual` is an opt-in dependency (extra `tui` / lazy feature `tui.textual`).
# Guard the module-level import so importing this module never hard-fails.
try:
    from textual.app import App, ComposeResult
    from textual.containers import Vertical
    from textual.reactive import reactive
    from textual.widgets import Footer, Header, Input, Markdown, Static

    _TEXTUAL_AVAILABLE = True
except Exception:  # pragma: no cover - import-time guard
    App = None  # type: ignore
    ComposeResult = None  # type: ignore
    Vertical = None  # type: ignore
    reactive = None  # type: ignore
    Footer = Header = Input = Markdown = Static = None  # type: ignore
    _TEXTUAL_AVAILABLE = False

_DEFAULT_AGENT = "Son of Anton Agent"


def is_available() -> bool:
    """Return True when Textual is installed (the ``tui`` extra is present)."""
    return _TEXTUAL_AVAILABLE


def _skin_palette() -> dict[str, str]:
    """Resolve a few terminal-native color tokens from the active skin.

    Reading the skin keeps the rebuild on the same palette as the rest of
    the app.  Falls back to Textual's named ANSI colors (which follow the
    terminal theme) when the skin engine isn't importable.
    """
    try:
        from son_of_anton_cli.skin_engine import get_active_skin

        skin = get_active_skin()
        return {
            "accent": skin.get_color("ui_accent", "yellow") or "yellow",
            "thinking": skin.get_color("ui_thinking", "cyan") or "cyan",
            "dim": skin.get_color("banner_dim", "gray") or "gray",
            "body": skin.get_color("banner_text", "white") or "white",
        }
    except Exception:
        return {"accent": "yellow", "thinking": "cyan", "dim": "gray", "body": "white"}


if _TEXTUAL_AVAILABLE:

    class SonOfAntonTUIApp(App):
        """Minimal Textual scaffold: streaming markdown + a command dock."""

        CSS = """
        Screen {
            background: $surface;
        }
        #sidebar {
            width: 24;
            padding: 0 1;
            border-right: dashed $primary;
            background: $boost;
        }
        #transcript {
            padding: 0 1;
        }
        #transcript Markdown {
            background: transparent;
        }
        #status {
            height: auto;
            padding: 0 1;
            color: $text-muted;
            background: $surface;
        }
        #cmd {
            dock: bottom;
            height: 3;
            border-top: solid $primary;
        }
        #cmd Input {
            height: 3;
        }
        """

        BINDINGS = [("ctrl+q", "quit", "Quit")]

        def __init__(self, agent_name: str = _DEFAULT_AGENT, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.agent_name = agent_name
            self._palette = _skin_palette()
            self._transcript: str = ""

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Vertical(classes="root"):
                with Vertical(id="main", classes="horizontal"):
                    yield Static(self.agent_name, id="sidebar")
                    yield Markdown("", id="transcript", classes="scroll")
                yield Static(
                    f" {self.agent_name} · waiting — type then Enter; ctrl+q to quit",
                    id="status",
                )
                with Vertical(id="cmd"):
                    yield Input(placeholder="Say anything…", id="input")
            yield Footer()

        def on_mount(self) -> None:
            self._input = self.query_one("#input", Input)
            self._md = self.query_one("#transcript", Markdown)
            self._md.update(
                "## Son of Anton TUI spike\n\n"
                "This is **streaming markdown** rendered by Textual. "
                "A worker below streams the rest of the document in chunks.\n"
            )
            self.run_worker(self._demo_stream(), group="demo", exclusive=True)

        async def _demo_stream(self) -> None:
            """Append a small markdown doc token-by-token to prove streaming."""
            chunks = [
                "\n\n### Why this matters\n",
                "- Todo list\n",
                "- **bold**, *italic*, `code`\n",
                "\n```python\nprint('fenced code with syntax highlighting')\n```\n",
                "\n| k | v |\n|---|---|\n| a | 1 |\n| b | 2 |\n",
                "\n> A blockquote with a **link** and math source: "
                r"`\int_0^\infty e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}`" "\n",
            ]
            import asyncio

            for chunk in chunks:
                self._transcript += chunk
                self._md.update(self._transcript)
                await asyncio.sleep(0.15)

        def on_input_submitted(self, event: Input.Submitted) -> None:
            value = (event.value or "").strip()
            if not value:
                return
            self._transcript += f"\n\n> **you:** {value}\n"
            self._md.update(self._transcript)
            event.input.value = ""

else:
    # Textual absent on a lean install: expose a consistent None so callers
    # can check availability without an AttributeError.
    SonOfAntonTUIApp = None  # type: ignore


def _run_demo() -> None:
    """Headless demo path (used by ``python -m son_of_anton_tui.tui --demo``)."""
    if not _TEXTUAL_AVAILABLE:
        print("Textual is not installed. Install the `tui` extra: pip install -e '.[tui]'")
        return
    SonOfAntonTUIApp().run()


if __name__ == "__main__":
    _run_demo()
