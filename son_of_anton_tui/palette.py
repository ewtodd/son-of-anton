"""Terminal-derived colours, the way opencode builds its "system" theme.

Following the terminal does not mean having only one surface.  opencode asks the
terminal for its actual default background and foreground (OSC 10/11), then
*generates* a gray ramp from that background: panels and elements are the
terminal's own colour nudged toward its opposite, so a user message, a prompt
block and the sidebar each read as a distinct surface while still belonging to
whatever theme the user runs.  The base background stays transparent, so
terminal transparency and background images survive.

This module is the same idea in Python:

* :func:`query_terminal_colors` performs the OSC round-trip (once, at startup,
  before Textual owns the terminal);
* :func:`gray_scale` and :func:`muted_text` are ports of opencode's
  ``generateGrayScale`` / ``generateMutedTextColor``
  (``packages/tui/src/theme/index.ts``);
* :func:`build_palette` assembles the Textual CSS variables.

Every function degrades to ``None`` rather than guessing: with no answer from
the terminal we simply have no generated surfaces, and the UI falls back to
rails and padding.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from typing import NamedTuple, Optional

# opencode: `background` stays transparent so terminal transparency is kept;
# only the generated surfaces are painted.
_OSC_FOREGROUND = 10
_OSC_BACKGROUND = 11

_OSC_REPLY = re.compile(
    r"rgba?:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})"
)
_HEX_REPLY = re.compile(r"#([0-9a-fA-F]{6})")


class RGB(NamedTuple):
    r: int
    g: int
    b: int

    @property
    def hex(self) -> str:
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}"


class TerminalColors(NamedTuple):
    background: RGB
    foreground: Optional[RGB]


def parse_osc_color(payload: str) -> Optional[RGB]:
    """Parse an OSC 10/11 reply into 8-bit RGB.

    Terminals answer with ``rgb:RRRR/GGGG/BBBB`` (16 bits per channel, though
    1-4 digits are all legal) and a few reply with ``#rrggbb``.  A 4-digit
    channel is scaled down by taking its high byte.
    """
    if not payload:
        return None
    match = _OSC_REPLY.search(payload)
    if match:
        channels = []
        for raw in match.groups():
            value = int(raw, 16)
            # Normalise whatever width the terminal used onto 0-255.
            scale = (1 << (4 * len(raw))) - 1
            channels.append(round(value * 255 / scale))
        return RGB(*channels)
    match = _HEX_REPLY.search(payload)
    if match:
        value = match.group(1)
        return RGB(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    return None


def query_terminal_colors(timeout: float = 0.12) -> Optional[TerminalColors]:
    """Ask the terminal for its default background (and foreground).

    Must run before Textual takes over the terminal.  Returns ``None`` whenever
    the answer isn't available — not a TTY, the terminal ignores the query, the
    platform has no termios — so callers treat generated surfaces as optional.
    """
    if os.environ.get("SON_OF_ANTON_TUI_NO_COLOR_QUERY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return None
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        import termios
        import tty
    except Exception:
        return None

    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except Exception:
        return None

    def _ask(code: int) -> Optional[RGB]:
        try:
            sys.stdout.write(f"\033]{code};?\033\\")
            sys.stdout.flush()
        except Exception:
            return None
        buffer = ""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except Exception:
                break
            if not ready:
                break
            try:
                chunk = os.read(fd, 64)
            except Exception:
                break
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
            # The reply ends with BEL or ST; stop as soon as one arrives.
            if "\007" in buffer or "\033\\" in buffer:
                break
        return parse_osc_color(buffer)

    try:
        tty.setraw(fd)
        background = _ask(_OSC_BACKGROUND)
        foreground = _ask(_OSC_FOREGROUND) if background else None
    except Exception:
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:
            pass

    if background is None:
        return None
    return TerminalColors(background=background, foreground=foreground)


def luminance(color: RGB) -> float:
    """Perceived luminance on 0-255, the weighting opencode uses."""
    return 0.299 * color.r + 0.587 * color.g + 0.114 * color.b


def polarity(background: RGB) -> str:
    """``"light"`` for a bright terminal background, else ``"dark"``."""
    return "light" if luminance(background) > 127.5 else "dark"


def gray_scale(background: RGB, is_dark: bool) -> dict[int, RGB]:
    """Twelve surfaces derived from the terminal background.

    A port of opencode's ``generateGrayScale``: each step moves the background
    40% of the way toward white (dark terminals) or black (light ones), scaled
    by the step index, so the ramp keeps the terminal's own hue instead of
    washing out to neutral gray.  Near-black and near-white backgrounds have no
    hue to preserve and fall back to a plain gray ramp.
    """
    grays: dict[int, RGB] = {}
    lum = luminance(background)
    for i in range(1, 13):
        factor = i / 12.0
        if is_dark:
            if lum < 10:
                value = int(factor * 0.4 * 255)
                r = g = b = value
            else:
                ratio = (lum + (255 - lum) * factor * 0.4) / lum
                r = min(background.r * ratio, 255)
                g = min(background.g * ratio, 255)
                b = min(background.b * ratio, 255)
        else:
            if lum > 245:
                value = int(255 - factor * 0.4 * 255)
                r = g = b = value
            else:
                ratio = (lum * (1 - factor * 0.4)) / lum
                r = max(background.r * ratio, 0)
                g = max(background.g * ratio, 0)
                b = max(background.b * ratio, 0)
        grays[i] = RGB(int(r), int(g), int(b))
    return grays


def muted_text(background: RGB, is_dark: bool) -> RGB:
    """Secondary text colour for this background (opencode's ``generateMutedTextColor``)."""
    lum = luminance(background)
    if is_dark:
        value = 180 if lum < 10 else min(int(160 + lum * 0.3), 200)
    else:
        value = 75 if lum > 245 else max(int(100 - (255 - lum) * 0.2), 60)
    return RGB(value, value, value)


def build_palette(colors: TerminalColors) -> dict[str, str]:
    """Textual CSS variables generated from the terminal's own colours.

    Mirrors the surface assignments in opencode's ``generateSystem``: panel and
    element are the second and third steps of the ramp, borders come from the
    middle of it, and the base background is left alone so the terminal shows
    through.  Accent hues are not set here — those stay on the terminal's ANSI
    palette, which the terminal themes for us.

    Text colours are deliberately left alone too.  opencode pins ``text`` to the
    queried foreground because it paints real RGBA; our text already renders as
    ``ansi_default``, which *is* the terminal's foreground and keeps following it
    if the user retints the terminal mid-session.  Pinning a hex would also
    break blends that read ``$foreground`` (Textual draws markdown table
    keylines as ``$foreground 20%``, which needs the ansi token to resolve).
    """
    background = colors.background
    is_dark = polarity(background) == "dark"
    grays = gray_scale(background, is_dark)
    return {
        "panel": grays[2].hex,
        "surface": grays[3].hex,
        "boost": grays[3].hex,
        "panel-lighten-1": grays[4].hex,
        "border-blurred": grays[6].hex,
        "border": grays[7].hex,
        "text-muted": muted_text(background, is_dark).hex,
    }
