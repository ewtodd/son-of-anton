"""The virtual transcript feed — one widget, zero children, unbounded scrollback.

The transcript used to be one Textual widget per row, and Textual re-lays-out
the whole scroll container on every mount, so the marginal cost of a row grew
linearly with the row count (~1 ms at 2k rows, ~6 ms at 16k).  Marathon
sessions stalled on every event.  This feed renders the transcript through the
Line API instead: blocks of content are rendered to line strips, and only the
lines currently in the viewport are painted per frame, so the frame cost is
O(visible lines) no matter how long the session runs.  The block list itself
grows without bound — the session store is the real history, and the feed
keeps all of it reachable by scrolling.

Blocks are dumb data: the app owns the turn state machine and mutates block
content; :class:`VirtualFeed` owns geometry (offsets, virtual size, painting).
Mutated blocks are marked dirty and re-rendered on the app's tick via
:meth:`VirtualFeed.flush`, so a flood of lines costs one re-render per frame.

Markdown is rendered by :func:`render_markdown_lines` with the same
``gfm-like`` markdown_it preset Textual's Markdown widget uses, in the fork's
plain aesthetic (no syntax rainbow), and tables go through the shared
``realign_markdown_tables`` helper so CJK column drift stays consistent with
the classic CLI path.
"""

from __future__ import annotations

import bisect
import re
import time
from typing import Any, Optional

from markdown_it import MarkdownIt
from rich.cells import cell_len
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from textual import events
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from agent.markdown_tables import realign_markdown_tables

_CONSOLE = Console(color_system="standard", force_terminal=True, highlight=False)
_MD = MarkdownIt("gfm-like")
_COLLAPSE_WS = re.compile(r"\s+")

# opencode gives each tool a one-glyph icon in a two-cell column, so labels
# align no matter the tool.
TOOL_ICON_WIDTH = 2
# Consecutive ANSI lines merge into one note block; the cap keeps a flood of
# output re-rendering a bounded block.
_NOTE_MERGE_MAX_LINES = 100


def _to_strips(renderable: Any, width: int) -> list[Strip]:
    """Render any rich renderable into line strips at ``width`` cells."""
    options = _CONSOLE.options.update_width(max(1, int(width)))
    return [Strip(line) for line in _CONSOLE.render_lines(renderable, options=options, pad=False)]


def _pad(strips: list[Strip], columns: int) -> list[Strip]:
    """Prefix every strip with ``columns`` blank cells (opencode's indents)."""
    if columns <= 0:
        return strips
    blank = Strip([Segment(" " * columns)])
    return [Strip.join([blank, strip]) for strip in strips]


