"""Welcome banner and ASCII art for the CLI.

Pure display functions with no SonOfAntonCLI state dependency.
"""
import os
import shutil
from typing import TYPE_CHECKING, List

# rich is imported lazily (inside the functions that use it) rather than at
# module level; keep the type-only reference available to checkers.
if TYPE_CHECKING:
    from rich.console import Console


# =========================================================================
# ANSI building blocks for conversation display
# =========================================================================

_BOLD = "\033[1m"
_DIM = "\033[2m"
_RST = "\033[0m"


def cprint(text: str):
    """Print one ANSI-colored banner line.

    Replaced by the interactive front-end (see ``son_of_anton_tui.backend``) so
    banner output lands in the transcript; this plain form is what the
    non-interactive paths use.
    """
    try:
        print(text)
    except Exception:
        # A redirected or absent stdout must never crash a display helper.
        pass


# =========================================================================
# Skin-aware color helpers
# =========================================================================

def _skin_color(key: str, fallback: str) -> str:
    """Get a color from the active skin, or return fallback."""
    try:
        from son_of_anton_cli.skin_engine import get_active_skin
        return get_active_skin().get_color(key, fallback)
    except Exception:
        return fallback
# =========================================================================
# ASCII Art & Branding
# =========================================================================

from son_of_anton_cli import __version__ as VERSION, __release_date__ as RELEASE_DATE

# The wordmark, at two widths. One line is the one we want whenever the
# terminal is wide enough for it; the stacked form keeps a logo on narrow
# windows instead of dropping it. The README uses the stacked form because
# GitHub renders it in a fixed column. Widths: 109 and 52.
SON_OF_ANTON_AGENT_LOGO_WIDE = """███████╗  ██████╗  ███╗   ██╗      ██████╗  ███████╗       █████╗   ███╗   ██╗ ████████╗  ██████╗  ███╗   ██╗
██╔════╝ ██╔═══██╗ ████╗  ██║     ██╔═══██╗ ██╔════╝      ██╔══██╗  ████╗  ██║ ╚══██╔══╝ ██╔═══██╗ ████╗  ██║
███████╗ ██║   ██║ ██╔██╗ ██║     ██║   ██║ █████╗        ███████║  ██╔██╗ ██║    ██║    ██║   ██║ ██╔██╗ ██║
╚════██║ ██║   ██║ ██║╚██╗██║     ██║   ██║ ██╔══╝        ██╔══██║  ██║╚██╗██║    ██║    ██║   ██║ ██║╚██╗██║
███████║ ╚██████╔╝ ██║ ╚████║     ╚██████╔╝ ██║           ██║  ██║  ██║ ╚████║    ██║    ╚██████╔╝ ██║ ╚████║
╚══════╝  ╚═════╝  ╚═╝  ╚═══╝      ╚═════╝  ╚═╝           ╚═╝  ╚═╝  ╚═╝  ╚═══╝    ╚═╝     ╚═════╝  ╚═╝  ╚═══╝"""

SON_OF_ANTON_AGENT_LOGO_STACKED = """███████╗  ██████╗  ███╗   ██╗      ██████╗  ███████╗
██╔════╝ ██╔═══██╗ ████╗  ██║     ██╔═══██╗ ██╔════╝
███████╗ ██║   ██║ ██╔██╗ ██║     ██║   ██║ █████╗
╚════██║ ██║   ██║ ██║╚██╗██║     ██║   ██║ ██╔══╝
███████║ ╚██████╔╝ ██║ ╚████║     ╚██████╔╝ ██║
╚══════╝  ╚═════╝  ╚═╝  ╚═══╝      ╚═════╝  ╚═╝

 █████╗   ███╗   ██╗ ████████╗  ██████╗  ███╗   ██╗
██╔══██╗  ████╗  ██║ ╚══██╔══╝ ██╔═══██╗ ████╗  ██║
███████║  ██╔██╗ ██║    ██║    ██║   ██║ ██╔██╗ ██║
██╔══██║  ██║╚██╗██║    ██║    ██║   ██║ ██║╚██╗██║
██║  ██║  ██║ ╚████║    ██║    ╚██████╔╝ ██║ ╚████║
╚═╝  ╚═╝  ╚═╝  ╚═══╝    ╚═╝     ╚═════╝  ╚═╝  ╚═══╝"""

LOGO_WIDE_MIN_COLUMNS = 109
LOGO_STACKED_MIN_COLUMNS = 95


def pick_banner_logo(term_width: int, skin_logo: str = "") -> str:
    """Return the logo to print at *term_width*, or "" to print none.

    Rich wraps anything wider than the terminal, which turns block letters
    into rubble, so each form has a floor and below the last one the banner
    goes without a logo. A skin's own art keeps the old single floor: its
    width is the skin author's business, not ours.
    """
    if skin_logo:
        return skin_logo if term_width >= LOGO_STACKED_MIN_COLUMNS else ""
    if term_width >= LOGO_WIDE_MIN_COLUMNS:
        return SON_OF_ANTON_AGENT_LOGO_WIDE
    if term_width >= LOGO_STACKED_MIN_COLUMNS:
        return SON_OF_ANTON_AGENT_LOGO_STACKED
    return ""


def format_banner_version_label() -> str:
    """Return the version label shown in the startup banner title."""
    return f"Son of Anton Agent v{VERSION} ({RELEASE_DATE})"


# =========================================================================
# Welcome banner
# =========================================================================

