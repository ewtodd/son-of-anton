"""Son of Anton — the Textual front-end.

A real application frame (opencode-inspired) instead of an input line plus
scrollback:

  * the transcript + prompt fill the main column on the LEFT;
  * a 42-col context panel sits on the RIGHT on wide terminals (> 120 cols);
  * a multi-line prompt: Enter submits, Shift+Enter inserts a newline; messages
    submitted while a turn is running queue above the prompt, and ``/sendnow``
    redirects the live turn onto the oldest one; ``/sendall`` sends them all;
  * slash commands, their arguments and ``@`` references autocomplete inline,
    Tab also completes file paths, history shows as ghost text, and ctrl+p
    opens a fuzzy command palette;
  * the ASCII "SON OF ANTON" wordmark opens the session.

The agent is the soa-d daemon.  ``son_of_anton_tui.backend.DaemonBackend``
is this app's client of it: the daemon's events (assistant tokens, reasoning,
tool lifecycle, notes, queue and status changes, approval and question
requests) arrive as typed events that this app renders, and what the user
types goes straight to the daemon, which starts the turn or queues it.  Modal
prompts are the daemon's approval and question requests, answered through
the backend.

Chrome is terminal-native: Textual's ``ansi-dark`` / ``ansi-light`` theme is
picked from the detected terminal polarity so backgrounds, foregrounds and
panels resolve to the terminal's own defaults; only the accents ride the active
Son of Anton skin, remapped onto the terminal's ANSI palette.  Fenced code in
the transcript renders as plain text (opencode-style): markdown structure, no
syntax rainbow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Optional

try:
    from textual import events, on
    from textual.app import App, ComposeResult, SuspendNotSupported
    from textual.binding import Binding
    from textual.command import DiscoveryHit, Hit, Hits, Provider
    from textual.containers import Container, Horizontal, Vertical, VerticalScroll
    from textual.message import Message
    from textual.screen import ModalScreen
    from textual.widgets import (
        Input,
        OptionList,
        SelectionList,
        Static,
        TextArea,
    )
    from textual.widgets.option_list import Option

    from son_of_anton_tui.feed import FeedStyle, VirtualFeed, note_width
    from son_of_anton_tui import completion as _completion

    _TEXTUAL_AVAILABLE = True
except Exception:  # pragma: no cover - import-time guard
    App = None  # type: ignore
    ComposeResult = None  # type: ignore
    SuspendNotSupported = Exception  # type: ignore
    events = on = None  # type: ignore
    Binding = None  # type: ignore
    DiscoveryHit = Hit = Hits = Provider = None  # type: ignore
    Container = Horizontal = Vertical = VerticalScroll = None  # type: ignore
    Message = None  # type: ignore
    ModalScreen = None  # type: ignore
    Input = OptionList = SelectionList = Static = TextArea = None  # type: ignore
    Option = None  # type: ignore
    FeedStyle = VirtualFeed = None  # type: ignore
    _TEXTUAL_AVAILABLE = False

_DEFAULT_AGENT = "Son of Anton Agent"

# opencode: the right panel only appears when the terminal is wide, and is 42
# columns (packages/tui/src/routes/session/index.tsx: `width > 120`).
SIDEBAR_THRESHOLD = 120
SIDEBAR_WIDTH = 42
FEED_PADDING = 2  # #feed has `padding: 0 2`

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# opencode gives each tool a one-glyph icon in a two-cell column
# (INLINE_TOOL_ICON_WIDTH), so labels align no matter the tool.
_TOOL_ICONS = {
    "terminal": "$",
    "execute_code": "$",
    "read_file": "→",
    "web_extract": "%",
    "web_search": "◈",
    "search_files": "✱",
    "session_search": "✱",
    "write_file": "←",
    "patch": "←",
    "delegate_task": "✓",
    "memory": "←",
    "todo": "☰",
    "clarify": "→",
    "cronjob": "◔",
    "vision_analyze": "◉",
}


def _tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name or "", "⚙")


def _to_ansi(value: str, default: str) -> str:
    """Map a skin token onto a terminal-following Textual color.

    Named ANSI colours (``yellow``, ``dim cyan``…) become ``ansi_<name>`` so
    the terminal's own palette decides the hue; ``default`` becomes
    ``ansi_default``; a genuine hex from a custom skin passes through.

    Idempotent: an ``ansi_<name>`` token is already mapped. ``_rich_color``
    feeds ``_skin_accent``'s output back through here, and rejecting that
    token blanked every transcript colour (reasoning, errors, headings).
    """
    v = (value or "").strip().lower()
    if not v:
        return default
    if v.startswith("ansi_"):
        return v
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
    try:
        from son_of_anton_cli.skin_engine import get_active_skin

        return _to_ansi(get_active_skin().get_color(key, ""), default)
    except Exception:
        return default


def _rich_color(value: str) -> str:
    """Map a Textual ansi_<name> token onto the rich basic colour of the same name."""
    token = _to_ansi(value, "")
    return token[5:] if token.startswith("ansi_") else token


def _polarity(colors: Any = None) -> str:
    """Terminal polarity: from the queried background when we have one.

    The OSC answer is authoritative; ``COLORFGBG`` is the fallback guess for
    terminals that don't reply, and dark is the last resort.
    """
    if colors is not None:
        from son_of_anton_tui.palette import polarity as _p

        return _p(colors.background)
    fgbg = os.environ.get("COLORFGBG", "")
    if fgbg:
        try:
            parts = fgbg.split(";")
            if len(parts) >= 2:
                return "light" if int(parts[-1]) >= 8 else "dark"
        except (ValueError, IndexError):
            pass
    return "dark"


def _theme_name(colors: Any = None) -> str:
    return f"ansi-{_polarity(colors)}"


def _wordmark_for_width(avail: int) -> str:
    """The largest wordmark that fits ``avail`` usable columns.

    ``avail`` is the transcript's real content width, scrollbar already
    deducted; the block letters must never wrap, so anything that doesn't fit
    falls back to the next smaller form and finally to plain text.
    """
    try:
        from son_of_anton_cli.banner import (
            SON_OF_ANTON_AGENT_LOGO_STACKED as _STACKED,
            SON_OF_ANTON_AGENT_LOGO_WIDE as _WIDE,
        )
    except Exception:
        return "SON OF ANTON"
    if avail >= 109:
        return _WIDE
    if avail >= 52:
        return _STACKED
    return "SON OF ANTON"


def _short_path(path: str) -> str:
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


def _provider_label(provider: dict) -> str:
    """Display name for a picker provider row.

    ``build_models_payload`` gives ``name`` ("custom") and ``slug``
    ("custom:custom"); the slug is an addressing detail, so it is the last
    resort.  One helper so the row and the title can't disagree.
    """
    p = provider or {}
    return str(p.get("label") or p.get("name") or p.get("slug") or "?")


def _compaction_stats_line(stats: dict) -> str:
    """The dim line under a compaction summary (same wording as the classic CLI)."""
    if isinstance(stats.get("before_tokens"), int) and not isinstance(stats.get("after_tokens"), int):
        return f"~{stats['before_tokens']:,} tokens before"
    try:
        from cli import _format_compaction_stats

        return _format_compaction_stats(stats)
    except Exception:
        return ""


def _fmt_tokens(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


class PromptHistory:
    """What ↑ / ↓ walk through at the prompt.

    Recall used to come free from prompt_toolkit's ``FileHistory``; deleting
    that REPL took it with it.  So this reads and appends the very same file
    (``~/.son-of-anton/.son_of_anton_history``) in the very same shape — a
    ``# <timestamp>`` header, then each line of the entry prefixed with ``+``
    — and every prompt typed before the move is still there under the arrow.

    ``path`` may be ``None`` (no backend attached, or an unwritable home):
    recall still works, it just lives and dies with the session.
    """

    MAX_ENTRIES = 500
    _MAX_FILE_BYTES = 512 * 1024

    def __init__(self, path: Any = None) -> None:
        self._path: Optional[Path] = Path(path) if path else None
        self._entries: list[str] = []
        self._index: Optional[int] = None  # None → showing the live draft
        self._draft = ""
        self._load()

    # ---------------- persistence ----------------
    def _load(self) -> None:
        if self._path is None:
            return
        try:
            # The file is append-only and never pruned on disk, so a
            # long-lived install reads its whole life history at every start.
            # Only the tail matters: a partial first line is simply skipped.
            with self._path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - self._MAX_FILE_BYTES))
                raw = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return
        entry: list[str] = []
        for line in raw.splitlines():
            if line.startswith("+"):
                entry.append(line[1:])
            elif entry:
                self._entries.append("\n".join(entry))
                entry = []
        if entry:
            self._entries.append("\n".join(entry))
        self._trim()

    def _trim(self) -> None:
        del self._entries[: -self.MAX_ENTRIES]

    def _append(self, text: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        body = "".join(f"+{line}\n" for line in text.split("\n"))
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n# {stamp}\n{body}")
        except OSError:
            self._path = None  # read-only home: keep recall in memory only

    # ---------------- recall ----------------
    def record(self, text: str) -> None:
        """Remember a submitted prompt and drop back to the live draft."""
        self.reset()
        text = text.strip("\n")
        if not text.strip():
            return
        # Consecutive duplicates collapse: pressing ↑ after sending the same
        # thing twice should reach the entry *before* it, not itself again.
        if not self._entries or self._entries[-1] != text:
            self._entries.append(text)
            self._trim()
            if self._path is not None:
                self._append(text)

    def reset(self) -> None:
        """Forget where in the history we are (called on submit)."""
        self._index = None
        self._draft = ""

    def prev(self, current: str) -> Optional[str]:
        """The entry before the one showing, or ``None`` at the oldest."""
        if not self._entries:
            return None
        if self._index is None:
            self._draft = current
            self._index = len(self._entries) - 1
        elif self._index == 0:
            return None
        else:
            self._index -= 1
        return self._entries[self._index]

    def next(self, current: str) -> Optional[str]:
        """The entry after the one showing — past the newest, the saved draft."""
        if self._index is None:
            return None
        self._index += 1
        if self._index >= len(self._entries):
            self._index = None
            return self._draft
        return self._entries[self._index]

    def suggest(self, prefix: str) -> str:
        """The rest of the newest one-line entry that extends ``prefix`` ('' if none)."""
        if not prefix:
            return ""
        for entry in reversed(self._entries):
            if len(entry) > len(prefix) and "\n" not in entry and entry.startswith(prefix):
                return entry[len(prefix):]
        return ""


def is_available() -> bool:
    """True when Textual is installed (the ``tui`` extra is present)."""
    return _TEXTUAL_AVAILABLE


if _TEXTUAL_AVAILABLE:
    from rich.text import Text

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    class TuiEvent(Message):
        """One backend event (posted from any thread — ``post_message`` is thread-safe)."""

        def __init__(self, kind: str, payload: dict) -> None:
            super().__init__()
            self.kind = kind
            self.payload = payload

    class _AppSink:
        def __init__(self, app: "SonOfAntonTUIApp") -> None:
            self._app = app

        def emit(self, kind: str, **payload: Any) -> None:
            self._app.post_message(TuiEvent(kind, payload))

        def run_with_terminal(self, fn: Any) -> Any:
            """Hand the terminal to ``fn``, from whichever thread asks.

            Suspending has to happen on the app's own thread; worker threads
            (where slash commands run) hop over via ``call_from_thread``, which
            blocks them until the child is done — exactly the semantics the
            caller wants.
            """
            app = self._app
            import threading

            if threading.get_ident() == getattr(app, "_thread_id", None):
                return app.run_suspended(fn)
            return app.call_from_thread(app.run_suspended, fn)

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------
    class PromptArea(TextArea):
        """Multi-line prompt: Enter submits, Shift+Enter newlines, Tab completes.

        ↑ / ↓ move the cursor through a multi-line draft and fall through to
        history recall once they run off the top or the bottom of it — the
        shell behaviour the prompt_toolkit REPL used to give for free.  So is
        the ghost text: the rest of the newest matching history entry, taken
        with →.

        Tab never leaves the prompt.  TextArea's default ``tab_behavior`` moves
        focus on to the transcript, which swallowed every key typed after a
        Tab that had nothing to complete.
        """

        BINDINGS = [
            # Terminals normally swallow ctrl+shift+v themselves and hand the
            # app a bracketed paste, which ``TextArea._on_paste`` already
            # inserts.  This covers the ones that forward the chord instead,
            # so the shortcut works either way.
            Binding("ctrl+shift+v", "paste", "Paste", show=False),
        ]

        class Submitted(Message):
            def __init__(self, prompt: "PromptArea", value: str) -> None:
                super().__init__()
                self.prompt = prompt
                self.value = value

        class Complete(Message):
            """Tab, or Enter / Up / Down / Escape while the completion list is showing."""

            def __init__(self, action: str) -> None:
                super().__init__()
                self.action = action

        completer_active: bool = False
        prompt_history: Optional[PromptHistory] = None
        # Set while ↑/↓ swap in a history entry: the ``Changed`` that follows
        # must not pop the slash-completion list open, or the next ↑ would
        # walk that list instead of the history.
        recalled: bool = False

        async def _on_key(self, event: events.Key) -> None:
            key = event.key or ""
            if key == "enter":
                event.stop()
                event.prevent_default()
                if self.completer_active:
                    self.post_message(self.Complete("accept-submit"))
                    return
                self.post_message(self.Submitted(self, self.text.rstrip("\n")))
                return
            if key.endswith("+enter"):
                event.stop()
                event.prevent_default()
                self.insert("\n")
                return
            if key == "tab":
                event.stop()
                event.prevent_default()
                self.post_message(self.Complete("accept" if self.completer_active else "tab"))
                return
            if self.completer_active and key in ("up", "down", "escape"):
                event.stop()
                event.prevent_default()
                self.post_message(self.Complete({"up": "up", "down": "down", "escape": "close"}[key]))
                return
            if key in ("up", "down") and self._recall(key):
                event.stop()
                event.prevent_default()
                return
            await super()._on_key(event)

        def update_suggestion(self) -> None:
            self.suggestion = self._history_suggestion()

        def _history_suggestion(self) -> str:
            # Only at the very end of a one-line draft, and never over an open
            # completion list (→ would take the ghost, Enter the list).
            if self.prompt_history is None or self.completer_active:
                return ""
            text = self.text
            if not text.strip() or "\n" in text or self.cursor_location != self.document.end:
                return ""
            return self.prompt_history.suggest(text)

        def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
            # TextArea draws the ghost at the cursor, wherever it is: drop it
            # as soon as the cursor leaves the end.
            self.update_suggestion()

        def _recall(self, key: str) -> bool:
            """Swap the draft for a history entry; False leaves the key alone.

            Inside a multi-line draft the arrows still have to move the cursor,
            so recall only fires from the top row (↑) or the bottom row (↓) —
            and only while there is somewhere left to go.
            """
            if self.prompt_history is None:
                return False
            row, _col = self.cursor_location
            if key == "up":
                if row != 0:
                    return False
                entry = self.prompt_history.prev(self.text)
            else:
                if row != self.document.line_count - 1:
                    return False
                entry = self.prompt_history.next(self.text)
            if entry is None:
                return False
            self.recalled = True
            self.text = entry
            self.move_cursor(self.document.end)
            return True

    # ------------------------------------------------------------------
    # Modals
    # ------------------------------------------------------------------
    def _detail_widgets(detail: Any):
        """Normalise a modal's ``detail`` into widgets (renderable, widget, or a list)."""
        if not detail:
            return
        items = detail if isinstance(detail, (list, tuple)) else [detail]
        for item in items:
            if isinstance(item, Static):
                yield item
            else:
                yield Static(item, classes="dialog-detail", markup=False)

    class ChoiceModal(ModalScreen[Optional[str]]):
        """Pick one of ``choices`` — ``(key, label, description)`` triples.

        Number keys pick directly, ↑/↓ + Enter confirm, Esc cancels (``None``).
        With ``filterable`` a text box narrows long lists (model pickers).
        """

        BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

        def __init__(
            self,
            title: str,
            choices: list,
            detail: Any = "",
            *,
            filterable: bool = False,
            deadline: Optional[float] = None,
            default: int = 0,
            accent: str = "primary",
        ) -> None:
            super().__init__()
            self._title = title
            self._choices = [tuple(c) if isinstance(c, (tuple, list)) else (str(c), str(c), "") for c in choices]
            self._detail = detail
            self._filterable = filterable
            self._deadline = deadline
            self._default = default
            self._accent = accent
            self._visible: list = []

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog", classes=f"accent-{self._accent}"):
                yield Static(self._title, classes="dialog-title", markup=False)
                yield from _detail_widgets(self._detail)
                if self._filterable:
                    yield Input(placeholder="type to filter…", id="filter")
                yield OptionList(id="choices")
                yield Static("", classes="dialog-hint")

        def on_mount(self) -> None:
            self._fill("")
            self.query_one(".dialog-hint", Static).update(self._hint())
            if self._filterable:
                self.query_one("#filter", Input).focus()
            else:
                self.query_one("#choices", OptionList).focus()
            if self._deadline:
                self.set_interval(1.0, self._tick)
                self._tick()

        def _hint(self) -> str:
            lead = "type to filter · " if self._filterable else "1-9 pick · "
            return f"{lead}↑↓ move · enter pick · esc cancel"

        def _tick(self) -> None:
            if not self._deadline:
                return
            remaining = max(0, int(self._deadline - time.monotonic()))
            self.query_one(".dialog-hint", Static).update(
                f"{self._hint()} · auto-resolves in {remaining}s"
            )

        def _fill(self, query: str) -> None:
            ol = self.query_one("#choices", OptionList)
            ol.clear_options()
            q = query.strip().lower()
            self._visible = []
            for idx, choice in enumerate(self._choices):
                key, label = choice[0], choice[1]
                desc = choice[2] if len(choice) > 2 else ""
                if q and q not in label.lower() and q not in str(key).lower():
                    continue
                self._visible.append(key)
                n = len(self._visible)
                row = Text()
                if not self._filterable:
                    row.append(f"{n} " if n <= 9 else "  ", style="dim")
                row.append(label, style="bold" if n - 1 == self._default else "")
                if desc:
                    row.append("   ")
                    row.append(desc, style="dim")
                row.no_wrap = True
                row.overflow = "ellipsis"
                ol.add_option(Option(row, id=f"opt-{idx}"))
            if self._visible:
                ol.highlighted = min(self._default, len(self._visible) - 1) if not q else 0

        @on(Input.Changed, "#filter")
        def _filter_changed(self, event: Input.Changed) -> None:
            self._fill(event.value)

        @on(Input.Submitted, "#filter")
        def _filter_submit(self) -> None:
            ol = self.query_one("#choices", OptionList)
            if ol.highlighted is not None and 0 <= ol.highlighted < len(self._visible):
                self.dismiss(self._visible[ol.highlighted])

        @on(OptionList.OptionSelected, "#choices")
        def _selected(self, event: OptionList.OptionSelected) -> None:
            if 0 <= event.option_index < len(self._visible):
                self.dismiss(self._visible[event.option_index])

        def on_key(self, event: events.Key) -> None:
            if self._filterable and self.query_one("#filter", Input).has_focus:
                if event.key in ("up", "down"):
                    ol = self.query_one("#choices", OptionList)
                    ol.action_cursor_up() if event.key == "up" else ol.action_cursor_down()
                    event.stop()
                return
            if event.character and event.character.isdigit():
                n = int(event.character)
                if 1 <= n <= len(self._visible):
                    event.stop()
                    self.dismiss(self._visible[n - 1])

        def action_cancel(self) -> None:
            self.dismiss(None)

    class MultiChoiceModal(ModalScreen[Optional[list]]):
        """Check any number of ``(key, label)`` rows; Enter confirms, Esc cancels."""

        BINDINGS = [
            Binding("escape", "cancel", "Cancel", show=False),
            Binding("enter", "confirm", "Confirm", show=False, priority=True),
        ]

        def __init__(self, title: str, choices: list, detail: Any = "", *, deadline: Optional[float] = None) -> None:
            super().__init__()
            self._title = title
            self._choices = choices
            self._detail = detail
            self._deadline = deadline

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Static(self._title, classes="dialog-title", markup=False)
                yield from _detail_widgets(self._detail)
                yield SelectionList(*[(label, key) for key, label in self._choices], id="choices")
                yield Static("space toggle · enter confirm · esc cancel", classes="dialog-hint")

        def on_mount(self) -> None:
            self.query_one("#choices", SelectionList).focus()

        def action_confirm(self) -> None:
            self.dismiss(list(self.query_one("#choices", SelectionList).selected))

        def action_cancel(self) -> None:
            self.dismiss(None)

    class TextModal(ModalScreen[Optional[str]]):
        """One line of text (optionally masked).  Enter submits, Esc cancels."""

        BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

        def __init__(self, title: str, prompt: str = "", *, password: bool = False, prefill: str = "", detail: Any = "") -> None:
            super().__init__()
            self._title = title
            self._prompt = prompt
            self._password = password
            self._prefill = prefill
            self._detail = detail

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Static(self._title, classes="dialog-title", markup=False)
                yield from _detail_widgets(self._detail)
                yield Input(value=self._prefill, placeholder=self._prompt, password=self._password, id="text")
                yield Static("enter submit · esc " + ("skip" if self._password else "cancel"), classes="dialog-hint")

        def on_mount(self) -> None:
            self.query_one("#text", Input).focus()

        @on(Input.Submitted, "#text")
        def _submit(self, event: Input.Submitted) -> None:
            self.dismiss(event.value)

        def action_cancel(self) -> None:
            self.dismiss(None)

    # ------------------------------------------------------------------
    # Command palette provider (ctrl+p)
    # ------------------------------------------------------------------
    class SlashProvider(Provider):
        """Fuzzy-search the slash-command registry; picking one prefills the prompt."""

        def _entries(self) -> list:
            app = self.app
            return app._palette_entries() if isinstance(app, SonOfAntonTUIApp) else []

        async def discover(self) -> Hits:
            app = self.app
            for cmd, category, desc in self._entries()[:24]:
                yield DiscoveryHit(cmd, partial(app._prefill_prompt, cmd), help=f"{category} · {desc}" if category else desc)

        async def search(self, query: str) -> Hits:
            matcher = self.matcher(query)
            app = self.app
            for cmd, category, desc in self._entries():
                score = matcher.match(cmd)
                if score <= 0:
                    score = matcher.match(f"{cmd} {desc}") * 0.5
                if score > 0:
                    yield Hit(
                        score,
                        matcher.highlight(cmd),
                        partial(app._prefill_prompt, cmd),
                        help=f"{category} · {desc}" if category else desc,
                    )

    # ------------------------------------------------------------------
    # The app
    # ------------------------------------------------------------------
    class SonOfAntonTUIApp(App):
        """opencode-inspired frame: transcript, prompt dock, right context panel."""

        TITLE = _DEFAULT_AGENT
        COMMANDS = {SlashProvider}
        ENABLE_COMMAND_PALETTE = True

        # Layout mirrors opencode's session route (packages/tui/src/routes/session):
        # a row of [main column | 42-col sidebar]; the main column carries the
        # horizontal padding (so transcript and prompt share one left edge) and
        # holds the scrolling transcript above a fixed dock. Surfaces follow
        # opencode too: `$panel` / `$surface` are generated from the terminal's
        # own background (see palette.py), and `$background` is never painted so
        # terminal transparency survives. With no answer from the terminal those
        # variables stay transparent and the rails carry the separation alone.
        CSS = """
        Screen { background: $background; layers: base overlay; }
        Horizontal#split { width: 1fr; height: 1fr; }
        Vertical#content { width: 1fr; padding: 0 2 1 2; }

        /* The transcript is the virtual feed: a Line API widget with no
           children, so there is nothing to style inside it — blocks carry
           their own rich styles (see son_of_anton_tui/feed.py). */
        #feed {
            width: 1fr; height: 1fr; margin-bottom: 1; overflow-x: hidden;
            scrollbar-size-vertical: 1;
            scrollbar-color: $text-muted; scrollbar-color-hover: $primary; scrollbar-color-active: $primary;
            scrollbar-background: transparent;
        }

        /* The dock: completion popup, the prompt block, then the status row. */
        #dock { height: auto; }
        #completer {
            height: auto; max-height: 9; display: none;
            background: $surface; border: none; padding: 0 1 0 3; margin-bottom: 1;
            scrollbar-size-vertical: 1;
        }
        #completer > .option-list--option-highlighted { background: transparent; color: $primary; text-style: bold; }

        /* Follow-ups typed while a turn is running sit here until they are
           sent, directly above the prompt they were typed in. */
        #queued-strip {
            height: auto; display: none; margin-bottom: 1; padding: 0 1 0 3;
            color: $text-muted;
        }

        /* opencode Prompt: a left rail, the textarea, then a meta row, the whole
           block sitting on the element surface. */
        #prompt-frame {
            height: auto; border-left: wide $primary;
            padding: 1 2 1 2; background: $surface;
        }
        #prompt-frame PromptArea {
            width: 1fr; height: auto; min-height: 1; max-height: 10;
            padding: 0; border: none; background: $surface;
        }
        #prompt-frame PromptArea:focus { border: none; background: $surface; }
        #prompt-meta { height: 1; margin-top: 1; color: $text-muted; }
        #prompt-meta-left { width: 1fr; color: $text-muted; }
        #prompt-meta-right { width: auto; color: $text-muted; }

        /* The status row must grow, not clip: the live action (e.g. the
           "waiting on <model> — 42s with no output yet …" heartbeat) is a
           single long Static, and a fixed height:1 clipped it once the
           window narrowed. Auto height lets it wrap to a second line
           instead of losing the tail. */
        #statusline { height: auto; margin-top: 1; }
        #status-left { width: 1fr; color: $text-muted; text-wrap: wrap; text-overflow: fold; }
        #status-left.busy { color: $text; }
        #status-right { width: auto; color: $text-muted; }

        /* opencode Sidebar: 42 wide, its own padding, product line pinned low,
           on the panel surface. */
        Vertical#context { width: 42; padding: 1 2; border-left: solid $primary; background: $panel; }
        Vertical#context.overlay { layer: overlay; dock: right; height: 100%; }
        #context-scroll { height: 1fr; scrollbar-size-vertical: 1; scrollbar-background: transparent; }
        #context .label { color: $text-muted; margin-top: 1; }
        #context .kv { color: $text; }
        #context .value { color: $primary; }
        #context .muted { color: $text-muted; }
        #context #ctx-title { color: $text; text-style: bold; }
        #context #ctx-bar { color: $primary; }
        #context-footer { height: auto; color: $text-muted; }

        ModalScreen { align: center middle; background: $background 60%; }
        #dialog {
            width: 76; max-width: 96%; height: auto; max-height: 90%;
            border: round $primary; background: $panel; padding: 1 2;
        }
        #dialog.accent-warning { border: round $warning; }
        #dialog.accent-error { border: round $error; }
        #dialog.accent-secondary { border: round $secondary; }
        .dialog-title { text-style: bold; margin-bottom: 1; }
        .dialog-detail { color: $text-muted; margin-bottom: 1; }
        .dialog-hint { color: $text-muted; margin-top: 1; }
        #dialog OptionList { height: auto; max-height: 14; background: transparent; border: none; padding: 0; }
        #dialog OptionList > .option-list--option-highlighted { background: transparent; color: $primary; text-style: bold; }
        #dialog SelectionList { height: auto; max-height: 14; background: transparent; border: none; padding: 0; }
        #dialog Input { border: tall $primary; background: transparent; }
        #dialog Input:focus { border: tall $primary; }
        #dialog .command { border-left: outer $warning; padding: 0 1; margin-bottom: 1; }
        """

        BINDINGS = [
            Binding("ctrl+c", "interrupt_or_quit", "Interrupt / quit", priority=True, show=False),
            Binding("ctrl+q", "quit", "Quit", priority=True, show=False),
            Binding("ctrl+l", "clear_feed", "Clear", show=False),
            Binding("ctrl+b", "toggle_sidebar", "Panel", show=False),
            Binding("shift+tab", "cycle_permission_mode", "Permissions", show=False, priority=True),
            Binding("ctrl+g", "edit_in_editor", "Editor", show=False, priority=True),
            Binding("ctrl+p", "command_palette", "Commands", show=False, priority=True),
            Binding("pageup", "feed_page_up", show=False, priority=True),
            Binding("pagedown", "feed_page_down", show=False, priority=True),
            Binding("escape", "escape", show=False),
        ]

        def __init__(
            self,
            backend: Any = None,
            agent_name: str = _DEFAULT_AGENT,
            terminal_colors: Any = None,
            **kwargs: Any,
        ) -> None:
            self.backend = backend
            self.agent_name = agent_name
            # Generated surfaces, opencode-style: the terminal's own background
            # nudged toward its opposite. Absent (no TTY, terminal stayed quiet)
            # every surface stays transparent and rails carry the separation.
            self.terminal_colors = terminal_colors
            self._surfaces: dict[str, str] = {}
            if terminal_colors is not None:
                try:
                    from son_of_anton_tui.palette import build_palette

                    self._surfaces = build_palette(terminal_colors)
                except Exception:
                    self._surfaces = {}
            self._theme = _theme_name(terminal_colors)
            self._transcript = ""  # concatenated assistant text (tests / recap)
            self._status = "ready"
            self._busy: Optional[str] = None
            self._sidebar_forced: Optional[bool] = None
            self._md_block: Optional[Any] = None
            self._reasoning: Optional[Any] = None
            self._tool_gen_line: Optional[Any] = None
            self._tool_lines: dict[str, list[Any]] = {}
            self._running_tools: list[Any] = []
            self._refresh_scheduled = False
            self._modal_open = False
            self._serviced: dict[str, Any] = {}  # attr -> the state object already shown
            self._spin = 0
            # The list on show, the text before the cursor it was built for,
            # and whether Tab opened it (then it keeps following the typing
            # even for the Tab-only kinds, like paths).
            self._completions: Optional[_completion.Completions] = None
            self._completion_before = ""
            self._completion_explicit = False
            self._project_files = _completion.ProjectFiles(on_update=self._project_files_loaded)
            # The daemon queue's texts (``queue.updated``), oldest first.
            self._queued_items: list[str] = []
            # Approval and question requests waiting for (or showing) a modal.
            self._prompts: list[dict] = []
            # The request whose modal is being opened, stamped on its screen.
            self._opening_owner: Optional[dict] = None
            # Blocking backend calls run here, in order, off the event loop.
            import queue as _queue

            self._jobs: _queue.Queue = _queue.Queue()
            self._job_thread: Optional[Any] = None
            self._last_ctrl_c = 0.0
            self._snap_cache: Optional[tuple] = None
            self._log_handlers: list[tuple[logging.Handler, Any]] = []
            super().__init__(**kwargs)
            self.theme = self._theme

        # ---------------- theme ----------------
        def get_css_variables(self) -> dict[str, str]:
            """Skin accents plus the surfaces generated from the terminal.

            ``background`` is never overridden: like opencode's system theme it
            stays transparent so terminal transparency survives, while panel and
            surface come from the generated ramp.
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
            vars_.update(self._surfaces)
            return vars_

        # ---------------- layout ----------------
        def compose(self) -> ComposeResult:
            with Horizontal(id="split"):
                with Vertical(id="content"):
                    yield VirtualFeed(self._feed_style(), wordmark_art=_wordmark_for_width)
                    with Vertical(id="dock"):
                        yield OptionList(id="completer")
                        yield Static("", id="queued-strip")
                        with Vertical(id="prompt-frame"):
                            yield PromptArea(
                                placeholder="Message Son of Anton…",
                                id="input",
                            )
                            with Horizontal(id="prompt-meta"):
                                yield Static("", id="prompt-meta-left")
                                yield Static("", id="prompt-meta-right")
                        with Horizontal(id="statusline"):
                            yield Static("", id="status-left")
                            yield Static("", id="status-right")
                yield from self._compose_sidebar()

        def _compose_sidebar(self) -> ComposeResult:
            """opencode's sidebar: identity on top, detail below, product pinned low."""
            with Vertical(id="context"):
                with VerticalScroll(id="context-scroll"):
                    # Titles, paths and mode names are not ours to parse:
                    # markup=False keeps a "[" in any of them from raising.
                    yield Static("", id="ctx-title", markup=False)
                    yield Static("", id="ctx-session-id", classes="muted")
                    yield Static("", id="ctx-cwd", classes="muted", markup=False)
                    yield Static("mode", classes="label")
                    yield Static("", id="ctx-mode", classes="kv", markup=False)
                    yield Static("reasoning", classes="label")
                    yield Static("", id="ctx-reasoning", classes="kv muted", markup=False)
                    yield Static("context", classes="label")
                    yield Static("", id="ctx-bar", classes="kv")
                    yield Static("", id="ctx-tokens", classes="kv muted")
                    yield Static("session usage", classes="label")
                    yield Static("", id="ctx-usage", classes="kv muted")
                    yield Static("background", id="ctx-bg-label", classes="label")
                    yield Static("", id="ctx-bg", classes="kv muted")
                    yield Static("", id="ctx-elapsed", classes="kv muted")
                yield Static(self._product_line(), id="context-footer")

        def _product_line(self) -> Any:
            try:
                from son_of_anton_cli import __version__ as version
            except Exception:
                version = ""
            line = Text()
            line.append("• ", style="green")
            line.append("Son of ", style="bold")
            line.append("Anton", style="bold")
            if version:
                line.append(f" {version}")
            return line

        def on_mount(self) -> None:
            self._feed = self.query_one("#feed", VirtualFeed)
            self._prompt = self.query_one("#input", PromptArea)
            self._queued_strip = self.query_one("#queued-strip", Static)
            self._prompt.prompt_history = PromptHistory(getattr(self.backend, "_history_file", None))
            self._completer = self.query_one("#completer", OptionList)
            self._status_left = self.query_one("#status-left", Static)
            self._status_right = self.query_one("#status-right", Static)
            self._panel = self.query_one("#context", Vertical)
            self._prompt.focus()
            self._apply_sidebar(self.size.width)
            self._show_intro()
            self._feed.anchor()
            self.begin_capture_print(self, stdout=True, stderr=True)
            self._stdout_buf = ""
            self._stderr_buf = ""
            self._redirect_log_handlers()
            if self.backend is not None:
                self.backend.attach(_AppSink(self), feed_width=note_width(self._feed_inner_width()))
            self.set_interval(0.12, self._tick_spinner)
            self.set_interval(1.0, self._tick_chrome)
            self._refresh_chrome()

        def on_unmount(self) -> None:
            self._restore_log_handlers()
            try:
                self.end_capture_print(self)
            except Exception:
                pass
            if self.backend is not None:
                self.backend.detach()

        def on_resize(self, event: Any) -> None:
            self._apply_sidebar(event.size.width)

        @on(VirtualFeed.Resized)
        def _feed_resized(self, event: VirtualFeed.Resized) -> None:
            # The backend console wraps the classic CLI output that lands in
            # indented note blocks. Measuring here, not from the app's resize:
            # that fires before layout, so the backend lagged one resize behind.
            if self.backend is not None:
                self.backend.set_feed_width(note_width(event.width))

        def _wide(self, width: Optional[int] = None) -> bool:
            return (self.size.width if width is None else width) > SIDEBAR_THRESHOLD

        def _sidebar_visible(self, width: Optional[int] = None) -> bool:
            """opencode: shown when explicitly opened, else only on a wide terminal."""
            if self._sidebar_forced is not None:
                return self._sidebar_forced
            return self._wide(width)

        def _cwd(self) -> str:
            return str(getattr(self.backend, "cwd", "") or os.getcwd())

        def _feed_width(self) -> int:
            # An overlaid sidebar floats above the column, so it takes no width.
            inline = self._sidebar_visible() and self._wide()
            return self.size.width - (SIDEBAR_WIDTH if inline else 0)

        def _feed_inner_width(self) -> int:
            """Columns actually available to transcript content.

            Measured from the mounted widget rather than derived, so padding
            changes can't drift, and one column is held back for the vertical
            scrollbar while it is hidden: it appears the moment the transcript
            overflows, and art sized to the pre-scrollbar width would wrap into
            rubble exactly then.
            """
            feed = getattr(self, "_feed", None)
            if feed is not None and feed.is_mounted:
                width = feed.scrollable_content_region.width
                if width > 0:
                    return max(0, width - (0 if feed.show_vertical_scrollbar else 1))
            return max(0, self._feed_width() - 2 * FEED_PADDING - 1)

        def _apply_sidebar(self, width: int) -> None:
            """Inline beside the transcript when wide; floating over it when narrow.

            Mirrors opencode, which keeps the panel reachable on a narrow terminal
            by drawing it over the content instead of dropping it.
            """
            try:
                visible = self._sidebar_visible(width)
                self._panel.display = visible
                self._panel.set_class(visible and not self._wide(width), "overlay")
            except Exception:
                pass

        def _feed_style(self) -> FeedStyle:
            """Transcript rich-style tokens, resolved from the active skin."""
            return FeedStyle(
                primary=_rich_color(_skin_accent("ui_accent", "ansi_yellow")),
                secondary=_rich_color(_skin_accent("ui_thinking", "ansi_cyan")),
                tool=_rich_color(_skin_accent("ui_tool", "ansi_yellow")),
                error=_rich_color(_skin_accent("ui_error", "ansi_red")),
                panel=self._surfaces.get("panel", ""),
            )

        def _show_intro(self) -> None:
            parts = []
            try:
                from son_of_anton_cli import __version__ as _v

                parts.append(f"v{_v}")
            except Exception:
                pass
            if self.backend is not None:
                snap = self.backend.status_snapshot()
                model = snap.get("model_short") or ""
                if model:
                    parts.append(model)
                sid = getattr(self.backend, "session_id", "") or ""
                if sid:
                    parts.append(f"session {sid}")
            parts.append(_short_path(self._cwd()))
            text = Text("  ·  ".join(parts), style="dim")
            text.append("\n/help for commands · ctrl+p for the palette · :q to quit", style="dim")
            try:
                self._feed.set_intro(text)
            except Exception:
                pass

        # ---------------- print / log capture ----------------
        def on_print(self, event: events.Print) -> None:
            if event.stderr:
                self._stderr_buf += event.text
                while "\n" in self._stderr_buf:
                    line, self._stderr_buf = self._stderr_buf.split("\n", 1)
                    self._note(line, muted=True)
            else:
                self._stdout_buf += event.text
                while "\n" in self._stdout_buf:
                    line, self._stdout_buf = self._stdout_buf.split("\n", 1)
                    self._note(line)

        def _redirect_log_handlers(self) -> None:
            """Point stderr log handlers at the captured stream so warnings can't paint over the UI."""
            try:
                for handler in list(logging.getLogger().handlers):
                    if isinstance(handler, logging.StreamHandler) and not isinstance(
                        handler, logging.FileHandler
                    ):
                        self._log_handlers.append((handler, handler.stream))
                        handler.setStream(sys.stderr)
            except Exception:
                pass

        def _restore_log_handlers(self) -> None:
            for handler, stream in self._log_handlers:
                try:
                    handler.setStream(stream)
                except Exception:
                    pass
            self._log_handlers = []

        # ---------------- feed helpers ----------------
        def _reset_note_block(self) -> None:
            self._feed.reset_note()

        def _note(self, text: str, *, muted: bool = False) -> None:
            """Append one ANSI/plain line, merging consecutive lines into a block."""
            if not text.strip():
                return
            rich = Text.from_ansi(text.rstrip())
            if muted:
                rich.stylize("dim")
            self._feed.add_note(rich)

        def _add_user_turn(self, text: str) -> None:
            self._feed.add_user(text)
            self._transcript_scroll()

        def _transcript_scroll(self) -> None:
            self._feed.scroll_end(animate=False)
            self._feed.anchor()

        # ---------------- backend events ----------------
        async def on_tui_event(self, event: TuiEvent) -> None:
            kind, p = event.kind, event.payload
            handler = getattr(self, f"_ev_{kind}", None)
            if handler is None:
                return
            result = handler(**p)
            if asyncio.iscoroutine(result):
                await result

        def _ev_refresh(self) -> None:
            self._schedule_refresh()

        def _ev_restyle(self) -> None:
            """/skin changed the accents — re-resolve CSS and transcript styles."""
            try:
                self.refresh_css()
            except Exception:
                pass
            try:
                self._feed.restyle(self._feed_style())
            except Exception:
                pass
            self._schedule_refresh()

        def _ev_ansi(self, text: str = "", stderr: bool = False) -> None:
            self._note(text, muted=stderr)

        def _ev_rich(self, renderable: Any = None) -> None:
            if renderable is None:
                return
            if isinstance(renderable, Text) and not renderable.plain.strip():
                return
            self._reset_note_block()
            self._feed.add_rich(renderable)

        async def _ev_compaction(self, text: str = "", stats: Optional[dict] = None) -> None:
            """opencode's compaction entry: a titled rule, then what the model now remembers."""
            from rich.rule import Rule

            if not (text or "").strip():
                return
            self._close_assistant()
            self._reset_note_block()
            self._feed.add_rich(Rule(title="Compaction", style="dim"))
            self._feed.add_markdown(text)
            line = _compaction_stats_line(stats or {})
            if line:
                self._feed.add_rich(Text(line, style="dim"))
            self._transcript_scroll()

        def _ev_assistant_start(self) -> None:
            self._close_assistant()
            self._md_block = self._feed.open_markdown()

        def _ev_assistant_delta(self, text: str = "") -> None:
            if not text:
                return
            if self._md_block is None:
                self._ev_assistant_start()
            self._transcript += text
            self._md_block.append(text)

        def _ev_assistant_end(self) -> None:
            self._close_assistant()

        def _close_assistant(self) -> None:
            self._md_block = None

        def _ev_reasoning_start(self) -> None:
            self._finish_reasoning()
            self._reasoning = self._feed.open_reasoning()

        def _ev_reasoning_delta(self, text: str = "") -> None:
            if self._reasoning is None:
                self._ev_reasoning_start()
            self._reasoning.append(text)

        def _ev_reasoning_end(self) -> None:
            self._finish_reasoning()

        def _finish_reasoning(self) -> None:
            block, self._reasoning = self._reasoning, None
            if block is not None:
                block.finish()
                self._feed.changed(block)

        def _ev_tool_gen(self, name: str = "") -> None:
            line = self._feed.open_tool(name)
            line.frame = _SPINNER[self._spin]
            line.dirty = True
            self._tool_gen_line = line

        def _ev_tool_start(
            self, name: str = "", label: str = "", hidden: bool = False, call_id: str = ""
        ) -> None:
            line = self._tool_gen_line
            self._tool_gen_line = None
            if hidden:
                if line is not None:
                    self._feed.remove_block(line)
                return
            if line is None:
                line = self._feed.open_tool(name)
            line.label = label or name
            line.icon = _tool_icon(name)
            line.dirty = True
            self._tool_lines.setdefault(call_id or name, []).append(line)
            self._running_tools.append(line)

        def _ev_tool_done(
            self,
            name: str = "",
            label: str = "",
            line: str = "",
            duration: float = 0.0,
            is_error: bool = False,
            hidden: bool = False,
            call_id: str = "",
        ) -> None:
            key = call_id or name
            rows = self._tool_lines.get(key)
            row = rows.pop(0) if rows else None
            if rows is not None and not rows:
                self._tool_lines.pop(key, None)
            if row is not None and row in self._running_tools:
                self._running_tools.remove(row)
            if hidden:
                if row is not None:
                    self._feed.remove_block(row)
                return
            if row is None:
                if not (label or line):
                    return
                row = self._feed.open_tool(name)
            row.finish(_tool_icon(name), label or name, is_error, duration)
            self._feed.changed(row)

        # ---- events from the daemon backend ----
        def _ev_user(self, text: str = "") -> None:
            self._close_assistant()
            self._finish_reasoning()
            self._add_user_turn(text)

        def _ev_markdown(self, text: str = "") -> None:
            if text.strip():
                self._feed.add_markdown(text)

        def _ev_reasoning_block(self, text: str = "") -> None:
            block = self._feed.open_reasoning()
            block.append(text)
            block.finish()
            self._feed.changed(block)

        def _ev_status(self, running: bool = False) -> None:
            self._busy = "turn" if running else None
            self._snap_cache = None
            self._update_status()

        def _ev_queue(self, items: Optional[list] = None) -> None:
            self._queued_items = [str(i) for i in (items or [])]
            self._update_status()

        def _ev_approval(self, approval_id: str = "", subject: str = "", reasons: Optional[list] = None,
                         choices: Optional[list] = None, approval_kind: str = "") -> None:
            self._add_prompt("approval", approval_id,
                             {"kind": approval_kind, "subject": subject, "reasons": reasons or [], "choices": choices or []})

        def _ev_approval_resolved(self, approval_id: str = "") -> None:
            self._resolve_prompt("approval", approval_id)

        def _ev_question(self, question_id: str = "", text: str = "", choices: Optional[list] = None,
                         multi_select: bool = False) -> None:
            self._add_prompt("question", question_id,
                             {"text": text, "choices": choices or [], "multi_select": multi_select})

        def _ev_question_resolved(self, question_id: str = "") -> None:
            self._resolve_prompt("question", question_id)

        def _ev_prompts_clear(self) -> None:
            entries, self._prompts = self._prompts, []
            for entry in entries:
                self._dismiss_modal_of(entry)

        def _clear_feed(self) -> None:
            self._feed.clear()
            self._transcript = ""
            self._md_block = None
            self._reasoning = None
            self._tool_gen_line = None
            self._tool_lines.clear()
            self._running_tools.clear()

        def _ev_clear(self) -> None:
            self._clear_feed()

        def _ev_pick(self, purpose: str = "", title: str = "", rows: Optional[list] = None,
                     default: int = 0, filterable: bool = False) -> None:
            self._open_pick(purpose, title, [tuple(r) for r in rows or []], default, filterable)

        def _ev_pick_many(self, purpose: str = "", title: str = "", rows: Optional[list] = None) -> None:
            self._open_pick_many(purpose, title, [tuple(r) for r in rows or []])

        def _ev_turn_end(self) -> None:
            self._close_assistant()
            self._finish_reasoning()
            if self._tool_gen_line is not None:
                self._feed.remove_block(self._tool_gen_line)
                self._tool_gen_line = None
            for row in self._running_tools:
                if not row.done:
                    row.finish(row.icon, row.label, False, time.monotonic() - row.started)
                    self._feed.changed(row)
            self._running_tools.clear()
            self._tool_lines.clear()
            self._reset_note_block()
            self._feed.flush()

        # ---------------- chrome ----------------
        def _schedule_refresh(self) -> None:
            if self._refresh_scheduled:
                return
            self._refresh_scheduled = True
            self.set_timer(0.08, self._refresh_chrome)

        def _tick_spinner(self) -> None:
            self._spin = (self._spin + 1) % len(_SPINNER)
            for row in self._running_tools:
                row.frame = _SPINNER[self._spin]
                row.dirty = True
            if self._tool_gen_line is not None:
                self._tool_gen_line.frame = _SPINNER[self._spin]
                self._tool_gen_line.dirty = True
            self._feed.flush()
            if self._busy:
                self._update_status()

        def _tick_chrome(self) -> None:
            self._refresh_chrome()
            if self.backend is not None:
                self.backend.idle_tick()

        def _refresh_chrome(self) -> None:
            self._refresh_scheduled = False
            self._update_status()
            self._update_context()
            self._service_modals()

        def _update_status(self) -> None:
            """The prompt meta row and the status row beneath it.

            opencode splits these: identity (agent · model provider) sits inside
            the prompt block, while the row below carries the working directory
            when idle and the live action when busy, with usage and the shortcut
            hints right-aligned.
            """
            backend = self.backend
            frame = _SPINNER[self._spin]

            # --- the row below the prompt --------------------------------
            if self._busy == "turn":
                action = (backend.status_action() or "").strip() if backend else ""
                started = backend.prompt_start_time() if backend else None
                left = Text.assemble((f"{frame} ", "bold"), action or "thinking")
                if started:
                    left.append(f"  {time.time() - started:.0f}s", style="dim")
                self._status_left.add_class("busy")
            elif self._busy:
                left = Text.assemble((f"{frame} ", "bold"), f"running {self._busy}")
                self._status_left.add_class("busy")
            else:
                left = Text(_short_path(self._cwd()))
                self._status_left.remove_class("busy")
            if self._queued_items:
                left.append(f"  · {len(self._queued_items)} queued", style="dim")
            self._status_left.update(left)
            self._update_queued_strip()

            right = Text()
            if self._busy:
                right.append("ctrl+c", style="bold")
                right.append(" interrupt")
            else:
                snap = self._snapshot()
                pct = snap.get("context_percent")
                if pct is not None:
                    right.append(f"{pct}% context")
                    right.append("  ·  ")
                if snap.get("focus_label"):
                    right.append(f"{snap['focus_label']}  ·  ")
                right.append("ctrl+p", style="bold")
                right.append(" commands")
            self._status_right.update(right)

            # --- the meta row inside the prompt block ---------------------
            meta = Text()
            if backend is not None:
                snap = self._snapshot()
                # The mode readout: the session permission mode, always shown
                # (default | ask | lockdown | yolo), with the warning styling
                # for the modes that change what the agent may do unasked.
                perm = backend.permission_mode()
                meta.append(perm, style={"yolo": "bold red", "lockdown": "yellow"}.get(perm, ""))
                model = snap.get("model_short") or ""
                if model:
                    meta.append("  ·  ", style="dim")
                    meta.append(model, style="bold")
                    provider = (
                        getattr(backend, "provider", None)
                        or getattr(backend, "requested_provider", None)
                        or ""
                    )
                    if provider:
                        meta.append(f"  {provider}", style="dim")
                effort = snap.get("reasoning_label") or "medium"
                meta.append("  ·  ", style="dim")
                meta.append(f"reasoning {effort}", style="dim")
            try:
                self.query_one("#prompt-meta-left", Static).update(meta)
                self.query_one("#prompt-meta-right", Static).update(
                    Text(f"{len(self._attached())} attached") if self._attached() else Text()
                )
            except Exception:
                pass

        def _update_queued_strip(self) -> None:
            """Render the follow-ups parked above the prompt, oldest first.

            Only the first three messages are previewed; the hint advertises
            the commands that redirect the queue into the live turn.
            """
            strip = getattr(self, "_queued_strip", None)
            if strip is None:
                return
            items = self._queued_items
            if not items:
                strip.update("")
                strip.display = False
                return
            room = max(20, self._feed_width() - 6)
            body = Text()
            shown = items[:3]
            for index, item in enumerate(shown):
                preview = " ".join(str(item).split())
                if len(preview) > room:
                    preview = preview[: room - 1] + "…"
                if index:
                    body.append("\n")
                body.append("↳ ", style="dim")
                body.append(preview)
            hidden = len(items) - len(shown)
            if hidden:
                body.append("\n")
                body.append(f"   +{hidden} more", style="dim")
            body.append("\n")
            if len(items) > 1:
                hint = "/sendnow sends the oldest · /sendall sends all"
            else:
                hint = "/sendnow sends this now"
            body.append(hint + " · /cancel drops", style="dim")
            strip.update(body)
            strip.display = True

        def _attached(self) -> list:
            return []

        def _snapshot(self) -> dict:
            """The backend status snapshot, reused for a short window.

            ``status_snapshot`` builds a ~30-key dict and the chrome paths call
            it up to three times per refresh (status row, meta row, sidebar).
            Token counts only move per API call, so caching the build for a few
            hundred milliseconds removes the redundant work invisibly.
            """
            cached = self._snap_cache
            now = time.monotonic()
            if cached is not None and now - cached[0] < 0.3:
                return cached[1]
            snap = self.backend.status_snapshot() if self.backend is not None else {}
            self._snap_cache = (now, snap)
            return snap

        def _update_context(self) -> None:
            if not self._panel.display:
                return
            backend = self.backend
            cwd = _short_path(self._cwd())
            if backend is None:
                self.query_one("#ctx-title", Static).update(self.agent_name)
                self.query_one("#ctx-cwd", Static).update(cwd)
                return
            snap = self._snapshot()
            self.query_one("#ctx-title", Static).update(snap.get("session_title") or "untitled")
            self.query_one("#ctx-session-id", Static).update(str(getattr(backend, "session_id", "") or ""))
            self.query_one("#ctx-cwd", Static).update(cwd)
            self.query_one("#ctx-mode", Static).update(backend.permission_mode())
            self.query_one("#ctx-reasoning", Static).update(
                snap.get("reasoning_label") or "medium (default)"
            )
            pct = snap.get("context_percent")
            if pct is None:
                self.query_one("#ctx-bar", Static).update(Text("▱▱▱▱▱▱▱▱▱▱  —", style="dim"))
                self.query_one("#ctx-tokens", Static).update("no turns yet")
            else:
                filled = max(0, min(10, round(pct / 10)))
                self.query_one("#ctx-bar", Static).update(f"{'▰' * filled}{'▱' * (10 - filled)}  {pct}%")
                self.query_one("#ctx-tokens", Static).update(
                    f"{_fmt_tokens(snap.get('context_tokens', 0))} / {_fmt_tokens(snap.get('context_length') or 0)}"
                    + (f" · {snap['compactions']} compacted" if snap.get("compactions") else "")
                )
            self.query_one("#ctx-usage", Static).update(
                f"in {_fmt_tokens(snap.get('session_input_tokens', 0))} · out {_fmt_tokens(snap.get('session_output_tokens', 0))}"
                f" · {snap.get('session_api_calls', 0)} calls"
            )
            bg = []
            if snap.get("active_background_tasks"):
                bg.append(f"{snap['active_background_tasks']} tasks")
            if snap.get("active_background_processes"):
                bg.append(f"{snap['active_background_processes']} processes")
            if snap.get("active_background_subagents"):
                bg.append(f"{snap['active_background_subagents']} subagents")
            bg_text = " · ".join(bg)
            bg_label = self.query_one("#ctx-bg-label", Static)
            bg_value = self.query_one("#ctx-bg", Static)
            bg_value.update(bg_text)
            # Nothing running is not "idle" — hide the row rather than imply
            # the whole session is parked while the user is mid-conversation.
            want_row = bool(bg_text)
            if bg_label.display != want_row:
                bg_label.display = want_row
                bg_value.display = want_row
            self.query_one("#ctx-elapsed", Static).update(f"session {snap.get('duration', '')}".rstrip())

        # ---------------- modal servicing ----------------
        def _add_prompt(self, kind: str, prompt_id: str, data: dict) -> None:
            if any(e["id"] == prompt_id and e["kind"] == kind for e in self._prompts):
                return
            self._prompts.append({"kind": kind, "id": prompt_id, "data": data, "shown": False})
            self._service_modals()

        def _resolve_prompt(self, kind: str, prompt_id: str) -> None:
            """Answered elsewhere (or the turn was interrupted): drop it and its modal."""
            for entry in list(self._prompts):
                if entry["kind"] == kind and entry["id"] == prompt_id:
                    self._prompts.remove(entry)
                    self._dismiss_modal_of(entry)
            self._service_modals()

        def _dismiss_modal_of(self, entry: dict) -> None:
            # The callbacks answer only a prompt still in ``_prompts``, so
            # dismissing a screen for a removed entry answers nothing.
            screen = self.screen
            if getattr(screen, "_soa_owner", None) is entry:
                try:
                    screen.dismiss(None)
                except Exception:
                    pass

        def _service_modals(self) -> None:
            """Show a modal for whichever daemon request is waiting (one at a time)."""
            if self._modal_open:
                return
            for entry in self._prompts:
                if entry["shown"]:
                    continue
                entry["shown"] = True
                self._modal_open = True
                self._opening_owner = entry
                try:
                    if entry["kind"] == "approval":
                        self._open_approval(entry)
                    else:
                        self._open_question(entry)
                finally:
                    self._opening_owner = None
                return

        def _push(self, screen: Any, callback: Any) -> None:
            # A modal opened from another modal's answer (question → "Other…")
            # answers the same request.
            screen._soa_owner = self._opening_owner

            def _done(result: Any) -> None:
                self._modal_open = False
                self._opening_owner = getattr(screen, "_soa_owner", None)
                try:
                    callback(result)
                finally:
                    self._opening_owner = None
                    self._prompt.focus()
                    self._schedule_refresh()
                    self._service_modals()

            self.push_screen(screen, _done)

        def _open_approval(self, entry: dict) -> None:
            data = entry["data"]
            labels = {
                "once": ("Allow once", "run this now"),
                "session": ("Allow for this session", "no more prompts for this until exit"),
                "always": ("Always allow", "add to the allowlist"),
                "deny": ("Deny", "the agent is told it was refused"),
            }
            memory = data.get("kind") == "memory"
            if memory:
                labels = {"once": ("Save", "write it to memory"), "deny": ("Don't save", "the agent is told it was refused")}
            choices = [(k, *labels[k]) for k in data.get("choices", []) if k in labels]
            detail = Text.assemble((str(data.get("subject", "")), "bold"))
            reasons = "; ".join(str(r) for r in data.get("reasons") or [])
            if reasons:
                detail.append("\n" + reasons, style="dim")
            screen = ChoiceModal("Save to memory?" if memory else "The agent wants to run a command", choices, detail, accent="warning")

            def _answer(result: Any) -> None:
                if entry in self._prompts:
                    self._prompts.remove(entry)
                    self._bg(partial(self.backend.answer_approval, entry["id"], result or "deny"))

            self._push(screen, _answer)

        def _open_question(self, entry: dict) -> None:
            data = entry["data"]
            question = str(data.get("text", ""))
            title = "Son of Anton needs your input"
            choices = list(data.get("choices") or [])
            multi = bool(data.get("multi_select"))

            def _lock(answers: list) -> None:
                if entry in self._prompts:
                    self._prompts.remove(entry)
                    self._bg(partial(self.backend.answer_question, entry["id"], answers))

            dismissed = ["The user dismissed the question without answering; use your best judgement."]

            def _freetext(base: Optional[list] = None) -> None:
                self._modal_open = True

                def _typed(text: Any) -> None:
                    text = str(text or "").strip()
                    if not text:
                        _lock(list(base) if base else dismissed)
                        return
                    _lock(list(base or []) + [text] if multi else [text])

                self._push(TextModal(title, "your answer", detail=Text(question)), _typed)

            if not choices:
                self._modal_open = False
                _freetext()
                return

            if multi:
                rows = [(str(i), c) for i, c in enumerate(choices)] + [("__other__", "Other…")]

                def _picked(result: Any) -> None:
                    if result is None:
                        _lock(dismissed)
                        return
                    keys = list(result)
                    picked = [choices[int(k)] for k in keys if k != "__other__"]
                    if "__other__" in keys:
                        _freetext(base=picked)
                        return
                    _lock(picked)

                self._push(MultiChoiceModal(title, rows, Text(question)), _picked)
                return

            rows = [(str(i), c, "") for i, c in enumerate(choices)] + [("__other__", "Other…", "type your own answer")]

            def _chosen(result: Any) -> None:
                if result is None:
                    _lock(dismissed)
                elif result == "__other__":
                    _freetext()
                else:
                    _lock([choices[int(result)]])

            self._push(ChoiceModal(title, rows, Text(question)), _chosen)

        def _open_pick(self, purpose: str, title: str, rows: list, default: int, filterable: bool) -> None:
            def _picked(result: Any) -> None:
                if result is not None:
                    self._bg(partial(self.backend.picked, purpose, str(result)))

            self._modal_open = True
            self._push(ChoiceModal(title, rows, filterable=filterable, default=default), _picked)

        def _open_pick_many(self, purpose: str, title: str, rows: list) -> None:
            def _picked(result: Any) -> None:
                if result:
                    self._bg(partial(self.backend.picked_many, purpose, [str(k) for k in result]))

            self._modal_open = True
            self._push(MultiChoiceModal(title, [(k, Text(label)) for k, label in rows]), _picked)

        # ---------------- input ----------------
        @on(PromptArea.Submitted)
        async def _handle_submit(self, event: PromptArea.Submitted) -> None:
            value = (event.value or "").strip()
            self._hide_completer()
            history = self._prompt.prompt_history
            self._prompt.text = ""
            if not value:
                if history is not None:
                    history.reset()
                return
            if history is not None:
                history.record(value)
            await self._submit_text(value)

        def _bg(self, fn: Any) -> None:
            """Run a blocking backend call off the event loop, in submission order."""
            self._jobs.put(fn)
            if self._job_thread is None:
                import threading

                self._job_thread = threading.Thread(target=self._job_loop, name="tui-jobs", daemon=True)
                self._job_thread.start()

        def _job_loop(self) -> None:
            while True:
                fn = self._jobs.get()
                try:
                    fn()
                except Exception:
                    logging.getLogger(__name__).debug("backend call failed", exc_info=True)

        def _run_command(self, value: str) -> None:
            keep_going = self.backend.run_slash(value)
            if keep_going is False:
                self.call_from_thread(self.exit)

        async def _submit_text(self, value: str) -> None:
            first_parts = value.split(None, 1)
            head = first_parts[0].lower() if first_parts else ""
            if head[:1] in ("/", ":"):
                from son_of_anton_cli.commands import resolve_command as _resolve_command

                _cmd = _resolve_command(head)
                if _cmd is not None and _cmd.name == "quit":
                    self.exit()
                    return

            if self.backend is None:
                self._add_user_turn(value)
                self._note("(no agent attached — this frame is running standalone)", muted=True)
                return

            if value.startswith("!"):
                self._reset_note_block()
                self._feed.add_rich(Text(value, style="dim"))
                self._transcript_scroll()
                self.run_worker(
                    partial(self.backend.run_bang, value),
                    thread=True, exit_on_error=False, group="shell", exclusive=False,
                )
                return

            from son_of_anton_tui.backend import looks_like_command

            if looks_like_command(value):
                self._reset_note_block()
                self._feed.add_rich(Text(f"⚙ {value}", style="dim"))
                self._transcript_scroll()
                self._bg(partial(self._run_command, value))
                return
            # Everything else goes straight to the daemon, which starts the
            # turn or queues the message.
            self._bg(partial(self.backend.send, value))

        async def _run_in_worker(self, label: str, fn: Any) -> Any:
            """Run a blocking call on a thread. The editor shows as busy while
            it holds the terminal; ``_busy == "turn"`` is the session's status."""
            if label == "editor":
                self._busy = label
                self._update_status()
            worker = self.run_worker(fn, thread=True, exit_on_error=False, group="agent", exclusive=False)
            try:
                return await worker.wait()
            except Exception as exc:  # WorkerFailed
                self._note(f"error: {getattr(exc, 'error', exc)}", muted=True)
                return None
            finally:
                if label == "editor":
                    running = getattr(self.backend, "session", {}).get("status") == "running"
                    self._busy = "turn" if running else None
                    self._update_status()

        # ---------------- prompt completion ----------------
        # What completes is son_of_anton_tui/completion.py; this half maps
        # the cursor to offsets, draws the list and applies the choice.
        def _palette_entries(self) -> list:
            if self.backend is not None:
                return self.backend.palette_entries()
            try:
                from son_of_anton_cli.commands import COMMANDS_BY_CATEGORY

                return [(cmd, cat, desc) for cat, cmds in COMMANDS_BY_CATEGORY.items() for cmd, desc in cmds.items()]
            except Exception:
                return []

        def _prefill_prompt(self, cmd: str) -> None:
            self._prompt.text = cmd + " "
            self._prompt.move_cursor(self._prompt.document.end)
            self._prompt.focus()

        @on(TextArea.Changed, "#input")
        def _prompt_changed(self, event: TextArea.Changed) -> None:
            if self._prompt.recalled:
                self._prompt.recalled = False
                self._hide_completer()
                return
            self._refresh_completer(explicit=self._completion_explicit)

        def _text_before_cursor(self) -> str:
            return self._prompt.get_text_range((0, 0), self._prompt.cursor_location)

        def _completions_for(self, before: str, *, explicit: bool) -> Optional[_completion.Completions]:
            try:
                return _completion.complete(
                    before,
                    explicit=explicit,
                    commands=self._palette_entries(),
                    cwd=os.getcwd(),
                    project_files=self._project_files,
                )
            except Exception:
                logging.getLogger(__name__).debug("prompt completion failed", exc_info=True)
                return None

        def _refresh_completer(self, *, explicit: bool) -> None:
            before = self._text_before_cursor()
            comps = self._completions_for(before, explicit=explicit)
            if comps is None:
                self._hide_completer()
            else:
                self._show_completer(comps, before)

        def _project_files_loaded(self, cwd: str) -> None:
            """Called off-thread when the ``@`` file list lands: redraw an open ``@`` list."""

            def _redraw() -> None:
                before = self._text_before_cursor()
                word = before.split()[-1] if before and not before[-1].isspace() else ""
                if word.startswith("@"):
                    self._refresh_completer(explicit=self._completion_explicit)

            try:
                self.call_from_thread(_redraw)
            except Exception:
                pass  # the app is gone or not running

        def _show_completer(self, comps: "_completion.Completions", before: str) -> None:
            self._completions = comps
            self._completion_before = before
            items = comps.items
            self._completer.clear_options()
            col = min(max(len(c.display) for c in items) + 2, 48)
            room = max(20, self._feed_width() - col - 4)
            for c in items:
                d = c.meta
                if len(d) > room:
                    d = d[: room - 1] + "…"
                row = Text.assemble((c.display.ljust(col), "bold"), (d, "dim"))
                row.no_wrap = True
                row.overflow = "ellipsis"
                self._completer.add_option(Option(row))
            self._completer.highlighted = 0
            self._completer.display = True
            self._prompt.completer_active = True
            self._prompt.suggestion = ""

        def _hide_completer(self) -> None:
            self._completer.display = False
            self._prompt.completer_active = False
            self._completions = None
            self._completion_before = ""
            self._completion_explicit = False
            self._prompt.update_suggestion()

        def _accept_completion(self, comps: "_completion.Completions", cand: "_completion.Candidate", before: str) -> None:
            prompt = self._prompt
            head = before[: comps.start]
            start = (head.count("\n"), len(head) - (head.rfind("\n") + 1))
            end = prompt.cursor_location
            insert = _completion.insertion(cand, prompt.get_text_range(end, prompt.document.end))
            # Hidden first: the edit's Changed re-opens the list where it
            # should (inside an @file: directory), and only there.
            self._hide_completer()
            prompt.replace(insert, start, end, maintain_selection_offset=False)

        def _tab_complete(self) -> None:
            """Tab with no list: complete like a shell — one match fills in,
            several extend to their common prefix and open the list."""
            before = self._text_before_cursor()
            comps = self._completions_for(before, explicit=True)
            if comps is None:
                return
            if len(comps.items) == 1:
                self._accept_completion(comps, comps.items[0], before)
                return
            self._completion_explicit = True
            word = before[comps.start :]
            common = os.path.commonprefix([c.insert for c in comps.items])
            if len(common) > len(word):
                head = before[: comps.start]
                start = (head.count("\n"), len(head) - (head.rfind("\n") + 1))
                # The Changed this edit posts redraws the (narrower) list.
                self._prompt.replace(common, start, self._prompt.cursor_location, maintain_selection_offset=False)
                return
            self._show_completer(comps, before)

        async def _submit_prompt(self) -> None:
            await self._handle_submit(PromptArea.Submitted(self._prompt, self._prompt.text.rstrip("\n")))

        @on(PromptArea.Complete)
        async def _complete(self, event: PromptArea.Complete) -> None:
            action = event.action
            if action == "tab":
                self._tab_complete()
                return
            if action == "close":
                self._hide_completer()
                return
            if action in ("up", "down"):
                if action == "up":
                    self._completer.action_cursor_up()
                else:
                    self._completer.action_cursor_down()
                return
            await self._accept_highlighted(self._completer.highlighted or 0, submit=action == "accept-submit")

        async def _accept_highlighted(self, idx: int, *, submit: bool = False) -> None:
            before = self._text_before_cursor()
            comps = self._completions
            if comps is None or before != self._completion_before:
                # The cursor moved since the list was drawn: rebuild for here.
                comps, idx = self._completions_for(before, explicit=True), 0
            if comps is None or not 0 <= idx < len(comps.items):
                self._hide_completer()
                if submit:
                    await self._submit_prompt()
                return
            cand = comps.items[idx]
            if submit and cand.insert.rstrip() == before[comps.start :].rstrip():
                # Enter on what is already typed in full: send it.
                self._hide_completer()
                await self._submit_prompt()
                return
            self._accept_completion(comps, cand, before)

        @on(OptionList.OptionSelected, "#completer")
        async def _completer_clicked(self, event: OptionList.OptionSelected) -> None:
            await self._accept_highlighted(event.option_index)
            self._prompt.focus()

        # ---------------- selection / clipboard ----------------
        def on_mouse_up(self, event: events.MouseUp) -> None:
            """A click in the transcript must never cost you the prompt.

            Textual focuses whatever focusable widget you press on, so a click
            — or a drag to select — anywhere in the feed left the caret
            stranded and you had to click back into the prompt before you
            could type again. The selection lives on the screen rather than on
            the focus, so handing focus straight back keeps the highlight and
            the caret both.
            """
            if self.screen is not self.screen_stack[0]:
                return  # a modal owns the input while it is up
            prompt = getattr(self, "_prompt", None)
            if prompt is not None and self.focused is not prompt:
                prompt.focus()

        def on_text_selected(self, event: events.TextSelected) -> None:
            """Native Textual selection (sidebar, dialogs) auto-copies.

            The transcript itself selects through the virtual feed, which posts
            ``VirtualFeed.TextCopied`` instead — a Line API widget has no
            children for Textual's selection machinery to walk.
            """
            try:
                selected = self.screen.get_selected_text()
            except Exception:
                return
            if not selected or not selected.strip():
                return
            self.copy_to_clipboard(selected)
            self.notify(f"copied {len(selected.strip())} chars", timeout=1.5)

        @on(VirtualFeed.TextCopied)
        def _copy_selection(self, event: VirtualFeed.TextCopied) -> None:
            """The virtual feed's drag-selection released with content: copy it.

            Mirroring what terminals do out of the box, the selection lands on
            the system clipboard (``copy_to_clipboard`` writes OSC 52) with a
            short toast. A plain click yields no selection, so no message.
            """
            try:
                self.copy_to_clipboard(event.text)
            except Exception:
                pass
            self.notify(f"copied {len(event.text.strip())} chars", timeout=1.5)

        # ---------------- actions ----------------
        def action_interrupt_or_quit(self) -> None:
            now = time.monotonic()
            if self._busy == "turn" and self.backend is not None:
                if now - self._last_ctrl_c < 2.0:
                    self.backend.interrupt_turn()
                    self.exit()
                    return
                self._last_ctrl_c = now
                self._note("interrupting… (ctrl+c again to force quit)", muted=True)
                self.backend.interrupt_turn()
                return
            if self._prompt.text:
                self._prompt.text = ""
                self._hide_completer()
                return
            self.exit()

        def action_escape(self) -> None:
            if self._completer.display:
                self._hide_completer()
            elif self._busy == "turn" and self.backend is not None:
                self._note("interrupting…", muted=True)
                self.backend.interrupt_turn()

        def action_clear_feed(self) -> None:
            self._clear_feed()

        def run_suspended(self, fn: Any) -> Any:
            """Run ``fn`` with the app suspended and the terminal restored."""
            try:
                with self.suspend():
                    return fn()
            except SuspendNotSupported:
                # Headless / unsupported driver: nothing owns the screen anyway.
                return fn()

        def action_edit_in_editor(self) -> None:
            """ctrl+g — compose the current draft in $EDITOR, then send it."""
            if self.backend is None:
                return
            draft = self._prompt.text
            self._prompt.text = ""
            self._hide_completer()

            async def _compose() -> None:
                composed = await self._run_in_worker(
                    "editor", partial(self.backend._compose_in_editor, draft)
                )
                if composed:
                    await self._submit_text(composed)
                elif draft:
                    # Editor left nothing: give the draft back rather than eat it.
                    self._prompt.text = draft

            self.run_worker(_compose(), group="editor")

        def action_cycle_permission_mode(self) -> None:
            """shift+tab: step the session's permission mode."""
            if self.backend is None:
                return
            self.run_worker(
                self.backend.cycle_permission_mode, thread=True, exit_on_error=False,
                group="permissions", exclusive=False,
            )

        def action_toggle_sidebar(self) -> None:
            self._sidebar_forced = not self._sidebar_visible()
            self._apply_sidebar(self.size.width)
            self._update_context()

        def action_feed_page_up(self) -> None:
            self._feed.scroll_page_up()

        def action_feed_page_down(self) -> None:
            self._feed.scroll_page_down()

else:
    SonOfAntonTUIApp = None  # type: ignore
    PromptArea = None  # type: ignore


def run_app(backend: Any) -> None:
    """Launch the Textual front-end around a started ``DaemonBackend``."""
    if not _TEXTUAL_AVAILABLE or SonOfAntonTUIApp is None:
        from son_of_anton_constants import missing_dependency_message

        print(missing_dependency_message("Textual (a core dependency)"))
        return
    # Ask the terminal for its colours BEFORE Textual takes the tty: the reply
    # is what lets the app generate panel surfaces that still follow the theme.
    from son_of_anton_tui.palette import query_terminal_colors

    app = SonOfAntonTUIApp(backend=backend, terminal_colors=query_terminal_colors())
    try:
        app.run()
    finally:
        backend.finish()


def _run_demo() -> None:
    """Standalone frame with no agent (``python -m son_of_anton_tui.tui``)."""
    if not _TEXTUAL_AVAILABLE:
        from son_of_anton_constants import missing_dependency_message

        print(missing_dependency_message("Textual (a core dependency)"))
        return
    SonOfAntonTUIApp().run()


if __name__ == "__main__":
    _run_demo()
