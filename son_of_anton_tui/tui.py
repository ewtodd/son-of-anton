"""Son of Anton — the Textual front-end.

A real application frame (opencode-inspired) instead of an input line plus
scrollback:

  * the transcript + prompt fill the main column on the LEFT;
  * a 42-col context panel sits on the RIGHT on wide terminals (> 120 cols);
  * a multi-line prompt: Enter submits, Shift+Enter inserts a newline;
  * slash commands autocomplete inline and ctrl+p opens a fuzzy palette;
  * the ASCII "SON OF ANTON" wordmark opens the session.

The agent is the unchanged ``cli.SonOfAntonCLI`` loop, wrapped by
``son_of_anton_tui.backend.TextualBackend``: it runs ``chat()`` and slash
commands on worker threads and emits typed events (assistant tokens,
reasoning, tool lifecycle, ANSI lines, refresh) that this app renders.  Modal
prompts (approval, clarify, sudo, secret, destructive-command confirm, model
picker) are the backend's own ``_*_state`` dicts, watched here and answered
through their ``response_queue`` — the same contract prompt_toolkit used.

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
    from textual.content import Content
    from textual.message import Message
    from textual.screen import ModalScreen
    from textual.widgets import (
        Collapsible,
        Input,
        Markdown,
        OptionList,
        SelectionList,
        Static,
        TextArea,
    )
    from textual.widgets.option_list import Option

    _TEXTUAL_AVAILABLE = True
except Exception:  # pragma: no cover - import-time guard
    App = None  # type: ignore
    ComposeResult = None  # type: ignore
    SuspendNotSupported = Exception  # type: ignore
    events = on = None  # type: ignore
    Binding = None  # type: ignore
    DiscoveryHit = Hit = Hits = Provider = None  # type: ignore
    Container = Horizontal = Vertical = VerticalScroll = None  # type: ignore
    Content = None  # type: ignore
    Message = None  # type: ignore
    ModalScreen = None  # type: ignore
    Collapsible = Input = Markdown = OptionList = SelectionList = Static = TextArea = None  # type: ignore
    Option = None  # type: ignore
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
TOOL_ICON_WIDTH = 2
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
    "skill_view": "→",
    "skills_list": "→",
    "skill_manage": "←",
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
    try:
        from son_of_anton_cli.skin_engine import get_active_skin

        return _to_ansi(get_active_skin().get_color(key, ""), default)
    except Exception:
        return default


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
            raw = self._path.read_text(encoding="utf-8", errors="replace")
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


def is_available() -> bool:
    """True when Textual is installed (the ``tui`` extra is present)."""
    return _TEXTUAL_AVAILABLE


if _TEXTUAL_AVAILABLE:
    from rich.text import Text
    from textual.widgets._markdown import MarkdownFence  # type: ignore

    class _PlainFence(MarkdownFence):
        """A fenced code block rendered as plain text (no syntax highlighting)."""

        @classmethod
        def highlight(cls, code: str, language: str, ansi: bool = False, dark: bool = False) -> Content:
            return Content(code)

    class PlainMarkdown(Markdown):
        """Markdown that renders fenced code without syntax highlighting."""

        BLOCKS = {**Markdown.BLOCKS, "fence": _PlainFence, "code_block": _PlainFence}

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
        shell behaviour the prompt_toolkit REPL used to give for free.
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
            """Tab / Up / Down / Escape while the completion list is showing."""

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
            if self.completer_active and key in ("tab", "up", "down", "escape"):
                event.stop()
                event.prevent_default()
                self.post_message(self.Complete({"tab": "accept", "up": "up", "down": "down", "escape": "close"}[key]))
                return
            if key in ("up", "down") and self._recall(key):
                event.stop()
                event.prevent_default()
                return
            await super()._on_key(event)

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
    # Feed widgets
    # ------------------------------------------------------------------
    class Wordmark(Static):
        """The ASCII wordmark, sized from the width it is actually given.

        Choosing the variant from outside is unreliable: an app-level resize
        handler runs before the new layout exists, so it sees the old width and
        can leave a 109-column form in a 108-column feed, which wraps into
        rubble. The widget's own resize fires after layout with its real width,
        scrollbar already deducted.
        """

        def __init__(self, **kw: Any) -> None:
            super().__init__("", **kw)
            self._art_width = -1

        def on_resize(self, event: events.Resize) -> None:
            self.fit(event.size.width)

        def fit(self, width: int) -> None:
            if width <= 0 or width == self._art_width:
                return
            self._art_width = width
            lines = _wordmark_for_width(width).splitlines()
            if not lines:
                self.update("")
                return
            pad = max(0, (width - max(len(line) for line in lines)) // 2)
            self.update("\n".join(f"{' ' * pad}{line}" for line in lines))

    class UserTurn(Static):
        """The user's message: an accent rail, bold text."""

    class NoteLine(Static):
        """Chrome / command output / ANSI lines from the classic CLI."""

        def __init__(self, renderable: Any = "", *, muted: bool = False, **kw: Any) -> None:
            super().__init__(renderable, **kw)
            self.text: Optional[Text] = renderable if isinstance(renderable, Text) else None
            self.line_count = 1
            if muted:
                self.add_class("muted")

        def append_line(self, line: Text) -> bool:
            """Fold another line into this block (only for Text-backed blocks)."""
            if self.text is None:
                return False
            merged = self.text.copy()
            merged.append("\n")
            merged.append_text(line)
            self.text = merged
            self.line_count += 1
            self.update(merged)
            return True

    class ToolLine(Static):
        """One tool call, in opencode's inline shape.

        A two-cell icon column then the label: a spinner sits in that column
        while the call runs and is replaced by the tool's glyph when it lands,
        so a row never reflows between the two states.
        """

        def __init__(self, label: str, icon: str = "⚙", **kw: Any) -> None:
            super().__init__("", **kw)
            self.label = label
            self.icon = icon
            self.started = time.monotonic()
            self.done = False
            self.add_class("running")

        def _row(self, lead: str, label: str, trailing: str = "") -> Text:
            row = Text()
            row.append(lead.ljust(TOOL_ICON_WIDTH))
            row.append(label)
            if trailing:
                row.append(trailing, style="dim")
            row.no_wrap = True
            row.overflow = "ellipsis"
            return row

        def render_running(self, frame: str) -> None:
            elapsed = time.monotonic() - self.started
            self.update(self._row(frame, self.label, f"  {elapsed:.0f}s" if elapsed >= 1 else ""))

        def finish(self, label: str, is_error: bool, duration: float = 0.0) -> None:
            self.done = True
            self.remove_class("running")
            if is_error:
                self.add_class("error")
            self.update(
                self._row(
                    "✗" if is_error else self.icon,
                    label or self.label,
                    f"  {duration:.1f}s" if duration else "",
                )
            )

    class ReasoningBlock(Collapsible):
        """Model reasoning: expanded while it streams, folded once the answer starts."""

        def __init__(self, **kw: Any) -> None:
            # markup=False: reasoning is the model's prose, and Textual's
            # content markup would try to parse any "[" in it — a stray
            # bracket used to take the whole app down with a MarkupError.
            self._body = Static("", classes="reasoning-body", markup=False)
            self._buffer = ""
            self._dirty = False
            super().__init__(self._body, title="reasoning", collapsed=False, **kw)

        def append(self, text: str) -> None:
            self._buffer += text
            self._dirty = True

        def flush(self) -> None:
            if self._dirty:
                self._dirty = False
                self._body.update(self._buffer.strip())

        def finish(self) -> None:
            self.flush()
            lines = len(self._buffer.strip().splitlines())
            self.title = f"reasoning · {lines} line{'s' if lines != 1 else ''}"
            self.collapsed = True

    # Blocks that always earn a blank line before whatever follows them —
    # opencode keeps the same set in `alwaysSeparate`.
    _ALWAYS_SEPARATE = (UserTurn, PlainMarkdown, ReasoningBlock)

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

        VerticalScroll#feed {
            width: 1fr; height: 1fr; margin-bottom: 1;
            scrollbar-size-vertical: 1;
            scrollbar-color: $text-muted; scrollbar-color-hover: $primary; scrollbar-color-active: $primary;
            scrollbar-background: transparent;
        }
        #feed .wordmark {
            text-style: bold; color: $primary; margin: 1 0 0 0; width: 1fr;
            /* block letters must clip, never wrap: a wrapped line is rubble */
            text-wrap: nowrap; overflow-x: hidden;
        }
        #feed .intro { color: $text-muted; margin: 0 0 1 0; padding: 0 0 0 3; }

        /* opencode UserMessage: left rail in the agent colour, padding 1/0/1/2,
           filled with the generated panel surface. */
        #feed UserTurn { margin-top: 1; padding: 1 0 1 2; border-left: wide $primary; background: $panel; }
        /* opencode indents transcript text to column 3; inline tool rows keep
           their icon in the two columns left of it. */
        #feed NoteLine { padding: 0 0 0 3; }
        #feed NoteLine.muted { color: $text-muted; }
        #feed NoteLine.command { color: $text-muted; margin-top: 1; }
        /* opencode InlineTool: a 2-cell icon column, then the label. */
        #feed ToolLine { color: $text-muted; }
        #feed ToolLine.running { color: $text; }
        #feed ToolLine.error { color: $error; }

        #feed PlainMarkdown { background: transparent; margin: 1 0 0 0; padding: 0 0 0 3; }
        #feed MarkdownH1, #feed MarkdownH2, #feed MarkdownH3, #feed MarkdownH4 {
            background: transparent; color: $primary; text-style: bold; content-align: left middle; margin: 1 0 0 0;
        }
        #feed MarkdownFence { background: transparent; color: $text; border-left: outer $text-muted 40%; padding: 0 1; margin: 1 0; }
        #feed MarkdownBlockQuote { background: transparent; border-left: outer $secondary; }
        #feed MarkdownHorizontalRule { border-bottom: solid $text-muted; }

        #feed ReasoningBlock { background: transparent; border: none; padding: 0; margin: 1 0 0 0; }
        #feed ReasoningBlock > CollapsibleTitle { color: $secondary; background: transparent; padding: 0 0 0 3; }
        #feed ReasoningBlock > CollapsibleTitle:hover { background: transparent; text-style: bold; }
        #feed ReasoningBlock > CollapsibleTitle:focus { background: transparent; text-style: bold; }
        #feed ReasoningBlock > Contents { padding: 0 0 0 5; }
        #feed .reasoning-body { color: $secondary; }

        /* The dock: completion popup, the prompt block, then the status row. */
        #dock { height: auto; }
        #completer {
            height: auto; max-height: 9; display: none;
            background: $surface; border: none; padding: 0 1 0 3; margin-bottom: 1;
            scrollbar-size-vertical: 1;
        }
        #completer > .option-list--option-highlighted { background: transparent; color: $primary; text-style: bold; }

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
            Binding("pageup", "feed_page_up", show=False),
            Binding("pagedown", "feed_page_down", show=False),
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
            self._md: Optional[PlainMarkdown] = None
            self._md_stream: Any = None
            self._reasoning: Optional[ReasoningBlock] = None
            self._note_block: Optional[NoteLine] = None
            self._note_lines = 0
            self._tool_gen_line: Optional[ToolLine] = None
            self._tool_lines: dict[str, list[ToolLine]] = {}
            self._running_tools: list[ToolLine] = []
            self._refresh_scheduled = False
            self._modal_open = False
            self._serviced: dict[str, Any] = {}  # attr -> the state object already shown
            self._spin = 0
            self._completion_cmds: list[str] = []
            self._queued = 0
            self._last_ctrl_c = 0.0
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
                    with VerticalScroll(id="feed"):
                        yield Wordmark(id="wordmark", classes="wordmark")
                        yield Static("", id="intro", classes="intro")
                    with Vertical(id="dock"):
                        yield OptionList(id="completer")
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
                    yield Static("context", classes="label")
                    yield Static("", id="ctx-bar", classes="kv")
                    yield Static("", id="ctx-tokens", classes="kv muted")
                    yield Static("session usage", classes="label")
                    yield Static("", id="ctx-usage", classes="kv muted")
                    yield Static("background", classes="label")
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
            self._feed = self.query_one("#feed", VerticalScroll)
            self._prompt = self.query_one("#input", PromptArea)
            self._prompt.prompt_history = PromptHistory(getattr(self.backend, "_history_file", None))
            self._completer = self.query_one("#completer", OptionList)
            self._status_left = self.query_one("#status-left", Static)
            self._status_right = self.query_one("#status-right", Static)
            self._panel = self.query_one("#context", Vertical)
            self._prompt.focus()
            self._apply_sidebar(self.size.width)
            self._show_wordmark()
            self._show_intro()
            self._feed.anchor()
            self.begin_capture_print(self, stdout=True, stderr=True)
            self._stdout_buf = ""
            self._stderr_buf = ""
            self._redirect_log_handlers()
            if self.backend is not None:
                self.backend.attach(_AppSink(self), feed_width=self._feed_inner_width())
                self._render_resumed_history()
            self.set_interval(0.12, self._tick_spinner)
            self.set_interval(1.0, self._tick_chrome)
            self._refresh_chrome()

        def on_unmount(self) -> None:
            if self.backend is not None:
                # Textual runs threaded workers on asyncio's default executor and
                # the loop JOINS them at shutdown, so a turn still in flight would
                # stall the quit. Unwind it (and any blocked prompt) first.
                self.backend.interrupt_turn()
                self.backend.cancel_pending_prompts()
            self._restore_log_handlers()
            try:
                self.end_capture_print(self)
            except Exception:
                pass
            if self.backend is not None:
                self.backend.detach()

        def on_resize(self, event: Any) -> None:
            self._apply_sidebar(event.size.width)
            # The wordmark is sized from the mounted feed, which has not been
            # re-laid-out yet at this point, so measure again once it has.
            self.call_after_refresh(self._on_resized)

        def _on_resized(self) -> None:
            self._show_wordmark()
            if self.backend is not None:
                self.backend.set_feed_width(self._feed_inner_width())

        def _wide(self, width: Optional[int] = None) -> bool:
            return (self.size.width if width is None else width) > SIDEBAR_THRESHOLD

        def _sidebar_visible(self, width: Optional[int] = None) -> bool:
            """opencode: shown when explicitly opened, else only on a wide terminal."""
            if self._sidebar_forced is not None:
                return self._sidebar_forced
            return self._wide(width)

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

        def _show_wordmark(self) -> None:
            """Nudge the wordmark to re-fit (it sizes itself on its own resize)."""
            try:
                self.query_one("#wordmark", Wordmark).fit(self._feed_inner_width())
            except Exception:
                pass

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
            parts.append(_short_path(os.getenv("TERMINAL_CWD", os.getcwd())))
            text = Text("  ·  ".join(parts), style="dim")
            text.append("\n/help for commands · ctrl+p for the palette · :q to quit", style="dim")
            try:
                self.query_one("#intro", Static).update(text)
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
        def _mount(self, widget: Any) -> None:
            """Mount a transcript row, separating it from a block above it.

            opencode's rule (``setPreLayoutSiblingMargin`` + ``alwaysSeparate``):
            a row gets a blank line above it when the previous sibling was a
            block, or was taller than one line. Consecutive one-line tool rows
            stay tight; anything following a user message, an answer or a
            reasoning block gets air.
            """
            previous = None
            for child in reversed(self._feed.children):
                if child.id in ("wordmark", "intro"):
                    break
                previous = child
                break
            if previous is not None and not isinstance(widget, _ALWAYS_SEPARATE):
                separate = isinstance(previous, _ALWAYS_SEPARATE)
                if not separate and isinstance(previous, NoteLine):
                    separate = previous.line_count > 1
                if separate:
                    widget.styles.margin = (1, 0, 0, 0)
            # Anything that isn't another chrome line ends the run of lines a
            # NoteLine is merging, so the merge can't reach across a block.
            if not isinstance(widget, NoteLine):
                self._reset_note_block()
            self._feed.mount(widget)

        def _reset_note_block(self) -> None:
            self._note_block = None
            self._note_lines = 0

        def _note(self, text: str, *, muted: bool = False) -> None:
            """Append one ANSI/plain line, merging consecutive lines into one block."""
            if not text.strip():
                return
            rich = Text.from_ansi(text.rstrip())
            if muted:
                rich.stylize("dim")
            if self._note_block is not None and self._note_lines < 400:
                if self._note_block.append_line(rich):
                    self._note_lines += 1
                    return
            block = NoteLine(rich)
            self._mount(block)
            self._note_block = block
            self._note_lines = 1

        def _add_user_turn(self, text: str) -> None:
            self._mount(UserTurn(Text(text)))
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
            """/skin changed the accents — re-resolve the CSS variables."""
            try:
                self.refresh_css()
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
            self._mount(NoteLine(renderable))

        async def _ev_assistant_start(self) -> None:
            await self._close_assistant()
            self._reset_note_block()
            self._md = PlainMarkdown("")
            await self._feed.mount(self._md)
            self._md_stream = Markdown.get_stream(self._md)

        async def _ev_assistant_delta(self, text: str = "") -> None:
            if not text:
                return
            if self._md_stream is None:
                await self._ev_assistant_start()
            self._transcript += text
            await self._md_stream.write(text)

        async def _ev_assistant_end(self) -> None:
            await self._close_assistant()

        async def _close_assistant(self) -> None:
            stream, self._md_stream = self._md_stream, None
            self._md = None
            if stream is not None:
                try:
                    await stream.stop()
                except Exception:
                    pass

        def _ev_reasoning_start(self) -> None:
            self._finish_reasoning()
            self._reset_note_block()
            self._reasoning = ReasoningBlock()
            self._mount(self._reasoning)

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

        def _ev_tool_gen(self, name: str = "") -> None:
            self._reset_note_block()
            line = ToolLine(f"Preparing {name}…", _tool_icon(name))
            self._tool_gen_line = line
            self._mount(line)
            line.render_running(_SPINNER[self._spin])

        def _ev_tool_start(self, name: str = "", label: str = "", hidden: bool = False) -> None:
            self._reset_note_block()
            line = self._tool_gen_line
            self._tool_gen_line = None
            if hidden:
                if line is not None:
                    line.remove()
                return
            if line is None:
                line = ToolLine(label or name, _tool_icon(name))
                self._mount(line)
            else:
                line.label = label or name
                line.icon = _tool_icon(name)
            line.render_running(_SPINNER[self._spin])
            self._tool_lines.setdefault(name, []).append(line)
            self._running_tools.append(line)

        def _ev_tool_done(
            self,
            name: str = "",
            label: str = "",
            line: str = "",
            duration: float = 0.0,
            is_error: bool = False,
            hidden: bool = False,
        ) -> None:
            rows = self._tool_lines.get(name)
            row = rows.pop(0) if rows else None
            if rows is not None and not rows:
                self._tool_lines.pop(name, None)
            if row is not None and row in self._running_tools:
                self._running_tools.remove(row)
            if hidden:
                if row is not None:
                    row.remove()
                return
            if row is None:
                if not (label or line):
                    return
                row = ToolLine(label or name, _tool_icon(name))
                self._reset_note_block()
                self._mount(row)
            row.finish(label, is_error, duration)

        async def _ev_turn_end(self) -> None:
            await self._close_assistant()
            self._finish_reasoning()
            if self._tool_gen_line is not None:
                self._tool_gen_line.remove()
                self._tool_gen_line = None
            for row in self._running_tools:
                if not row.done:
                    row.finish("", False, time.monotonic() - row.started)
            self._running_tools.clear()
            self._tool_lines.clear()
            self._reset_note_block()

        # ---------------- chrome ----------------
        def _schedule_refresh(self) -> None:
            if self._refresh_scheduled:
                return
            self._refresh_scheduled = True
            self.set_timer(0.08, self._refresh_chrome)

        def _tick_spinner(self) -> None:
            self._spin = (self._spin + 1) % len(_SPINNER)
            if self._reasoning is not None:
                self._reasoning.flush()
            for row in self._running_tools:
                row.render_running(_SPINNER[self._spin])
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
                action = ""
                try:
                    action = (backend._render_spinner_text() or "").strip() if backend else ""
                except Exception:
                    action = ""
                started = getattr(backend, "_prompt_start_time", None) if backend else None
                left = Text.assemble((f"{frame} ", "bold"), action or "thinking")
                if started:
                    left.append(f"  {time.time() - started:.0f}s", style="dim")
                self._status_left.add_class("busy")
            elif self._busy:
                left = Text.assemble((f"{frame} ", "bold"), f"running {self._busy}")
                self._status_left.add_class("busy")
            else:
                left = Text(_short_path(os.getenv("TERMINAL_CWD", os.getcwd())))
                self._status_left.remove_class("busy")
            if self._queued:
                left.append(f"  · {self._queued} queued", style="dim")
            self._status_left.update(left)

            right = Text()
            if self._busy:
                right.append("ctrl+c", style="bold")
                right.append(" interrupt")
            else:
                snap = backend.status_snapshot() if backend is not None else {}
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
                snap = backend.status_snapshot()
                mode = getattr(backend, "_agent_mode", None) or "auto"
                meta.append(mode)
                perm = backend.permission_mode()
                if perm != "default":
                    # yolo skips dangerous-command approvals and persists, so it
                    # is never allowed to look like an incidental label.
                    meta.append(
                        f"  {perm}",
                        style={"yolo": "bold red", "lockdown": "yellow"}.get(perm, ""),
                    )
                    meta.append(" (session)", style="dim")
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
            try:
                self.query_one("#prompt-meta-left", Static).update(meta)
                self.query_one("#prompt-meta-right", Static).update(
                    Text(f"{len(self._attached())} attached") if self._attached() else Text()
                )
            except Exception:
                pass

        def _attached(self) -> list:
            return list(getattr(self.backend, "_attached_images", None) or [])

        def _update_context(self) -> None:
            if not self._panel.display:
                return
            backend = self.backend
            cwd = _short_path(os.getenv("TERMINAL_CWD", os.getcwd()))
            if backend is None:
                self.query_one("#ctx-title", Static).update(self.agent_name)
                self.query_one("#ctx-cwd", Static).update(cwd)
                return
            snap = backend.status_snapshot()
            self.query_one("#ctx-title", Static).update(snap.get("session_title") or "untitled")
            self.query_one("#ctx-session-id", Static).update(str(getattr(backend, "session_id", "") or ""))
            self.query_one("#ctx-cwd", Static).update(cwd)
            self.query_one("#ctx-mode", Static).update(getattr(backend, "_agent_mode", None) or "auto")
            pct = snap.get("context_percent")
            if pct is None:
                self.query_one("#ctx-bar", Static).update(Text("▱▱▱▱▱▱▱▱▱▱  —", style="dim"))
                self.query_one("#ctx-tokens", Static).update("no turns yet")
            else:
                filled = max(0, min(10, round(pct / 10)))
                self.query_one("#ctx-bar", Static).update(f"{'▰' * filled}{'▱' * (10 - filled)}  {pct}%")
                self.query_one("#ctx-tokens", Static).update(
                    f"{_fmt_tokens(snap.get('context_tokens', 0))} / {_fmt_tokens(snap.get('context_length') or 0)}"
                    + (f" · {snap['compressions']} compressed" if snap.get("compressions") else "")
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
            self.query_one("#ctx-bg", Static).update(" · ".join(bg) or "idle")
            self.query_one("#ctx-elapsed", Static).update(f"session {snap.get('duration', '')}".rstrip())

        # ---------------- modal servicing ----------------
        def _service_modals(self) -> None:
            """Show a modal for whichever backend prompt is waiting (one at a time)."""
            backend = self.backend
            if backend is None or self._modal_open:
                return
            for attr, opener in (
                ("_approval_state", self._open_approval),
                ("_sudo_state", self._open_sudo),
                ("_secret_state", self._open_secret),
                ("_clarify_state", self._open_clarify),
                ("_slash_confirm_state", self._open_slash_confirm),
                ("_tui_text_prompt_state", self._open_text_prompt),
                ("_tui_picker_state", self._open_picker),
                ("_model_picker_state", self._open_model_picker),
            ):
                state = getattr(backend, attr, None)
                if not state:
                    continue
                # Clarify (batch) and the model picker mutate one state dict in
                # place as they advance, so they are re-serviced on every tick;
                # the rest are shown once per state object.
                if self._serviced.get(attr) is state and attr not in ("_clarify_state", "_model_picker_state"):
                    continue
                self._serviced[attr] = state
                self._modal_open = True
                opener(state)
                return

        def _push(self, screen: Any, callback: Any) -> None:
            def _done(result: Any) -> None:
                self._modal_open = False
                try:
                    callback(result)
                finally:
                    self._prompt.focus()
                    self._schedule_refresh()

            self.push_screen(screen, _done)

        def _open_approval(self, state: dict) -> None:
            labels = {
                "once": ("Allow once", "run this command now"),
                "session": ("Allow for this session", "no more prompts for this command until exit"),
                "always": ("Always allow", "add to the allowlist"),
                "deny": ("Deny", "the agent is told the command was refused"),
            }
            choices = [(k, *labels[k]) for k in state.get("choices", []) if k in labels]
            detail = [
                Static(Text(str(state.get("command", "")), style="bold"), classes="command"),
                Static(Text(str(state.get("description", "")), style="dim"), classes="dialog-detail"),
            ]
            deadline = getattr(self.backend, "_approval_deadline", 0) or None
            screen = ChoiceModal("The agent wants to run a command", choices, detail, deadline=deadline, accent="warning")

            def _answer(result: Any) -> None:
                if self.backend._approval_state is state:
                    state["response_queue"].put(result or "deny")

            self._push(screen, _answer)

        def _open_sudo(self, state: dict) -> None:
            screen = TextModal("sudo password", "password (Enter to skip)", password=True,
                               detail="The command needs sudo. The password is cached for this session and never shown to the model.")

            def _answer(result: Any) -> None:
                if self.backend._sudo_state is state:
                    state["response_queue"].put(result or "")

            self._push(screen, _answer)

        def _open_secret(self, state: dict) -> None:
            screen = TextModal(
                f"Secret: {state.get('var_name', '')}",
                str(state.get("prompt", "")) or "value (Enter to skip)",
                password=True,
                detail="Stored in ~/.son-of-anton/.env — never exposed to the model.",
            )

            def _answer(result: Any) -> None:
                if self.backend._secret_state is state:
                    state["response_queue"].put(result or "")

            self._push(screen, _answer)

        def _open_clarify(self, state: dict) -> None:
            backend = self.backend
            deadline = getattr(backend, "_clarify_deadline", None) or None
            batch = bool(state.get("questions"))
            question = str(state.get("question", ""))
            title = "Son of Anton needs your input"
            if batch:
                title = f"Question {state.get('active', 0) + 1} of {len(state['questions'])}"
            choices = list(state.get("choices") or [])
            multi = bool(state.get("multi_select"))

            def _lock(answer: Any, meta: Optional[dict] = None) -> None:
                """Deliver ``answer`` the way the prompt_toolkit Enter handler did."""
                if backend._clarify_state is not state:
                    return
                if batch:
                    backend._clarify_batch_lock(state, answer, meta=meta)
                    backend._clarify_freetext = False
                else:
                    state["response_queue"].put(answer)
                    backend._clarify_state = None
                    backend._clarify_freetext = False

            def _freetext(prefill: str = "", base: Optional[list] = None) -> None:
                self._modal_open = True

                def _typed(text: Any) -> None:
                    if text is None or not str(text).strip():
                        if not batch:
                            _lock("")
                        return
                    text = str(text).strip()
                    if batch:
                        if base is not None:
                            import json

                            _lock(json.dumps(base + [text], ensure_ascii=False),
                                  meta={"kind": "multi", "choices": base, "other_text": text})
                        else:
                            _lock(text, meta={"kind": "other", "other_text": text})
                    else:
                        if base:
                            text = ", ".join(base) + ", " + text
                        _lock(text)

                self._push(TextModal(title, "your answer", prefill=prefill, detail=Text(question)), _typed)

            if not choices:
                self._modal_open = False
                _freetext()
                return

            if multi:
                rows = [(str(i), c) for i, c in enumerate(choices)] + [("__other__", "Other…")]

                def _picked(result: Any) -> None:
                    if result is None:
                        _lock("" if not batch else "[]", meta={"kind": "multi", "choices": [], "other_text": ""})
                        return
                    keys = [k for k in result]
                    picked = [choices[int(k)] for k in keys if k != "__other__"]
                    if "__other__" in keys:
                        _freetext(base=picked)
                        return
                    if batch:
                        import json

                        _lock(json.dumps(picked, ensure_ascii=False), meta={"kind": "multi", "choices": picked, "other_text": ""})
                    else:
                        _lock(", ".join(picked))

                self._push(MultiChoiceModal(title, rows, Text(question), deadline=deadline), _picked)
                return

            rows = [(str(i), c, "") for i, c in enumerate(choices)] + [("__other__", "Other…", "type your own answer")]

            def _chosen(result: Any) -> None:
                if result is None:
                    _lock("The user dismissed the question without answering; use your best judgement.")
                    return
                if result == "__other__":
                    _freetext()
                    return
                _lock(choices[int(result)], meta={"kind": "choice"})

            self._push(ChoiceModal(title, rows, Text(question), deadline=deadline, default=int(state.get("selected", 0) or 0)), _chosen)

        def _open_slash_confirm(self, state: dict) -> None:
            choices = [tuple(c) for c in state.get("choices", [])]
            screen = ChoiceModal(str(state.get("title", "Confirm")), choices, Text(str(state.get("detail", ""))), accent="warning")

            def _answer(result: Any) -> None:
                if self.backend._slash_confirm_state is state:
                    state["response_queue"].put(result if result is not None else "cancel")

            self._push(screen, _answer)

        def _open_text_prompt(self, state: dict) -> None:
            screen = TextModal(str(state.get("prompt", "")).strip() or "Input", "", password=bool(state.get("password")))

            def _answer(result: Any) -> None:
                if self.backend._tui_text_prompt_state is state:
                    state["response_queue"].put(result)

            self._push(screen, _answer)

        def _open_picker(self, state: dict) -> None:
            items = [(str(i), str(item), "") for i, item in enumerate(state.get("items", []))]
            screen = ChoiceModal(str(state.get("title", "Pick one")), items, filterable=len(items) > 12,
                                 default=int(state.get("default", 0) or 0))

            def _answer(result: Any) -> None:
                if self.backend._tui_picker_state is state:
                    state["response_queue"].put(None if result is None else int(result))

            self._push(screen, _answer)

        def _open_model_picker(self, state: dict) -> None:
            backend = self.backend
            stage = state.get("stage")
            if stage == "provider":
                providers = state.get("providers") or []
                rows = []
                for i, p in enumerate(providers):
                    label = _provider_label(p)
                    count = p.get("total_models") or len(p.get("models") or [])
                    desc = f"{count} model{'s' if count != 1 else ''}"
                    if p.get("is_current"):
                        label = f"{label}  ●"
                    rows.append((str(i), label, desc))
                screen = ChoiceModal(
                    f"Switch model  ·  now {state.get('current_model', '')} via {state.get('current_provider', '')}",
                    rows, "Pick a provider", default=int(state.get("selected", 0) or 0),
                )

                def _picked(result: Any) -> None:
                    if backend._model_picker_state is not state:
                        return
                    if result is None:
                        backend._close_model_picker()
                        return
                    self._advance_model_picker(state, int(result))

                self._push(screen, _picked)
                return
            if stage == "model":
                model_list = list(state.get("model_list") or [])
                state["_filtered_pairs"] = None
                current = str(state.get("current_model") or "")
                rows = []
                current_idx = 0
                for i, m in enumerate(model_list):
                    if m == current:
                        current_idx = i
                        rows.append((str(i), f"{m}  ●", "current"))
                    else:
                        rows.append((str(i), m, ""))
                rows.append(("__back__", "← back to providers", ""))
                screen = ChoiceModal(
                    f"Models  ·  {_provider_label(state.get('provider_data'))}",
                    rows,
                    filterable=True,
                    default=current_idx,
                )

                def _picked(result: Any) -> None:
                    if backend._model_picker_state is not state:
                        return
                    if result is None:
                        backend._close_model_picker()
                        return
                    self._advance_model_picker(
                        state, len(model_list) if result == "__back__" else int(result)
                    )

                self._push(screen, _picked)
                return
            self._modal_open = False

        def _advance_model_picker(self, state: dict, selected: int) -> None:
            """Run the picker's own state machine for the chosen row.

            Unlike every other modal — which answers a queue the worker thread is
            already blocked on — the picker re-enters the backend to advance
            itself (provider → model list → switch).  That work runs on a thread,
            and this must be scheduled rather than merely created: an un-awaited
            coroutine here silently does nothing.  ``_modal_open`` stays set until
            it finishes so the ticker cannot re-open the stage we are leaving.
            """
            state["selected"] = selected
            self._modal_open = True

            async def _advance() -> None:
                try:
                    await self._run_in_worker(
                        "model", self.backend._handle_model_picker_selection
                    )
                finally:
                    self._modal_open = False
                    self._schedule_refresh()

            self.run_worker(_advance(), group="model-picker")

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

        async def _submit_text(self, value: str) -> None:
            if self.backend is None:
                if value in (":q", ":quit", "/quit", "/exit"):
                    self.exit()
                    return
                self._add_user_turn(value)
                self._note("(no agent attached — this frame is running standalone)", muted=True)
                return
            if self._busy == "turn":
                # The agent is mid-turn: hand the message to chat()'s interrupt
                # monitor (or queue it, per display.busy_input_mode).
                mode = getattr(self.backend, "busy_input_mode", "interrupt")
                if mode == "queue":
                    self.backend._pending_input.put(value)
                    self._queued += 1
                    self._note("queued for the next turn", muted=True)
                else:
                    self.backend._interrupt_queue.put(value)
                    self._note("interrupting the current turn…", muted=True)
                self._update_status()
                return
            if self._busy:
                self.backend._pending_input.put(value)
                self._queued += 1
                self._update_status()
                return
            self.run_worker(self._dispatch(value), group="dispatch", exclusive=False)

        async def _dispatch(self, value: str, images: Optional[list] = None) -> None:
            backend = self.backend
            import cli as _cli

            # A bare number right after a bare `/resume` picks that session, so
            # it must not be sent to the agent as a message.
            if backend.consume_resume_selection(value):
                await self._after_dispatch()
                return
            if _cli._looks_like_slash_command(value) or value in (":q", ":quit"):
                self._reset_note_block()
                self._mount(NoteLine(Text(f"⚙ {value}"), classes="command"))
                self._transcript_scroll()
                keep_going = await self._run_in_worker("command", partial(backend.run_slash, value))
                if keep_going is False:
                    self.exit()
                    return
                seed = getattr(backend, "_pending_agent_seed", None)
                if seed:
                    backend._pending_agent_seed = None
                    await self._dispatch(seed)
                    return
                await self._after_dispatch()
                return
            if value.startswith("!"):
                self._reset_note_block()
                self._mount(NoteLine(Text(value), classes="command"))
                handled = await self._run_in_worker("shell", partial(backend.handle_bang_shell, value))
                if handled:
                    await self._after_dispatch()
                    return
            self._add_user_turn(value)
            await self._run_in_worker("turn", partial(backend.run_turn, value, images))
            await self._after_dispatch()

        async def _after_dispatch(self) -> None:
            self._schedule_refresh()
            pending = self.backend.drain_pending_input() if self.backend else []
            self._queued = 0
            for item in pending:
                images = None
                if isinstance(item, tuple):
                    item, images = item
                if isinstance(item, str) and item.strip():
                    await self._dispatch(item.strip(), images)

        async def _run_in_worker(self, label: str, fn: Any) -> Any:
            self._busy = label
            self._update_status()
            worker = self.run_worker(fn, thread=True, exit_on_error=False, group="agent", exclusive=False)
            try:
                return await worker.wait()
            except Exception as exc:  # WorkerFailed
                self._note(f"error: {getattr(exc, 'error', exc)}", muted=True)
                return None
            finally:
                self._busy = None
                self._update_status()

        # ---------------- slash completion ----------------
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
            text = self._prompt.text
            if text.startswith("/") and "\n" not in text and " " not in text.strip():
                self._show_completer(text.strip())
            else:
                self._hide_completer()

        def _show_completer(self, prefix: str) -> None:
            q = prefix.lower()
            matches = [(c, d) for c, _cat, d in self._palette_entries() if c.lower().startswith(q)]
            if not matches or (len(matches) == 1 and matches[0][0].lower() == q):
                self._hide_completer()
                return
            matches = matches[:40]
            self._completer.clear_options()
            self._completion_cmds = [c for c, _ in matches]
            col = max(len(c) for c, _ in matches) + 2
            room = max(20, self._feed_width() - col - 4)
            for c, d in matches:
                d = d.split(" (usage:")[0].strip()
                if len(d) > room:
                    d = d[: room - 1] + "…"
                row = Text.assemble((c.ljust(col), "bold"), (d, "dim"))
                row.no_wrap = True
                row.overflow = "ellipsis"
                self._completer.add_option(Option(row))
            self._completer.highlighted = 0
            self._completer.display = True
            self._prompt.completer_active = True

        def _hide_completer(self) -> None:
            self._completer.display = False
            self._prompt.completer_active = False
            self._completion_cmds = []

        @on(PromptArea.Complete)
        async def _complete(self, event: PromptArea.Complete) -> None:
            action = event.action
            if action == "close":
                self._hide_completer()
                return
            if action in ("up", "down"):
                if action == "up":
                    self._completer.action_cursor_up()
                else:
                    self._completer.action_cursor_down()
                return
            idx = self._completer.highlighted or 0
            if 0 <= idx < len(self._completion_cmds):
                cmd = self._completion_cmds[idx]
                if action == "accept-submit" and self._prompt.text.strip().lower() == cmd.lower():
                    self._hide_completer()
                    self._prompt.text = ""
                    await self._submit_text(cmd)
                    return
                self._prompt.text = cmd + " "
                self._prompt.move_cursor(self._prompt.document.end)
            self._hide_completer()

        @on(OptionList.OptionSelected, "#completer")
        def _completer_clicked(self, event: OptionList.OptionSelected) -> None:
            idx = event.option_index
            if 0 <= idx < len(self._completion_cmds):
                self._prefill_prompt(self._completion_cmds[idx])
            self._hide_completer()

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
            """Click-and-highlight auto-copies, with a "copied" toast.

            Textual's native mouse selection is already active (feed widgets
            are selectable by default) and it posts ``TextSelected`` on
            mouse-up — but it stops there and makes the user hit ctrl+c.
            Mirroring what terminals do out of the box, we copy the
            selection to the system clipboard (``copy_to_clipboard`` writes
            OSC 52) and surface a short toast. A plain click yields no
            selection, so this is a no-op.
            """
            try:
                selected = self.screen.get_selected_text()
            except Exception:
                return
            if not selected or not selected.strip():
                return
            self.copy_to_clipboard(selected)
            self.notify(f"copied {len(selected.strip())} chars", timeout=1.5)

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
            for w in list(self._feed.children):
                if w.id in ("wordmark", "intro"):
                    continue
                w.remove()
            self._reset_note_block()
            self._transcript = ""

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
                self._run_in_worker("permissions", self.backend.cycle_permission_mode),
                group="permissions",
            )

        def action_toggle_sidebar(self) -> None:
            self._sidebar_forced = not self._sidebar_visible()
            self._apply_sidebar(self.size.width)
            self.call_after_refresh(self._on_resized)
            self._update_context()

        def action_feed_page_up(self) -> None:
            self._feed.scroll_page_up()

        def action_feed_page_down(self) -> None:
            self._feed.scroll_page_down()

        # ---------------- resume ----------------
        def _render_resumed_history(self) -> None:
            backend = self.backend
            history = getattr(backend, "_resume_display_history", None) or getattr(backend, "conversation_history", None)
            if not getattr(backend, "_resumed", False) or not history:
                return
            try:
                limit = int((backend.config or {}).get("display", {}).get("resume_exchanges", 10)) * 2
            except Exception:
                limit = 20
            shown = [m for m in history if m.get("role") in ("user", "assistant") and m.get("display_kind") != "hidden"]
            hidden = max(0, len(shown) - limit)
            self._note(f"resumed session {backend.session_id}" + (f" · {hidden} earlier messages not shown" if hidden else ""), muted=True)
            for msg in shown[-limit:]:
                content = msg.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") if isinstance(p, dict) and p.get("type") == "text" else "[image]"
                        for p in content if isinstance(p, dict)
                    )
                content = (content or "").strip()
                if msg.get("role") == "user":
                    if content:
                        self._mount(UserTurn(Text(content)))
                else:
                    try:
                        import cli as _cli

                        content = _cli._strip_reasoning_tags(content)
                    except Exception:
                        pass
                    if content:
                        self._mount(PlainMarkdown(content))
                    for call in msg.get("tool_calls") or []:
                        name = (call.get("function") or {}).get("name") if isinstance(call, dict) else None
                        if name:
                            row = ToolLine(name, _tool_icon(name))
                            self._mount(row)
                            row.finish(name, False)
            self._reset_note_block()

else:
    SonOfAntonTUIApp = None  # type: ignore
    PlainMarkdown = None  # type: ignore
    PromptArea = None  # type: ignore


def run_app(backend: Any) -> None:
    """Launch the Textual front-end around a prepared ``TextualBackend``."""
    if not _TEXTUAL_AVAILABLE or SonOfAntonTUIApp is None:
        print("Textual is not installed. Install the `tui` extra: pip install -e '.[tui]'")
        return
    if not backend.prepare_interactive_state():
        return
    if getattr(backend, "_resumed", False):
        try:
            backend._preload_resumed_session()
        except Exception:
            pass
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
        print("Textual is not installed. Install the `tui` extra: pip install -e '.[tui]'")
        return
    SonOfAntonTUIApp().run()


if __name__ == "__main__":
    _run_demo()