def _format_context_length(tokens: int) -> str:
    """Format a token count for display (e.g. 128000 → '128K', 1048576 → '1M')."""
    if tokens >= 1_000_000:
        val = tokens / 1_000_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}M"
        return f"{val:.1f}M"
    elif tokens >= 1_000:
        val = tokens / 1_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}K"
        return f"{val:.1f}K"
    return str(tokens)


def build_welcome_banner(console: "Console", model: str, cwd: str,
                         tools: List[dict] = None,
                         session_id: str = None,
                         context_length: int = None):
    """Build and print the welcome banner (what ``/clear`` shows).

    Args:
        console: Rich Console instance.
        model: Current model name.
        cwd: Current working directory.
        tools: List of tool definitions.
        session_id: Session identifier.
        context_length: Model's context window size in tokens.
    """
    from rich.panel import Panel
    from rich.table import Table

    tools = tools or []

    # Single centred column. The old two-column grid existed to park the
    # per-category tool listings beside the model info; with those gone
    # a second column just pushes the counts away from what they describe.
    layout_table = Table.grid(padding=(0, 2))
    layout_table.add_column("body", justify="center")

    # Resolve skin colors once for the entire banner
    accent = _skin_color("banner_accent", "yellow")
    dim = _skin_color("banner_dim", "dim yellow")
    session_color = _skin_color("session_border", "default")

    # A skin may still supply hero art; the built-in diamond cluster is gone.
    try:
        from son_of_anton_cli.skin_engine import get_active_skin
        _bskin = get_active_skin()
        _hero = getattr(_bskin, "banner_hero", "") or ""
    except Exception:
        _bskin = None
        _hero = ""
    left_lines = ["", _hero, ""] if _hero else [""]
    if not (model or "").strip() or (model or "").strip().lower() == "unknown":
        # Unconfigured install: say so in red instead of a blank/"unknown"
        # slug — this is the single clearest place to tell the user what
        # is wrong and how to fix it.
        left_lines.append(
            f"[bold red]no model configured "
            f"[{dim}]— run /model or declare one in config.toml"
        )
    else:
        model_short = model.split("/")[-1] if "/" in model else model
        if model_short.endswith(".gguf"):
            model_short = model_short[:-5]
        if len(model_short) > 28:
            model_short = model_short[:25] + "..."
        ctx_str = f" [{dim}]· [{dim}]{_format_context_length(context_length)} context" if context_length else ""
        left_lines.append(f"[{accent}]{model_short}{ctx_str}")

    if os.getenv("SON_OF_ANTON_YOLO_MODE"):
        left_lines.append(f"[bold red]YOLO mode [{dim}]— all approval prompts bypassed")
    left_lines.append(f"[{dim}]{cwd}")
    if session_id:
        left_lines.append(f"[dim {session_color}]Session: {session_id}")
    left_content = "\n".join(left_lines)

    # The per-category tool listings used to live here. They were
    # already truncated ("+N more", "and N more toolsets...") at any normal
    # terminal width, so they told the user less than the counts below while
    # filling most of the screen on every launch. /tools and /toolsets show
    # the real lists on demand.
    right_lines: list[str] = []

    # MCP server count for the summary line. Probe cheaply first: the full
    # get_mcp_status() path JOINS the in-flight background plugin discovery
    # (~100ms on the startup path), so skip it when nothing declares a server.
    mcp_status = []
    try:
        from son_of_anton_cli.config import load_config as _load_cfg
        _has_native_mcp = bool((_load_cfg() or {}).get("mcp_servers"))
    except Exception:
        _has_native_mcp = True  # can't tell — take the full path
    _has_portable_mcp = False
    if not _has_native_mcp:
        try:
            from son_of_anton_cli.plugins import get_portable_mcp_server_names_nowait
            _has_portable_mcp = bool(get_portable_mcp_server_names_nowait())
        except Exception:
            _has_portable_mcp = True
    if _has_native_mcp or _has_portable_mcp:
        try:
            from tools.mcp_tool import get_mcp_status
            mcp_status = get_mcp_status()
        except Exception:
            mcp_status = []

    right_lines.append("")
    mcp_connected = sum(1 for s in mcp_status if s["connected"]) if mcp_status else 0
    summary_parts = [f"{len(tools)} tools"]
    if mcp_connected:
        summary_parts.append(f"{mcp_connected} MCP servers")
    summary_parts.append("/help for commands")
    right_lines.append(f"[{dim}]{' · '.join(summary_parts)}")

    right_content = "\n".join(right_lines)
    layout_table.add_row("\n".join([left_content, right_content]).strip("\n"))

    title_color = _skin_color("banner_title", "bold yellow")
    border_color = _skin_color("banner_border", "yellow")
    title_markup = f"[{title_color}]{format_banner_version_label()}"
    from rich.align import Align

    outer_panel = Panel(
        Align.center(layout_table),
        title=title_markup,
        border_style=border_color,
        padding=(0, 2),
    )

    console.print()
    term_width = shutil.get_terminal_size().columns
    _logo = pick_banner_logo(
        term_width,
        skin_logo=getattr(_bskin, "banner_logo", "") if _bskin else "",
    )
    if _logo:
        # Tint the wordmark with the skin's accent so it reads as brand,
        # not as flat default terminal text.  The block-char art itself is
        # unchanged; only the applied Rich style differs.
        logo_style = _skin_color("banner_accent", "yellow")
        console.print(_logo, style=logo_style)
        console.print()
    console.print(outer_panel)