class FeedStyle:
    """Rich style tokens for the transcript, resolved from the active skin."""

    __slots__ = ("primary", "secondary", "muted", "code", "link", "error", "panel")

    def __init__(
        self,
        primary: str = "",
        secondary: str = "",
        muted: str = "dim",
        code: str = "dim",
        link: str = "underline",
        error: str = "red",
        panel: str = "",
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.muted = muted
        self.code = code
        self.link = link
        self.error = error
        self.panel = panel


def _combine(*styles: str) -> Optional[str]:
    joined = " ".join(s for s in styles if s)
    return joined or None


# ---------------------------------------------------------------------------
# Inline markdown
# ---------------------------------------------------------------------------

def _render_inline_children(children: list, styles: FeedStyle, base: str = "") -> Text:
    """Inline markdown tokens -> one rich Text (same walk Textual's widget uses)."""
    text = Text()
    i = 0
    while i < len(children):
        child = children[i]
        child_type = child.type
        if child_type == "text":
            text.append(_COLLAPSE_WS.sub(" ", child.content), style=base or None)
            i += 1
        elif child_type == "softbreak":
            text.append(" ", style=base or None)
            i += 1
        elif child_type == "hardbreak":
            text.append("\n", style=base or None)
            i += 1
        elif child_type == "code_inline":
            text.append(child.content, style=_combine(base, styles.code))
            i += 1
        elif child_type in ("em_open", "strong_open", "s_open", "link_open"):
            add = {
                "em_open": "italic",
                "strong_open": "bold",
                "s_open": "strike",
                "link_open": styles.link,
            }[child_type]
            close = child_type.replace("_open", "_close")
            depth = 1
            j = i + 1
            while depth:
                if children[j].type == child_type:
                    depth += 1
                elif children[j].type == close:
                    depth -= 1
                j += 1
            text.append_text(
                _render_inline_children(children[i + 1 : j - 1], styles, _combine(base, add) or base)
            )
            i = j
        elif child_type == "image":
            alt = (child.content or "").strip()
            text.append(f"🖼 {alt}" if alt else "🖼", style=_combine(base, styles.muted))
            i += 1
        elif child_type in ("em_close", "strong_close", "s_close", "link_close"):
            i += 1
        else:
            # Autolinks, entities and anything unexpected: keep the visible text.
            if child.content:
                text.append(_COLLAPSE_WS.sub(" ", child.content), style=base or None)
            i += 1
    return text


def _block_strips(body: Text, width: int, pad: int) -> list[Strip]:
    return _pad(_to_strips(body, width), pad)


# ---------------------------------------------------------------------------
# Block markdown
# ---------------------------------------------------------------------------

def render_markdown_lines(source: str, width: int, styles: FeedStyle) -> list[Strip]:
    """Render markdown source to line strips, in the fork's plain aesthetic."""
    strips: list[Strip] = []
    tokens = _MD.parse(source)
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        token_type = token.type

        if token_type == "heading_open":
            level = int(token.tag[1])
            inline = tokens[i + 1] if i + 1 < n and tokens[i + 1].type == "inline" else None
            body = (
                _render_inline_children(inline.children or [], styles)
                if inline is not None
                else Text(token.tag)
            )
            body.stylize(_combine("bold", styles.primary if level <= 2 else ""))
            strips.extend(_block_strips(body, width, 3))
            i += 3 if inline is not None else 2

        elif token_type == "paragraph_open":
            inline = tokens[i + 1]
            body = _render_inline_children(inline.children or [], styles)
            strips.extend(_block_strips(body, width, 3))
            i += 3

        elif token_type == "fence":
            for line in token.content.rstrip("\n").split("\n"):
                strips.extend(
                    _to_strips(Text("▎" + line, style=styles.code, overflow="crop"), width)
                )
            i += 1

        elif token_type == "table_open":
            start, end = token.map
            raw = "\n".join(source.splitlines()[start:end])
            aligned = realign_markdown_tables(raw, width - 3)
            for line in aligned.split("\n"):
                strips.extend(_to_strips(Text(line, overflow="crop"), width - 3))
                strips[-1] = _pad([strips[-1]], 3)[0]
            i += 1
            while i < n and tokens[i].type != "table_close":
                i += 1
            i += 1

        elif token_type in ("bullet_list_open", "ordered_list_open"):
            ordered = token_type == "ordered_list_open"
            counter = 1
            i += 1
            while i < n and tokens[i].type not in ("bullet_list_close", "ordered_list_close"):
                item = tokens[i]
                if item.type != "list_item_open":
                    i += 1
                    continue
                item_level = item.level
                parts: list[str] = []
                j = i + 1
                while j < n and not (tokens[j].type == "list_item_close" and tokens[j].level == item_level):
                    if tokens[j].type == "inline" and tokens[j].level == item_level + 2:
                        parts.append(_render_inline_children(tokens[j].children or [], styles).plain)
                    j += 1
                marker = f"{counter}. " if ordered else "• "
                counter += 1
                body = Text("\n\n".join(parts))
                item_strips = _to_strips(body, max(1, width - 3 - len(marker)))
                for k, strip in enumerate(item_strips):
                    lead = marker if k == 0 else " " * len(marker)
                    lead_strip = _to_strips(Text(lead, style=styles.secondary), len(marker))[0]
                    strips.append(_pad([Strip.join([lead_strip, strip])], 3)[0])
                i = j + 1
            i += 1

        elif token_type == "blockquote_open":
            parts: list[str] = []
            j = i + 1
            while j < n and tokens[j].type != "blockquote_close":
                if tokens[j].type == "inline" and tokens[j].level == 2:
                    parts.append(_render_inline_children(tokens[j].children or [], styles).plain)
                j += 1
            body = Text("\n\n".join(parts))
            for strip in _to_strips(body, max(1, width - 5)):
                strips.append(_pad([Strip.join([_to_strips(Text("▎", style=styles.secondary), 1)[0], strip])], 3)[0])
            i = j + 1

        elif token_type == "hr":
            strips.extend(_pad(_to_strips(Text("─" * width, style=styles.muted, overflow="crop"), width), 0))
            i += 1

        elif token_type == "html_block":
            for line in token.content.rstrip("\n").split("\n"):
                strips.extend(_to_strips(Text(line, overflow="crop"), width))
            i += 1

        else:
            i += 1

    return strips


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

class _Block:
    """One transcript entry: content source, rendered lazily into line strips."""

    __slots__ = ("kind", "idx", "before", "dirty", "lines", "height")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.idx = 0
        self.before = 0
        self.dirty = True
        self.lines: list[Strip] = []
        self.height = 0

    def render(self, width: int, styles: FeedStyle) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class _StaticBlock(_Block):
    """A finished block rendered from a single rich renderable."""

    __slots__ = ("renderable", "style_str", "pad")

    def __init__(self, kind: str, renderable: Any, style_str: str = "", pad: int = 3) -> None:
        super().__init__(kind)
        self.renderable = renderable
        self.style_str = style_str
        self.pad = pad

    def render(self, width: int, styles: FeedStyle) -> None:
        renderable = self.renderable
        if self.style_str and isinstance(renderable, Text):
            renderable = renderable.copy()
            renderable.stylize(self.style_str)
        self.lines = _pad(_to_strips(renderable, width), self.pad)
        self.height = len(self.lines)
        self.dirty = False


class _UserBlock(_Block):
    """A user turn: primary rail + 2-cell gutter, wrapped with a hanging indent.

    The widget feed's UserTurn carried ``padding: 1 0 1 2`` and a ``border-left``
    that ran the full height, so every wrapped or multi-line message kept the
    rail. Rendering the message as one rich Text only prefixed the first line;
    this wraps the body at ``width - 3`` and prefixes every rendered strip.
    """

    __slots__ = ("text",)

    RAIL_WIDTH = 3  # 1 rail cell + 2 gutter cells

    def __init__(self, text: str) -> None:
        super().__init__("user")
        self.text = text

    def render(self, width: int, styles: FeedStyle) -> None:
        # The old UserTurn used Textual's `border-left: wide`, whose glyph is
        # the thin `▎` in the primary colour — a background-filled cell reads
        # as a heavy solid bar, especially through the padding rows.
        rail = Segment("▎", Style(color=styles.primary or "yellow"))
        panel = Style.parse(f"on {styles.panel}") if styles.panel else None
        gutter = Strip([rail, Segment("  ", panel)])
        body = Text(
            self.text,
            style=_combine("bold", f"on {styles.panel}" if styles.panel else ""),
        )
        body_width = max(1, width - self.RAIL_WIDTH)

        def _row(body_strip: Optional[Strip] = None) -> Strip:
            row = Strip.join([gutter, body_strip]) if body_strip is not None else gutter
            if panel is not None:
                pad = width - row.cell_length
                if pad > 0:
                    row = Strip.join([row, Strip([Segment(" " * pad, panel)])])
            return row

        lines = [_row()]
        lines.extend(_row(strip) for strip in _to_strips(body, body_width))
        lines.append(_row())
        self.lines = lines
        self.height = len(lines)
        self.dirty = False


class _WordmarkBlock(_Block):
    """The ASCII wordmark, centered, bold primary, never wrapping.

    The variant is chosen from the width at render time (``art_fn``), so it
    re-fits on every width change without the app measuring anything.
    """

    __slots__ = ("art_fn",)

    def __init__(self, art_fn: Any) -> None:
        super().__init__("wordmark")
        self.art_fn = art_fn

    def render(self, width: int, styles: FeedStyle) -> None:
        art = self.art_fn(max(1, width)) or ""
        lines = art.splitlines() or [""]
        # One pad for every line, from the widest line: the art's rows taper
        # (letter bottoms are narrower), and padding each line to its own
        # width would shift those rows right of the ones above them.
        pad = max(0, (width - max(cell_len(line) for line in lines)) // 2)
        out: list[Strip] = []
        style = _combine("bold", styles.primary)
        for line in lines:
            out.extend(_to_strips(Text(" " * pad + line, style=style, overflow="crop"), width))
        self.lines = out
        self.height = len(out)
        self.dirty = False


class _NoteBlock(_Block):
    """Consecutive ANSI/chrome lines merged into one growing block."""

    __slots__ = ("texts",)

    def __init__(self) -> None:
        super().__init__("note")
        self.texts: list[Text] = []

    def append(self, line: Text) -> None:
        self.texts.append(line)
        self.dirty = True

    def render(self, width: int, styles: FeedStyle) -> None:
        out: list[Strip] = []
        for line in self.texts:
            out.extend(_to_strips(line, width))
        self.lines = _pad(out, 3)
        self.height = len(self.lines)
        self.dirty = False


class _ToolBlock(_Block):
    """One tool row: icon column, label, live elapsed / final duration."""

    __slots__ = ("label", "icon", "done", "is_error", "duration", "started", "frame")

    def __init__(self, name: str) -> None:
        super().__init__("tool")
        self.label = f"Preparing {name}…"
        self.icon = ""
        self.done = False
        self.is_error = False
        self.duration = 0.0
        self.started = time.monotonic()
        self.frame = " "

    def finish(self, icon: str, label: str, is_error: bool, duration: float) -> None:
        self.done = True
        self.icon = icon
        self.label = label
        self.is_error = is_error
        self.duration = duration
        self.dirty = True

    def render(self, width: int, styles: FeedStyle) -> None:
        lead = ("✗" if self.is_error else self.icon) if self.done else self.frame
        row = Text()
        row.append(lead.ljust(TOOL_ICON_WIDTH))
        row.append(self.label)
        if self.done and self.duration:
            row.append(f"  {self.duration:.1f}s", style=styles.muted)
        elif not self.done:
            elapsed = time.monotonic() - self.started
            if elapsed >= 1:
                row.append(f"  {elapsed:.0f}s", style=styles.muted)
        row.no_wrap = True
        row.overflow = "ellipsis"
        if self.is_error:
            row.stylize(styles.error)
        self.lines = _to_strips(row, width)
        self.height = len(self.lines)
        self.dirty = False


class _ReasoningBlock(_Block):
    """Model reasoning: expanded while streaming, folded once the answer starts.

    The header row is the click target that folds/unfolds the block — the
    affordance the old ``Collapsible`` widget gave for free.
    """

    __slots__ = ("buffer", "collapsed", "line_total")

    def __init__(self) -> None:
        super().__init__("reasoning")
        self.buffer = ""
        self.collapsed = False
        self.line_total = 0

    def append(self, text: str) -> None:
        self.buffer += text
        self.dirty = True

    def finish(self) -> None:
        self.line_total = len(self.buffer.strip().splitlines()) if self.buffer.strip() else 0
        self.collapsed = True
        self.dirty = True

    def toggle(self) -> None:
        """Fold or unfold in place (a click on the header row)."""
        self.collapsed = not self.collapsed
        self.dirty = True

    def _line_count(self) -> int:
        # While streaming, line_total is still 0 (it is set by finish()), so a
        # block folded mid-stream counts the buffer it is hiding.
        if self.line_total:
            return self.line_total
        return len(self.buffer.strip().splitlines()) if self.buffer.strip() else 0

    def render(self, width: int, styles: FeedStyle) -> None:
        if self.collapsed:
            count = self._line_count()
            label = f"▶ reasoning · {count} line{'s' if count != 1 else ''}"
            self.lines = _pad(_to_strips(Text(label, style=styles.secondary), width), 3)
        else:
            lines = _pad(_to_strips(Text("▼ reasoning", style=styles.secondary), width), 3)
            if self.buffer.strip():
                lines += _pad(
                    _to_strips(Text(self.buffer.strip(), style=styles.secondary), width), 5
                )
            self.lines = lines
        self.height = len(self.lines)
        self.dirty = False


class _MarkdownBlock(_Block):
    """A growing assistant response, rendered from its markdown source."""

    __slots__ = ("source",)

    def __init__(self, source: str = "") -> None:
        super().__init__("markdown")
        self.source = source

    def append(self, text: str) -> None:
        self.source += text
        self.dirty = True

    def render(self, width: int, styles: FeedStyle) -> None:
        self.lines = render_markdown_lines(self.source, width, styles)
        self.height = len(self.lines)
        self.dirty = False


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------

def _highlight_slice(strip: Strip, lo: int, hi: int, extra: Style) -> Strip:
    """Return the strip with cells [lo, hi) restyled (e.g. reversed)."""
    if lo >= hi:
        return strip
    out: list[Segment] = []
    pos = 0
    for seg in strip:
        if seg.control or not seg.text:
            out.append(seg)
            continue
        length = cell_len(seg.text)
        start, end = pos, pos + length
        pos = end
        if end <= lo or start >= hi:
            out.append(seg)
            continue
        before = max(0, lo - start)
        after = max(0, end - hi)
        keep = length - before - after
        if before > 0:
            left, seg = seg.split_cells(before)
            out.append(left)
        if keep > 0:
            mid, seg = seg.split_cells(keep)
            out.append(Segment(mid.text, (mid.style or Style()) + extra))
        if seg.text:
            out.append(seg)
    return Strip(out)


def _slice_text(strip: Strip, lo: int, hi: int) -> str:
    """Plain text of cells [lo, hi) in a strip."""
    chunks: list[str] = []
    pos = 0
    for seg in strip:
        if seg.control or not seg.text:
            continue
        length = cell_len(seg.text)
        start, end = pos, pos + length
        pos = end
        if end <= lo or start >= hi:
            continue
        before = max(0, lo - start)
        after = max(0, end - hi)
        _, mid = seg.split_cells(before)
        mid, _ = mid.split_cells(length - before - after)
        chunks.append(mid.text)
    return "".join(chunks)


# ---------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------

class VirtualFeed(ScrollView):
    """The transcript as a Line API widget: blocks rendered on demand."""

    class TextCopied(Message):
        """A drag-selection over the transcript was released with content."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    # Kinds whose end earns a blank line before the next block (opencode's
    # alwaysSeparate set).
    _SEPARATE_AFTER = ("user", "markdown", "reasoning")

    def __init__(self, styles: FeedStyle, *, wordmark_art: Any = None, **kwargs: Any) -> None:
        super().__init__(id="feed", **kwargs)
        self._styles = styles
        self._blocks: list[_Block] = []
        self._starts: list[int] = []
        self._total = 0
        self._width = 1
        self._note: Optional[_NoteBlock] = None
        self._note_lines = 0
        self._wordmark: Optional[_WordmarkBlock] = None
        self._intro: Optional[_StaticBlock] = None
        self._selection: Optional[list] = None
        self._selecting = False
        if wordmark_art is not None:
            self._wordmark = _WordmarkBlock(wordmark_art)
            self._blocks.append(self._wordmark)
            self._sync(0)

    def on_mount(self) -> None:
        super().on_mount()
        self._adopt_real_width()

    def on_resize(self, event: events.Resize) -> None:
        # The widget's own resize fires after layout with the real width, so
        # the content always tracks the viewport. The app-level resize handler
        # runs earlier and sees pre-layout geometry, which left the header one
        # frame stale on every terminal resize.
        self._adopt_real_width()

    def _adopt_real_width(self) -> None:
        width = self.scrollable_content_region.width
        if width <= 0:
            return
        width = max(1, width - (0 if self.show_vertical_scrollbar else 1))
        self.set_width(width)

    # ---------------- geometry ----------------

    def _sync(self, first: int) -> None:
        blocks, starts = self._blocks, self._starts
        if first == 0:
            start = 0
            del starts[:]
        else:
            prev = blocks[first - 1]
            start = starts[first - 1] + prev.height
            del starts[first:]
        for i in range(first, len(blocks)):
            block = blocks[i]
            block.idx = i
            start += block.before
            starts.append(start)
            start += block.height
        self._total = start
        self.virtual_size = Size(self._width, max(1, start))
        if self._anchored and not self._anchor_released:
            # Follow new content now instead of waiting for the next compositor
            # pass; the anchored flag keeps it pinned from then on.
            self.scroll_end(immediate=True, animate=False)

    def _append_block(self, block: _Block, before: int = 0) -> _Block:
        block.before = before
        block.idx = len(self._blocks)
        block.render(self._width, self._styles)
        self._blocks.append(block)
        self._starts.append(self._total + block.before)
        self._total = self._starts[-1] + block.height
        self.virtual_size = Size(self._width, max(1, self._total))
        self.refresh()
        return block

    def changed(self, block: _Block) -> None:
        """Re-render one block now (tool rows, finished reasoning)."""
        block.render(self._width, self._styles)
        self._sync(block.idx)
        self.refresh()

    def remove_block(self, block: _Block) -> None:
        """Drop one block (a hidden tool row, an orphaned "Preparing" row)."""
        idx = block.idx
        if idx >= len(self._blocks) or self._blocks[idx] is not block:
            return
        del self._blocks[idx]
        if self._note is block:
            self._note = None
            self._note_lines = 0
        self._sync(idx)
        self.refresh()

    def flush(self) -> None:
        """Re-render every block with pending content (once per frame)."""
        first = -1
        for i, block in enumerate(self._blocks):
            if block.dirty:
                block.render(self._width, self._styles)
                if first == -1:
                    first = i
        if first != -1:
            self._sync(first)
            self.refresh()

    def restyle(self, styles: FeedStyle) -> None:
        self._styles = styles
        for block in self._blocks:
            block.render(self._width, styles)
        self._sync(0)
        self.refresh()

    def set_width(self, width: int) -> None:
        width = max(1, int(width))
        if width == self._width:
            return
        self._width = width
        for block in self._blocks:
            block.render(width, self._styles)
        self._sync(0)
        self.refresh()

    # ---------------- chrome ----------------

    def set_intro(self, renderable: Any) -> None:
        if self._intro is None:
            self._intro = _StaticBlock("intro", renderable, pad=3)
            index = 1 if self._wordmark is not None else 0
            self._intro.render(self._width, self._styles)
            self._blocks.insert(index, self._intro)
            self._sync(0)
        else:
            self._intro.renderable = renderable
            self.changed(self._intro)

    # ---------------- content ----------------

    def _separate(self, kind: str) -> int:
        if not self._blocks:
            return 0
        # user / markdown / reasoning each carried a permanent margin-top in
        # the widget feed; the port dropped it and jammed everything together.
        if kind in self._SEPARATE_AFTER:
            return 1
        prev = self._blocks[-1]
        if prev.kind in ("wordmark", "intro"):
            return 0
        if prev.kind in self._SEPARATE_AFTER:
            return 1
        if prev.kind == "note" and prev.height > 1:
            return 1
        return 0

    def reset_note(self) -> None:
        """End the merge run; flush any pending lines into the block."""
        if self._note is not None and self._note.dirty:
            self._note.render(self._width, self._styles)
            self._sync(self._note.idx)
            self.refresh()
        self._note = None
        self._note_lines = 0

    def add_user(self, text: str) -> None:
        self.reset_note()
        self._append_block(_UserBlock(text), before=self._separate("user"))

    def add_note(self, line: Text) -> None:
        """Append one ANSI/plain line, merging consecutive lines into a block."""
        if self._note is not None and self._note_lines < _NOTE_MERGE_MAX_LINES:
            self._note.append(line)
            self._note_lines += 1
            return
        self.reset_note()
        block = _NoteBlock()
        block.append(line)
        self._note = block
        self._note_lines = 1
        self._append_block(block, before=self._separate("note"))

    def add_rich(self, renderable: Any, style_str: str = "") -> None:
        """A finished chrome block (Rich renderables, compaction stats)."""
        self.reset_note()
        self._append_block(_StaticBlock("note", renderable, style_str, pad=3), before=self._separate("note"))

    def add_markdown(self, source: str) -> None:
        """A finished markdown block (compaction summaries, resumed answers)."""
        self.reset_note()
        self._append_block(_MarkdownBlock(source), before=self._separate("markdown"))

    def open_tool(self, name: str) -> _ToolBlock:
        self.reset_note()
        return self._append_block(_ToolBlock(name), before=self._separate("tool"))  # type: ignore[return-value]

    def open_reasoning(self) -> _ReasoningBlock:
        self.reset_note()
        return self._append_block(_ReasoningBlock(), before=self._separate("reasoning"))  # type: ignore[return-value]

    def open_markdown(self) -> _MarkdownBlock:
        self.reset_note()
        return self._append_block(_MarkdownBlock(), before=self._separate("markdown"))  # type: ignore[return-value]

    def clear(self) -> None:
        """Drop every content block; the wordmark and intro stay."""
        self._note = None
        self._note_lines = 0
        self._blocks = [b for b in (self._wordmark, self._intro) if b is not None]
        self._sync(0)
        self.refresh()

    # ---------------- painting ----------------

    def render_line(self, y: int) -> Strip:
        # The Line API hands us the viewport row; translate it to a content
        # line with the scroll offset, or the feed paints from line 0 forever
        # and the scrollbar looks dead (Textual's Log widget does the same).
        y = int(self.scroll_offset.y) + y
        if not self._blocks or y < 0:
            return Strip.blank(self._width)
        idx = bisect.bisect_right(self._starts, y) - 1
        if idx < 0:
            return Strip.blank(self._width)
        block = self._blocks[idx]
        local = y - self._starts[idx]
        # Render the last-painted lines even while a block is dirty: flush()
        # catches up within a tick, and returning blanks here made the
        # streamed answer flash text/blank whenever anything repainted.
        if 0 <= local < block.height:
            strip = block.lines[local]
        else:
            strip = Strip.blank(self._width)
        return self._selection_strip(y, strip)

    # ---------------- selection ----------------

    def _point(self, event: Any) -> tuple[int, int]:
        x = int(event.offset.x + self.scroll_offset.x)
        y = int(event.offset.y + self.scroll_offset.y)
        return x, y

    def _selection_strip(self, y: int, strip: Strip) -> Strip:
        sel = self._selection
        if sel is None:
            return strip
        y1, x1, y2, x2 = sel
        if y1 == y2:
            if y != y1:
                return strip
            lo, hi = min(x1, x2), max(x1, x2)
        elif y < y1 or y > y2:
            return strip
        elif y == y1:
            lo, hi = x1, 1 << 30
        elif y == y2:
            lo, hi = 0, x2
        else:
            lo, hi = 0, 1 << 30
        return _highlight_slice(strip, lo, hi, Style(reverse=True))

    def _line_slice(self, y: int, lo: int, hi: int) -> str:
        if not self._blocks or y < 0 or lo >= hi:
            return ""
        idx = bisect.bisect_right(self._starts, y) - 1
        if idx < 0:
            return ""
        block = self._blocks[idx]
        local = y - self._starts[idx]
        if not (0 <= local < block.height) or block.dirty:
            return ""
        return _slice_text(block.lines[local], lo, hi)

    def _selected_text(self) -> str:
        sel = self._selection
        if sel is None:
            return ""
        y1, x1, y2, x2 = sel
        if y1 == y2:
            return self._line_slice(y1, min(x1, x2), max(x1, x2))
        parts: list[str] = []
        for y in range(y1, y2 + 1):
            if y == y1:
                lo, hi = x1, 1 << 30
            elif y == y2:
                lo, hi = 0, x2
            else:
                lo, hi = 0, 1 << 30
            parts.append(self._line_slice(y, lo, hi))
        return "\n".join(parts)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        self._selecting = True
        self.capture_mouse(True)
        x, y = self._point(event)
        self._selection = [y, x, y, x]
        event.stop()
        self.refresh()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._selecting or self._selection is None:
            return
        x, y = self._point(event)
        y1, x1 = self._selection[0], self._selection[1]
        if y >= y1:
            self._selection[2], self._selection[3] = y, x
        else:
            self._selection = [y, x, y1, x1]
        self.refresh()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._selecting:
            return
        self._selecting = False
        self.capture_mouse(False)
        selection = self._selection
        text = self._selected_text()
        self._selection = None
        if text and text.strip():
            self.refresh()
            self.post_message(self.TextCopied(text))
            return
        # A plain click (no drag) on a reasoning header folds/unfolds it — the
        # interaction the old Collapsible widget provided.
        if (
            selection is not None
            and selection[0] == selection[2]
            and selection[1] == selection[3]
        ):
            if self._toggle_reasoning_at(selection[0]):
                return
        self.refresh()

    def _toggle_reasoning_at(self, y: int) -> bool:
        """Toggle the reasoning block whose header row is at ``y``."""
        if not self._blocks or y < 0:
            return False
        idx = bisect.bisect_right(self._starts, y) - 1
        if idx < 0:
            return False
        block = self._blocks[idx]
        if not isinstance(block, _ReasoningBlock):
            return False
        if y != self._starts[idx]:
            return False  # body lines select, only the header toggles
        block.toggle()
        self.changed(block)
        return True
