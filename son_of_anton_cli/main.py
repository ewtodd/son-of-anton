#!/usr/bin/env python3
"""
Son of Anton CLI - Main entry point.

Usage:
    son-of-anton                     # Interactive chat (default)
    son-of-anton chat                # Interactive chat
    son-of-anton config              # View configuration
    son-of-anton sessions list       # List past sessions
    son-of-anton cron list           # List cron jobs
    son-of-anton mcp                 # Manage MCP servers
    son-of-anton gateway             # Run gateway in foreground
    son-of-anton gateway status      # Show gateway status
    son-of-anton status              # Show status of all components
    son-of-anton pause / resume      # Engage / clear the global emergency stop
    son-of-anton problem create      # Build a physics problem spec from a dataset
    son-of-anton --version           # Show version

The fork ships no imperative installation surface. There is no `setup`,
`update`, `uninstall`, `login`/`logout`, `doctor` or `gateway install`: the
deployment is declared by the NixOS or Home Manager module, `systemctl` runs
the service, and `settings` in that module owns config.toml. Commands that
mutated an install were carried over from upstream, already refused under
managed mode, and are gone.
"""

import os
import sys

# ── Startup fast-path bootstrap ─────────────────────────────────────────
# Two lines of inline path math so ``python son_of_anton_cli/main.py`` (script
# mode — sys.path[0] is son_of_anton_cli/, not the repo root) can import the
# canonical helpers; everything else lives in son_of_anton_cli._startup_fast.
_bootstrap_root = os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir))
if _bootstrap_root not in sys.path:
    sys.path.insert(0, _bootstrap_root)
from son_of_anton_cli import _startup_fast  # noqa: E402


def _exit_after_oneshot(rc: object) -> None:
    """Exit one-shot mode without letting late native finalizers change rc.

    The SIGABRT this guards against (#30387, #43055) fires in a
    native-extension finalizer during CPython's ``Py_FinalizeEx``, *after*
    the response has printed. Flush streams, shut down file logging, then
    ``os._exit`` past interpreter finalization. The ``atexit`` chain is
    deliberately skipped — several handlers re-enter native code that may
    be the abort source. Stateful cleanup is handled in ``_run_agent`` and
    ``_cleanup_oneshot_runtime``.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    try:
        logging.shutdown()
    except Exception:
        pass
    if rc is None:
        exit_code = 0
    elif isinstance(rc, int):
        exit_code = rc
    else:
        exit_code = 1
    os._exit(exit_code)


_oneshot_cleanup_done = False


def _cleanup_oneshot_runtime() -> None:
    """Best-effort process-global cleanup before one-shot hard exit.

    ``run_oneshot`` owns the agent-local cleanup (memory provider, agent.close,
    session_db.close — all in ``_run_agent``'s finally block). This mirrors the
    process-global pieces from ``cli.py:_run_cleanup()`` that would otherwise
    be skipped by ``os._exit``.
    """
    global _oneshot_cleanup_done
    if _oneshot_cleanup_done:
        return
    _oneshot_cleanup_done = True
    try:
        from tools.terminal_tool import cleanup_all_environments
        cleanup_all_environments()
    except Exception:
        pass
    try:
        from tools.async_delegation import interrupt_all
        interrupt_all(reason="oneshot shutdown")
    except Exception:
        pass
    try:
        from tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
    except BaseException:
        pass
    try:
        from agent.auxiliary_client import shutdown_cached_clients
        shutdown_cached_clients()
    except Exception:
        pass


def _run_and_exit_oneshot(
    prompt: str,
    *,
    model: object = None,
    provider: object = None,
    toolsets: object = None,
    usage_file: object = None,
) -> None:
    try:
        from son_of_anton_cli.oneshot import run_oneshot

        rc = run_oneshot(
            prompt,
            model=model,
            provider=provider,
            toolsets=toolsets,
            usage_file=usage_file,
        )
    except KeyboardInterrupt:
        rc = 130
    except SystemExit as exc:
        if exc.code is not None and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
            rc = 1
        else:
            rc = exc.code
    except BaseException:
        # Defense-in-depth. ``run_oneshot`` already converts agent failures
        # into an int return code and only re-raises KeyboardInterrupt /
        # SystemExit (handled above). Anything still escaping here means
        # ``run_oneshot`` itself malfunctioned — surface it on stderr but never
        # fall through to normal interpreter teardown, which is the exact path
        # that aborts with SIGABRT on AL2023 (the bug this routine fixes).
        import traceback
        try:
            traceback.print_exc()
        except Exception:
            pass
        rc = 1
    try:
        _cleanup_oneshot_runtime()
    finally:
        # The hard exit is the safety boundary for #43055. Even an interrupt
        # during best-effort cleanup must not fall back into interpreter
        # finalization, where the reported native SIGABRT occurs.
        _exit_after_oneshot(rc)


def _project_root_str_fast() -> str:
    return _startup_fast.project_root_str()


def _ensure_project_root_on_path_fast() -> None:
    _startup_fast.ensure_project_root_on_path()


def _set_process_title() -> None:
    """Set the process title to 'son-of-anton' so tools like 'ps', 'top', and
    'htop' show the app name instead of 'python3.xx'.

    Purely cosmetic and non-fatal: ``prctl(PR_SET_NAME)``, 15-char limit.
    """
    import ctypes

    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(15, b"son-of-anton", 0, 0, 0)  # PR_SET_NAME = 15
    except Exception:
        pass


def _try_ultrafast_version() -> bool:
    """Handle ``son-of-anton --version`` before config/logging imports."""
    return _startup_fast.try_fast_version()


_ensure_project_root_on_path_fast()

if _try_ultrafast_version():
    raise SystemExit(0)

from pathlib import Path
from typing import Optional


from son_of_anton_cli.subcommands.cron import build_cron_parser
from son_of_anton_cli.subcommands.gateway import build_gateway_parser

from son_of_anton_cli.subcommands.status import build_status_parser
from son_of_anton_cli.subcommands.pause import build_pause_parser
from son_of_anton_cli.subcommands.config import build_config_parser
from son_of_anton_cli.subcommands.rag import build_rag_parser
from son_of_anton_cli.subcommands.mcp import build_mcp_parser
from son_of_anton_cli.subcommands.problem import build_problem_parser
from son_of_anton_cli.subcommands.nuke import build_nuke_parser


# Add project root to path
PROJECT_ROOT = Path(_project_root_str_fast())
_ensure_project_root_on_path_fast()


# Load .env from ~/.son-of-anton/.env first, then project root as dev fallback.
# User-managed env files should override stale shell exports on restart.
from son_of_anton_cli.config import get_son_of_anton_home
from son_of_anton_cli.env_loader import load_son_of_anton_dotenv

load_son_of_anton_dotenv(project_env=PROJECT_ROOT / ".env")

# Bridge security.redact_secrets from config.toml → SON_OF_ANTON_REDACT_SECRETS env
# var BEFORE son_of_anton_logging imports agent.redact (which snapshots the flag at
# module-import time). Without this, config.toml's toggle is ignored because
# the setup_logging() call below imports agent.redact, which reads the env var
# exactly once. Env var in .env still wins — this is config.toml fallback only.
try:
    # Reuse read_raw_config()'s (mtime, size)-keyed cache instead of a bespoke
    # TOML parse — the SAME parse then serves son_of_anton_logging's
    # _read_logging_config and any later raw reads in this process, collapsing
    # 3-4 config.toml parses per invocation into one.
    from son_of_anton_cli.config import read_raw_config as _read_raw_early

    _cfg_path = get_son_of_anton_home() / "config.toml"
    if _cfg_path.exists():
        _early_cfg_raw = _read_raw_early() or {}
        if "SON_OF_ANTON_REDACT_SECRETS" not in os.environ:
            _early_sec_cfg = _early_cfg_raw.get("security", {})
            if isinstance(_early_sec_cfg, dict):
                _early_redact = _early_sec_cfg.get("redact_secrets")
                if _early_redact is not None:
                    os.environ["SON_OF_ANTON_REDACT_SECRETS"] = str(_early_redact).lower()
        del _early_cfg_raw
    del _cfg_path
except Exception:
    pass  # best-effort — redaction stays at default (enabled) on config errors

# Initialize centralized file logging early — all `son-of-anton` subcommands
# (chat, setup, gateway, config, etc.) write to agent.log + errors.log.
try:
    from son_of_anton_logging import setup_logging as _setup_logging

    _setup_logging(mode="cli")
except Exception:
    pass  # best-effort — don't crash the CLI if logging setup fails

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _relative_time(ts) -> str:
    """Format a timestamp as relative time (e.g., '2h ago', 'yesterday').

    Thin wrapper kept for backward compatibility; the implementation lives
    in :mod:`son_of_anton_cli.timefmt` so lightweight consumers don't have to
    import the whole CLI surface.
    """
    from son_of_anton_cli.timefmt import relative_time

    return relative_time(ts)


def _has_any_provider_configured() -> bool:
    """True when config.toml (or the environment) names an endpoint to talk to."""
    from son_of_anton_cli.config import load_config

    if os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_KEY"):
        return True
    cfg = load_config()
    model_cfg = cfg.get("model")
    if isinstance(model_cfg, dict) and any(
        str(model_cfg.get(key) or "").strip() for key in ("provider", "base_url", "api_key")
    ):
        return True
    return bool(cfg.get("custom_providers"))


def _confirm_startup_expensive_model_override(args) -> None:
    """Guard startup -m/--provider overrides before the first API call."""
    explicit_model = (getattr(args, "model", None) or "").strip()
    explicit_provider = (getattr(args, "provider", None) or "").strip()
    if not explicit_model and not explicit_provider:
        return

    try:
        from son_of_anton_cli.config import load_config
        from son_of_anton_cli.model_selection_guards import (
            combined_message,
            selection_warnings,
        )
    except Exception as exc:
        logger.warning("startup model cost guard unavailable: %s", exc)
        return

    try:
        config = load_config()
    except Exception as exc:
        logger.warning("startup model cost guard could not load config: %s", exc)
        config = {}
    if not isinstance(config, dict):
        config = {}
    model_cfg = config.get("model") or {}
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    security_cfg = config.get("security") or {}
    if not isinstance(security_cfg, dict):
        security_cfg = {}

    model = explicit_model or (model_cfg.get("default") or "").strip()
    if not model:
        return
    provider = (explicit_provider or model_cfg.get("provider") or "").strip()
    try:
        # Unified registry: cost guard + id-keyed guards (e.g. the
        # data-training-tier warning) all fire at startup too.
        warnings = selection_warnings(
            model,
            provider=provider,
            base_url=(model_cfg.get("base_url") or ""),
            api_key=(model_cfg.get("api_key") or ""),
        )
    except Exception as exc:
        logger.warning("startup model cost guard failed for %s/%s: %s", provider, model, exc)
        return
    if not warnings:
        return

    # Cost and provider-routing confirmation is intentionally independent of
    # --yolo / --accept-hooks: those flags approve local command/tool risk, not
    # paid aggregator spend or a surprising provider route.
    is_interactive = sys.stdin.isatty()
    allow_unattended_data_training = (
        security_cfg.get("allow_data_training_tiers_noninteractive") is True
    )
    if not is_interactive and allow_unattended_data_training:
        acknowledged = [
            warning for warning in warnings if warning.kind == "data_policy"
        ]
        if acknowledged:
            sys.stderr.write(combined_message(acknowledged) + "\n")
            sys.stderr.write(
                "Proceeding in non-interactive mode because "
                "security.allow_data_training_tiers_noninteractive is true.\n"
            )
            warnings = [
                warning for warning in warnings if warning.kind != "data_policy"
            ]
            if not warnings:
                return

    message = combined_message(warnings)
    if not is_interactive:
        sys.stderr.write(message + "\n")
        if any(warning.kind == "data_policy" for warning in warnings):
            sys.stderr.write(
                "To acknowledge data-training tiers for unattended runs, set "
                "security.allow_data_training_tiers_noninteractive to true "
                "in config.toml.\n"
            )
        sys.stderr.write(
            "Refusing this startup model override in non-interactive mode. "
            "Run interactively and confirm if you intend to use it.\n"
        )
        raise SystemExit(1)

    sys.stderr.write(message + "\n")
    try:
        reply = input("Use this model for this invocation? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        reply = ""
    if reply not in {"y", "yes"}:
        sys.stderr.write("Model override cancelled.\n")
        raise SystemExit(1)


def _session_status_tag(status: Optional[str]) -> str:
    """Short fixed-width tag for a session lifecycle status."""
    return {
        "complete": "done",
        "interrupted": "intr",
        "error": "err",
        "empty": "empty",
    }.get(status or "", "-")


def _annotate_session_statuses(sessions: list, session_db) -> None:
    """Attach a ``_status`` key to each session row (best-effort, cheap).

    Uses ``SessionDB.session_lifecycle_statuses`` — one indexed last-message
    lookup per listed session, never a transcript scan. On any failure the
    rows simply stay untagged and the picker renders '-' for status.
    """
    if session_db is None or not sessions:
        return
    try:
        statuses = session_db.session_lifecycle_statuses(
            [s.get("id") for s in sessions]
        )
    except Exception:
        return
    for s in sessions:
        s["_status"] = statuses.get(s.get("id"), "")


def _session_browse_picker(sessions: list, session_db=None) -> Optional[str]:
    """Interactive curses-based session browser with live search filtering.

    Shows lifecycle status (done / intr / err / empty) and message count per
    session when *session_db* is provided. With a live *session_db*, pressing
    ``d`` on a row (while the search filter is empty) prompts y/n and deletes
    the session via ``SessionDB.delete_session``.

    Returns the selected session ID, or None if cancelled.
    """
    if not sessions:
        print("No sessions found.")
        return None

    _annotate_session_statuses(sessions, session_db)

    def _delete_session(session_id: str) -> bool:
        if session_db is None:
            return False
        try:
            sessions_dir = get_son_of_anton_home() / "sessions"
        except Exception:
            sessions_dir = None
        try:
            return bool(
                session_db.delete_session(
                    session_id, sessions_dir=sessions_dir, remove_journal=True
                )
            )
        except Exception:
            return False

    # Try curses-based picker first
    try:
        import curses

        result_holder = [None]

        # Layout: [arrow 3] [title/preview flexible] [status 5] [msgs 5]
        #         [active 12] [src 6] [id 18]
        _FIXED_COLS = 3 + 5 + 2 + 5 + 2 + 12 + 6 + 18 + 6

        def _format_row(s, max_x):
            """Format a session row for display."""
            title = (s.get("title") or "").strip()
            preview = (s.get("preview") or "").strip()
            source = s.get("source", "")[:6]
            last_active = _relative_time(s.get("last_active"))
            sid = s["id"][:18]
            status = _session_status_tag(s.get("_status"))
            msgs = s.get("message_count")
            msgs_str = str(msgs) if isinstance(msgs, int) else "-"

            name_width = max(20, max_x - _FIXED_COLS)

            if title:
                name = title[:name_width]
            elif preview:
                name = preview[:name_width]
            else:
                name = sid

            return (
                f"{name:<{name_width}}  {status:<5}  {msgs_str:>5}  "
                f"{last_active:<10}  {source:<5} {sid}"
            )

        def _match(s, query):
            """Check if a session matches the search query (case-insensitive)."""
            q = query.lower()
            return (
                q in (s.get("title") or "").lower()
                or q in (s.get("preview") or "").lower()
                or q in s.get("id", "").lower()
                or q in (s.get("source") or "").lower()
            )

        def _curses_browse(stdscr):
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)  # selected
                curses.init_pair(2, curses.COLOR_YELLOW, -1)  # header
                curses.init_pair(3, curses.COLOR_CYAN, -1)  # search
                curses.init_pair(4, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)  # dim
                curses.init_pair(5, curses.COLOR_RED, -1)  # error/delete

            cursor = 0
            scroll_offset = 0
            search_text = ""
            confirm_delete = None  # session dict pending y/n confirmation
            flash = ""  # one-frame notice (e.g. "deleted <title>")
            filtered = list(sessions)

            def _status_attr(status):
                if not curses.has_colors():
                    return curses.A_NORMAL
                return {
                    "complete": curses.color_pair(1),
                    "interrupted": curses.color_pair(2),
                    "error": curses.color_pair(5),
                    "empty": curses.color_pair(4),
                }.get(status or "", curses.A_NORMAL)

            while True:
                stdscr.clear()
                max_y, max_x = stdscr.getmaxyx()
                if max_y < 5 or max_x < 40:
                    # Terminal too small
                    try:
                        stdscr.addstr(0, 0, "Terminal too small")
                    except curses.error:
                        pass
                    stdscr.refresh()
                    stdscr.getch()
                    return

                # Header line
                if search_text:
                    header = f"  Browse sessions — filter: {search_text}█"
                    header_attr = curses.A_BOLD
                    if curses.has_colors():
                        header_attr |= curses.color_pair(3)
                else:
                    header = (
                        "  Browse sessions — ↑↓ navigate  Enter select"
                        "  Type to filter  Esc quit"
                    )
                    header_attr = curses.A_BOLD
                    if curses.has_colors():
                        header_attr |= curses.color_pair(2)
                try:
                    stdscr.addnstr(0, 0, header, max_x - 1, header_attr)
                except curses.error:
                    pass

                # Column header line
                name_width = max(20, max_x - _FIXED_COLS)
                col_header = (
                    f"   {'Title / Preview':<{name_width}}  {'Stat':<5}  "
                    f"{'Msgs':>5}  {'Active':<10}  {'Src':<5} {'ID'}"
                )
                try:
                    dim_attr = (
                        curses.color_pair(4) if curses.has_colors() else curses.A_DIM
                    )
                    stdscr.addnstr(1, 0, col_header, max_x - 1, dim_attr)
                except curses.error:
                    pass

                # Compute visible area
                visible_rows = max_y - 4  # header + col header + blank + footer
                visible_rows = max(visible_rows, 1)

                # Clamp cursor and scroll
                if not filtered:
                    try:
                        msg = "  No sessions match the filter."
                        stdscr.addnstr(3, 0, msg, max_x - 1, curses.A_DIM)
                    except curses.error:
                        pass
                else:
                    if cursor >= len(filtered):
                        cursor = len(filtered) - 1
                    cursor = max(cursor, 0)
                    if cursor < scroll_offset:
                        scroll_offset = cursor
                    elif cursor >= scroll_offset + visible_rows:
                        scroll_offset = cursor - visible_rows + 1

                    for draw_i, i in enumerate(
                        range(
                            scroll_offset,
                            min(len(filtered), scroll_offset + visible_rows),
                        )
                    ):
                        y = draw_i + 3
                        if y >= max_y - 1:
                            break
                        s = filtered[i]
                        arrow = " → " if i == cursor else "   "
                        row = arrow + _format_row(s, max_x - 3)
                        attr = curses.A_NORMAL
                        if i == cursor:
                            attr = curses.A_BOLD
                            if curses.has_colors():
                                attr |= curses.color_pair(1)
                        try:
                            stdscr.addnstr(y, 0, row, max_x - 1, attr)
                            if i != cursor:
                                # Recolor the status tag column in place.
                                status = s.get("_status")
                                tag = _session_status_tag(status)
                                tag_x = 3 + max(20, (max_x - 3) - _FIXED_COLS) + 2
                                if tag_x + 5 < max_x - 1:
                                    stdscr.addnstr(
                                        y, tag_x, f"{tag:<5}", 5, _status_attr(status)
                                    )
                        except curses.error:
                            pass

                # Footer
                footer_y = max_y - 1
                footer_attr = (
                    curses.color_pair(4) if curses.has_colors() else curses.A_DIM
                )
                if confirm_delete is not None:
                    label = (
                        (confirm_delete.get("title") or "").strip()
                        or (confirm_delete.get("preview") or "").strip()
                        or confirm_delete["id"]
                    )
                    if len(label) > 40:
                        label = label[:37] + "..."
                    footer = f"  Delete session '{label}'? [y/N]"
                    footer_attr = curses.A_BOLD
                    if curses.has_colors():
                        footer_attr |= curses.color_pair(5)
                elif flash:
                    footer = f"  {flash}"
                    flash = ""
                else:
                    if filtered:
                        footer = f"  {cursor + 1}/{len(filtered)} sessions"
                        if len(filtered) < len(sessions):
                            footer += f" (filtered from {len(sessions)})"
                    else:
                        footer = f"  0/{len(sessions)} sessions"
                    if session_db is not None and not search_text:
                        footer += "   d delete"
                try:
                    stdscr.addnstr(footer_y, 0, footer, max_x - 1, footer_attr)
                except curses.error:
                    pass

                stdscr.refresh()
                key = stdscr.getch()

                if confirm_delete is not None:
                    # y/n confirmation mode — only an explicit 'y' deletes.
                    target = confirm_delete
                    confirm_delete = None
                    if key in {ord("y"), ord("Y")}:
                        if _delete_session(target["id"]):
                            sessions[:] = [
                                s for s in sessions if s["id"] != target["id"]
                            ]
                            filtered = (
                                [s for s in sessions if _match(s, search_text)]
                                if search_text
                                else list(sessions)
                            )
                            flash = "Deleted."
                            if not sessions:
                                return
                        else:
                            flash = "Delete failed."
                    continue

                if key in {curses.KEY_UP,}:
                    if filtered:
                        cursor = (cursor - 1) % len(filtered)
                elif key in {curses.KEY_DOWN,}:
                    if filtered:
                        cursor = (cursor + 1) % len(filtered)
                elif key in {curses.KEY_ENTER, 10, 13}:
                    if filtered:
                        result_holder[0] = filtered[cursor]["id"]
                    return
                elif key == 27:  # Esc
                    if search_text:
                        # First Esc clears the search
                        search_text = ""
                        filtered = list(sessions)
                        cursor = 0
                        scroll_offset = 0
                    else:
                        # Second Esc exits
                        return
                elif key in {curses.KEY_BACKSPACE, 127, 8}:
                    if search_text:
                        search_text = search_text[:-1]
                        if search_text:
                            filtered = [s for s in sessions if _match(s, search_text)]
                        else:
                            filtered = list(sessions)
                        cursor = 0
                        scroll_offset = 0
                elif key == ord("q") and not search_text:
                    return
                elif (
                    key == ord("d")
                    and not search_text
                    and session_db is not None
                    and filtered
                ):
                    # 'd' only acts as delete when the filter is empty —
                    # while a search is active it types into the query below.
                    confirm_delete = filtered[cursor]
                elif 32 <= key <= 126:
                    # Printable character → add to search filter
                    search_text += chr(key)
                    filtered = [s for s in sessions if _match(s, search_text)]
                    cursor = 0
                    scroll_offset = 0

        curses.wrapper(_curses_browse)
        return result_holder[0]

    except Exception:
        pass

    # Fallback: numbered list when the interactive picker can't run. Shows
    # the same status/message-count columns but has no delete support.
    print("\n  Browse sessions  (enter number to resume, q to cancel)\n")
    for i, s in enumerate(sessions):
        title = (s.get("title") or "").strip()
        preview = (s.get("preview") or "").strip()
        label = title or preview or s["id"]
        if len(label) > 50:
            label = label[:47] + "..."
        last_active = _relative_time(s.get("last_active"))
        src = s.get("source", "")[:6]
        status = _session_status_tag(s.get("_status"))
        msgs = s.get("message_count")
        msgs_str = str(msgs) if isinstance(msgs, int) else "-"
        print(
            f"  {i + 1:>3}. {label:<50}  {status:<5}  {msgs_str:>5}  "
            f"{last_active:<10}  {src}"
        )

    while True:
        try:
            val = input(f"\n  Select [1-{len(sessions)}]: ").strip()
            if not val or val.lower() in {"q", "quit", "exit"}:
                return None
            idx = int(val) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx]["id"]
            print(f"  Invalid selection. Enter 1-{len(sessions)} or q to cancel.")
        except ValueError:
            print("  Invalid input. Enter a number or q to cancel.")
        except (KeyboardInterrupt, EOFError):
            print()
            return None


def _resolve_workspace_key() -> Optional[str]:
    """The current workspace identity for cwd-scoped resume.

    Git repo root when CWD is inside a repo (so all sessions across its
    subdirs/worktrees group together), else the CWD itself. Returns None when
    neither can be determined — callers fall back to the global MRU then.
    """
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return os.path.abspath(result.stdout.strip())
    except Exception:
        pass
    try:
        return os.getcwd()
    except Exception:
        return None


def _resolve_workspace_session(source: str = "cli") -> Optional[str]:
    """Most recently-used session in the *current* workspace, or ``None``.

    Workspace-scoped only — no global fallback. Bare ``-c`` uses this so a
    session from another directory is never resumed by accident; a miss means
    the caller starts a fresh session.
    """
    db = None
    try:
        from son_of_anton_state import SessionDB

        db = SessionDB()
        ws_key = _resolve_workspace_key()
        if not ws_key:
            return None
        sessions = db.search_sessions(source=source, limit=1, workspace_key=ws_key)
        return sessions[0]["id"] if sessions else None
    except Exception:
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _resolve_last_session(source: str = "cli") -> Optional[str]:
    """Look up the most recently-used session ID for a source.

    Scoped to the current workspace first (git repo root, else cwd) so
    ``son-of-anton -c`` from repo A continues repo A's last session rather than the
    global MRU. Falls back to the unscoped MRU when no session matches the
    current workspace, preserving the old behaviour for fresh directories.
    """
    db = None
    try:
        from son_of_anton_state import SessionDB

        db = SessionDB()
        ws_key = _resolve_workspace_key()
        if ws_key:
            sessions = db.search_sessions(source=source, limit=1, workspace_key=ws_key)
            if sessions:
                return sessions[0]["id"]
        # Fallback: global MRU for this source.
        sessions = db.search_sessions(source=source, limit=1)
        return sessions[0]["id"] if sessions else None
    except Exception:
        pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return None


def _resolve_session_by_name_or_id(name_or_id: str) -> Optional[str]:
    """Resolve a session name (title) or ID to a session ID.

    - If it looks like a session ID (contains underscore + hex), try direct lookup first.
    - Otherwise, treat it as a title and use resolve_session_by_title (auto-latest).
    - Falls back to the other method if the first doesn't match.
    - If the resolved session is a compaction root, follow the chain forward
      to the latest continuation. Users who remember the old root ID (e.g.
      from an exit summary printed before the bug fix, or from notes) get
      resumed at the live tip instead of a stale parent with no messages.
    """
    db = None
    try:
        from son_of_anton_state import SessionDB

        db = SessionDB()

        # Try as exact session ID first
        session = db.get_session(name_or_id)
        resolved_id: Optional[str] = None
        if session:
            resolved_id = session["id"]
        else:
            # Try as title (with auto-latest for lineage)
            resolved_id = db.resolve_session_by_title(name_or_id)

        if resolved_id:
            # Project forward through compaction chain so resumes land on
            # the live tip instead of a dead compacted parent.
            try:
                resolved_id = db.get_compaction_tip(resolved_id) or resolved_id
            except Exception:
                pass

        return resolved_id
    except Exception:
        pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return None


def _create_titled_session(title: str) -> Optional[str]:
    """Create a fresh session with the given title; return its session id.

    Used by ``chat -c <title> --create-if-missing`` (#86794): programmatic
    callers (plugins, scripts) that want "send to this named thread, making
    it if needed" get a deterministic outcome instead of a silent no-op.

    The session id follows the same timestamp+uuid shape the CLI uses for a
    brand-new session; the title is recorded with user provenance so
    auto-titling never overwrites it.
    """
    db = None
    try:
        import uuid as _uuid

        from son_of_anton_state import SessionDB

        now = datetime.now()
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")
        short_uuid = _uuid.uuid4().hex[:6]
        new_session_id = f"{timestamp_str}_{short_uuid}"

        db = SessionDB()
        db.create_session(new_session_id, source="cli")
        db.set_session_title(new_session_id, title)
        return new_session_id
    except Exception:
        # Programmatic callers (the #86794 use case) rely on --create-if-missing
        # being deterministic; swallow the failure to keep the error path simple,
        # but log the underlying cause so it lands in errors.log and stays
        # debuggable (DB lock, I/O error, import error — all otherwise invisible).
        logger.exception("Failed to create titled session %r", title)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _resolve_continue_arg(args) -> None:
    """Resolve ``-c/--continue`` into ``args.resume``.

    Handles both forms:
    - ``-c <name>``: resolve by title/ID. On miss, fail loudly on **stderr**
      (exit 1) so programmatic callers see the error even under quiet mode
      (#86794); with ``--create-if-missing``, create a fresh titled session
      and resume into it instead.
    - bare ``-c``: continue the most recent session in the current workspace
      (git repo root, else cwd). No global fallback — a workspace with no
      previous session starts fresh with a warning.
    """
    continue_val = getattr(args, "continue_last", None)
    if continue_val and not getattr(args, "resume", None):
        if isinstance(continue_val, str):
            # -c "session name" — resolve by title or ID
            resolved = _resolve_session_by_name_or_id(continue_val)
            if resolved:
                args.resume = resolved
            elif getattr(args, "create_if_missing", False):
                # --create-if-missing: no session matches the title — create a
                # new session with that title and proceed. This is the
                # programmatic-caller primitive ("send to this named thread,
                # making it if needed"); without it a background/quiet send to
                # a not-yet-existing named session silently no-ops (#86794).
                new_sid = _create_titled_session(continue_val)
                if new_sid:
                    args.resume = new_sid
                else:
                    print(
                        f"No session found matching '{continue_val}' and "
                        "a new titled session could not be created.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            else:
                print(f"No session found matching '{continue_val}'.", file=sys.stderr)
                print(
                    "Use 'son-of-anton sessions list' to see available sessions, or "
                    "pass --create-if-missing to start a new session with that title.",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            # -c with no argument — the most recent session in THIS workspace
            # (git repo root, else cwd). Deliberately no terminal-breadcrumb or
            # global fallback: resuming another directory's conversation from a
            # bare -c is never what the user meant. A workspace with no
            # previous session starts a fresh one, with a warning.
            if getattr(args, "create_if_missing", False):
                # --create-if-missing only makes sense with a named session;
                # with a bare -c there is nothing to create, so surface the
                # no-op to programmatic callers instead of silently ignoring it.
                print(
                    "--create-if-missing requires a session name: "
                    "`-c <name> --create-if-missing`",
                    file=sys.stderr,
                )
            last_id = _resolve_workspace_session(source="cli")
            if last_id:
                args.resume = last_id
            else:
                workspace = _resolve_workspace_key() or os.getcwd()
                print(
                    f"Warning: no previous session in this workspace "
                    f"({workspace}) — starting a new one. "
                    f"Use 'son-of-anton -c <name>' to resume a session by name.",
                    file=sys.stderr,
                )


def cmd_chat(args):
    """Run interactive chat CLI."""
    _apply_safe_mode(args)

    # --in DIR: run in DIR. Must happen before any session resolution so the
    # workspace-scoped "latest"/-c lookups key off DIR, and it pins the
    # session there — an explicit --in wins over a resumed session's
    # recorded cwd (so the restore step below is skipped).
    in_dir = getattr(args, "in_dir", None)
    if in_dir:
        _target_dir = os.path.abspath(os.path.expanduser(in_dir))
        if not os.path.isdir(_target_dir):
            print(f"Error: --in directory not found: {in_dir}")
            sys.exit(1)
        try:
            os.chdir(_target_dir)
        except OSError as e:
            print(f"Error: cannot enter --in directory {in_dir}: {e}")
            sys.exit(1)
        args.no_restore_cwd = True

    # --resume latest: keyword for "most recent session" — same resolution
    # as `-c` with no name (workspace-scoped MRU, then global fallback).
    # The keyword wins over a session literally titled "latest"; that
    # session stays reachable via its ID or `-c latest` (title match).
    _resume_raw = getattr(args, "resume", None)
    if isinstance(_resume_raw, str) and _resume_raw.strip().lower() == "latest":
        _last_id = _resolve_last_session(source="cli")
        if _last_id:
            args.resume = _last_id
        else:
            print("No previous CLI session found to resume.")
            print("Use 'son-of-anton sessions list' to see available sessions.")
            sys.exit(1)

    # Resolve --continue into --resume with the latest session or by name
    _resolve_continue_arg(args)

    # --pickup: resume the session most recently handed off or relayed from
    # the messaging chat; the TUI ends the relaying as it starts. A headless
    # worker mid-turn on it yields first, and the TUI continues its turn.
    if getattr(args, "pickup", False):
        if getattr(args, "query", None) or getattr(args, "query_file", None):
            print("Error: --pickup opens the TUI; it does not take a query.")
            sys.exit(2)
        from son_of_anton_cli.relay_host import resolve_pickup

        _picked = resolve_pickup()
        if not _picked:
            sys.exit(1)
        args.resume = _picked

    # --resume @claude / @codex / @opencode: import a foreign session and
    # resume the newly created Son of Anton session.
    _resume_foreign = getattr(args, "resume", None)
    if isinstance(_resume_foreign, str) and _resume_foreign.strip().lower() in (
        "@claude",
        "@codex",
        "@opencode",
    ):
        from son_of_anton_cli.foreign_sessions import (
            import_foreign_session,
            pick_foreign_session,
        )

        _foreign_source = _resume_foreign.strip().lower().lstrip("@")
        _picked = pick_foreign_session(_foreign_source)
        if _picked is None:
            sys.exit(1)
        try:
            _imported_id = import_foreign_session(
                _picked.source, _picked.path, foreign_id=_picked.session_id
            )
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
        print(f"✓ Imported as {_imported_id} — resuming it now.")
        print(f"  (later: son-of-anton --resume {_imported_id})")
        args.resume = _imported_id

    # Resolve --resume by title if it's not a direct session ID
    resume_val = getattr(args, "resume", None)
    if resume_val:
        resolved = _resolve_session_by_name_or_id(resume_val)
        if resolved:
            args.resume = resolved
        # If resolution fails, keep the original value — _init_agent will
        # report "Session not found" with the original input

    # Session<->workspace binding: cd back into a resumed session's recorded cwd
    # so it resumes in the repo it belonged to. Opt out with --no-restore-cwd;
    # skipped under --worktree (that path owns its own dir). Best-effort — a
    # missing dir warns and stays put rather than failing the resume.
    if (
        getattr(args, "resume", None)
        and not getattr(args, "no_restore_cwd", False)
        and not getattr(args, "worktree", False)
    ):
        _resume_db = None
        try:
            from son_of_anton_state import SessionDB

            _resume_db = SessionDB()
            _saved_cwd = ((_resume_db.get_session(args.resume) or {}).get("cwd") or "").strip()
            if _saved_cwd and not os.path.isdir(_saved_cwd):
                print(f"⚠ session's recorded dir is gone ({_saved_cwd}); staying in {os.getcwd()}")
            elif _saved_cwd and os.path.realpath(_saved_cwd) != os.path.realpath(os.getcwd()):
                os.chdir(_saved_cwd)
                print(f"↪ restored workspace dir: {_saved_cwd}")
        except Exception:
            pass  # never let cwd-restore break a resume
        finally:
            if _resume_db is not None:
                try:
                    _resume_db.close()
                except Exception:
                    pass

    # First-run guard: check if any provider is configured before launching
    if not _has_any_provider_configured():
        print()
        print(
            "It looks like Son of Anton isn't configured yet -- no API keys or providers found."
        )
        print()
        print(
            "  Declare a provider under model in config.toml (the Nix module's\n"
            "  settings), with its key in .env, then start again."
        )
        print()
        sys.exit(1)

    # --yolo: bypass all dangerous command approvals.
    # Also set in main() before _prepare_agent_startup() — that is the
    # authoritative site because it runs before tool imports freeze
    # _YOLO_MODE_FROZEN.  This redundant set is a safety net for callers
    # that invoke cmd_chat directly (e.g. subcommand dispatch).
    if getattr(args, "yolo", False):
        os.environ["SON_OF_ANTON_YOLO_MODE"] = "1"

    # --ignore-user-config: make load_cli_config() / load_config() skip the
    # user's ~/.son-of-anton/config.toml and return built-in defaults. Set BEFORE
    # importing cli (which runs `CLI_CONFIG = load_cli_config()` at module
    # import time). Credentials in .env are still loaded — this flag only
    # ignores behavioral/config settings.
    if getattr(args, "ignore_user_config", False):
        os.environ["SON_OF_ANTON_IGNORE_USER_CONFIG"] = "1"

    # --ignore-rules: skip auto-injection of AGENTS.md/SOUL.md/.cursorrules
    # (rules) and memory entries.
    # Maps to AIAgent(skip_context_files=True, skip_memory=True).
    if getattr(args, "ignore_rules", False):
        os.environ["SON_OF_ANTON_IGNORE_RULES"] = "1"

    # --source: tag session source for filtering (e.g. 'tool' for third-party integrations)
    if getattr(args, "source", None):
        os.environ["SON_OF_ANTON_SESSION_SOURCE"] = args.source

    _confirm_startup_expensive_model_override(args)

    # Import and run the CLI
    from cli import main as cli_main

    # --query-file: read the single query from a file (or stdin via '-') so
    # callers never have to shell-quote message bodies. This is the transport
    # the Bot Mode DM protocol uses — interpolating arbitrary text into a
    # double-quoted shell argument truncates on quotes and executes $(...)
    # (see tools/bot_mode_probe.py).
    _qfile = getattr(args, "query_file", None)
    if _qfile:
        if args.query:
            # argparse's mutually-exclusive group catches the normal CLI path;
            # this guards programmatic callers that fill the namespace directly.
            print("Error: -q/--query and --query-file are mutually exclusive", file=sys.stderr)
            sys.exit(2)
        try:
            if _qfile == "-":
                args.query = sys.stdin.read()
            else:
                with open(_qfile, "r", encoding="utf-8", errors="replace") as _fh:
                    args.query = _fh.read()
        except OSError as _e:
            print(f"Error: cannot read --query-file {_qfile}: {_e}", file=sys.stderr)
            sys.exit(2)
        if not (args.query or "").strip():
            print(f"Error: --query-file {_qfile} is empty", file=sys.stderr)
            sys.exit(2)

    # Build kwargs from args
    kwargs = {
        "model": args.model,
        "provider": getattr(args, "provider", None),
        "reasoning": getattr(args, "reasoning", None),
        "toolsets": args.toolsets,
        "verbose": getattr(args, "verbose", None),
        "quiet": getattr(args, "quiet", False),
        "query": args.query,
        "image": getattr(args, "image", None),
        "resume": getattr(args, "resume", None),
        "worktree": getattr(args, "worktree", False),
        "checkpoints": getattr(args, "checkpoints", False),
        "pass_session_id": getattr(args, "pass_session_id", False),
        "max_turns": getattr(args, "max_turns", None),
        "run_budget": getattr(args, "run_budget", None),
        "ignore_rules": getattr(args, "ignore_rules", False) or getattr(args, "safe_mode", False),
        "ignore_user_config": getattr(args, "ignore_user_config", False) or getattr(args, "safe_mode", False),
        "compact": getattr(args, "compact", False),
        "pickup": bool(getattr(args, "pickup", False)),
    }
    # Filter out None values
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    try:
        cli_main(**kwargs)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_gateway(args):
    """Gateway management commands."""
    from son_of_anton_cli.gateway import gateway_command

    gateway_command(args)


# ─────────────────────────────────────────────────────────────────────────────
# Auxiliary model configuration
#
# Son of Anton uses lightweight "auxiliary" models for side tasks (vision analysis,
# context compaction, web extraction, session search, etc.). Each task has
# its own provider+model pair in config.toml under `auxiliary.<task>`.
#
# The UI lives behind "Configure auxiliary models..." at the bottom of the
# `son-of-anton model` provider picker. It does NOT re-run credential setup — it
# only routes already-authenticated providers to specific aux tasks. Users
# configure new providers through the normal `son-of-anton model` flow first.
# ─────────────────────────────────────────────────────────────────────────────

# (task_key, display_name, short_description)
_AUX_TASKS: list[tuple[str, str, str]] = [
    ("vision", "Vision", "image/screenshot analysis"),
    ("compaction", "Compaction", "context summarization"),
    ("web_extract", "Web extract", "web page summarization"),
    ("approval", "Approval", "smart command approval"),
    ("mcp", "MCP", "MCP tool reasoning"),
    ("title_generation", "Title generation", "session titles"),
    ("memory_query_rewrite", "Memory query rewrite", "memory retrieval queries"),
]

# The main.py decomposition moved the sessions/update command implementations
# into their own modules, but main.py still re-exports their surface so
# argparse wiring and test monkeypatches on son_of_anton_cli.main.<name> keep
# resolving unchanged. Importing those modules eagerly costs ~50ms on
# every `son-of-anton` invocation, including fast paths like `son-of-anton --version`
# that never run a subcommand. Resolve the re-exports through the module
# __getattr__ below instead, so each module is only imported when one of its
# names is actually touched. Monkeypatching keeps working: patch.object sets
# a real module attribute, which shadows __getattr__.
_LAZY_COMMAND_EXPORTS = {
    "son_of_anton_cli.sessions_cmd": (
        "cmd_sessions",
    ),
}

_LAZY_COMMAND_ATTR_TO_MODULE = {
    attr: module for module, attrs in _LAZY_COMMAND_EXPORTS.items() for attr in attrs
}


def _self():
    """This module, for attribute access at call time.

    Bare-name global lookups inside this module do not go through the PEP 562
    __getattr__ below, so internal callers of the lazily re-exported names use
    _self().<name> instead. That resolves the lazy re-export on first use and
    keeps monkeypatches on son_of_anton_cli.main.<name> working, exactly like a
    globals lookup did. ``sys`` is imported locally because some tests patch
    this module's ``sys`` attribute.
    """
    import sys as _sys

    return _sys.modules[__name__]


def __getattr__(name):
    """Defer the command-module imports until first read."""
    module = _LAZY_COMMAND_ATTR_TO_MODULE.get(name)
    if module is not None:
        import importlib

        value = getattr(importlib.import_module(module), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def cmd_status(args):
    """Show status of all components."""
    from son_of_anton_cli.status import show_status

    show_status(args)


def cmd_cron(args):
    """Cron job management."""
    from son_of_anton_cli.cron import cron_command

    cron_command(args)


def cmd_config(args):
    """Configuration management."""
    from son_of_anton_cli.config import config_command

    config_command(args)


def _size_delta_label(saved_mb: float) -> str:
    """Human label for a before/after database size delta, in MB.

    A negative delta means the file GREW — concurrent session writes during a
    long optimize can outweigh what the rebuild freed. Printing
    "reclaimed -163.0 MB" for that reads as data loss, so say "grew by"
    instead.
    """
    if saved_mb >= 0:
        return f"reclaimed {saved_mb:.1f} MB"
    return f"grew by {-saved_mb:.1f} MB"


def _coalesce_session_name_args(argv: list) -> list:
    """Join unquoted multi-word session names after -c/--continue and -r/--resume.

    When a user types ``son-of-anton -c Pokemon Agent Dev`` without quoting the
    session name, argparse sees three separate tokens.  This function merges
    them into a single argument so argparse receives
    ``['-c', 'Pokemon Agent Dev']`` instead.

    Tokens are collected after the flag until we hit another flag (``-*``)
    or a known top-level subcommand.
    """
    # Exactly the top-level subcommands this fork registers. A name in here
    # that is not a real command is not harmless: it ends the session name
    # early, so `son-of-anton -c auth notes` would lose everything from "auth"
    # on. This list carried eleven commands the fork does not have.
    _SUBCOMMANDS = {
        "chat",
        "config",
        "cron",
        "gateway",
        "mcp",
        "nuke",
        "pause",
        "problem",
        "resume",
        "sessions",
        "status",
    }
    _SESSION_FLAGS = {"-c", "--continue", "-r", "--resume"}

    result = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _SESSION_FLAGS:
            result.append(token)
            i += 1
            # Collect subsequent non-flag, non-subcommand tokens as one name
            parts: list = []
            while (
                i < len(argv)
                and not argv[i].startswith("-")
                and argv[i] not in _SUBCOMMANDS
            ):
                parts.append(argv[i])
                i += 1
            if parts:
                result.append(" ".join(parts))
        else:
            result.append(token)
            i += 1
    return result


def cmd_problem(args, parser=None):
    """Create or run a physics problem spec."""
    action = getattr(args, "problem_action", None)

    if action == "create":
        from autophysicist.spec_builder import run as run_spec_builder

        return run_spec_builder(args, parser)

    if action == "run":
        from autophysicist.run import render_report, run_problem

        workspace = run_problem(
            args.spec,
            mode="physics",
            max_iterations=getattr(args, "max_iterations", None),
            script_timeout=getattr(args, "script_timeout", None),
            token_budget=getattr(args, "token_budget", None),
            workspace_root=getattr(args, "workspace", None),
        )
        print(render_report(workspace, "physics"))
        return 0

    print(
        "usage: son-of-anton problem create --data PATH --goal TEXT -o FILE\n"
        "       son-of-anton problem run SPEC\n"
        "Run 'son-of-anton problem <action> --help' for the full options."
    )
    return 2


# Top-level subcommands that argparse knows about WITHOUT running plugin
# discovery.  Used to short-circuit eager plugin imports (which can take
# 500ms+ pulling in google.cloud.pubsub_v1, aiohttp, grpc, etc.) when the
# user's invocation clearly doesn't need any plugin-registered subcommand.
#
# Keep this in sync with the ``subparsers.add_parser("NAME", ...)`` calls
# below in ``main()``. Missing an entry here only costs a one-time
# discovery; extra entries here would let a plugin command silently fail
# to parse.
_BUILTIN_SUBCOMMANDS = frozenset(
    {
        "chat", "config", "cron", "gateway", "mcp",
        "nuke", "pause", "problem", "resume", "sessions", "status",
        # Help-ish invocations — plugin commands not being listed in
        # top-level --help is an acceptable trade-off for skipping an
        # expensive eager import of every bundled plugin module.
        "help",
    }
)


# Top-level flags that take a value. Needed by ``_first_positional_argv``
# so that in ``son-of-anton -m gpt5 chat``, ``gpt5`` is correctly skipped as a
# flag value rather than misclassified as a subcommand. Kept in sync with
# the top-level flags declared in ``son_of_anton_cli/_parser.py``.
#
# Correctness-safe either way: missing an entry here only makes the
# fast-path bail out too eagerly (we run plugin discovery when we didn't
# need to); extra entries would make us skip a real positional.
_TOP_LEVEL_VALUE_FLAGS = frozenset(
    {
        "-z", "--oneshot",
        "-m", "--model",
        "--provider",
        "-t", "--toolsets",
        "-r", "--resume",
        "--usage-file",
        "--in",
        # ``-c / --continue`` is nargs='?' (optional value). Treat it as
        # value-taking: if the next token is a subcommand-looking word
        # the user almost certainly meant it as the session name, and
        # either interpretation keeps us on the safe side.
        "-c", "--continue",
    }
)


def _first_positional_index(argv: list) -> int | None:
    """Index of the first non-flag, non-flag-value token in *argv*, or None.

    Handles ``--`` (everything after it is positional) and ``--flag=value``
    (its value is inline, so the token is skipped whole).
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return i + 1 if i + 1 < len(argv) else None
        if tok.startswith("-"):
            # ``--flag=value`` carries its value inline — single token.
            if "=" in tok:
                i += 1
                continue
            if tok in _TOP_LEVEL_VALUE_FLAGS and i + 1 < len(argv):
                i += 2
                continue
            i += 1
            continue
        return i
    return None


def _first_positional_argv() -> str | None:
    """Return the first non-flag, non-flag-value token in ``sys.argv[1:]``.

    Used by ``main()`` to decide whether plugin discovery has to run at
    argparse-setup time. Handles common invocations like
    ``son-of-anton -m gpt5 --provider openai chat "msg"`` by skipping the
    values attached to known top-level flags.

    Does NOT fully simulate argparse — unknown ``--foo=bar`` / ``--foo
    bar`` flags degrade gracefully (``bar`` may be wrongly classified as
    a positional, which at worst forces a one-time plugin discovery).
    """
    argv = sys.argv[1:]
    idx = _first_positional_index(argv)
    return argv[idx] if idx is not None else None


def _rewrite_bare_prompt(argv: list, known_commands) -> list:
    """Route a leading bare prompt to ``chat -q``.

    ``son-of-anton "hello"`` and ``son-of-anton chat -q hello`` are the same
    single-query mode, but the parser only declares the latter: subparsers
    make the first positional a required command choice. A first positional
    that is not a registered subcommand (built-in or plugin) is treated as
    the prompt, together with any immediately following non-flag tokens, so
    unquoted multi-word prompts work too. Flags before it stay before the
    injected ``chat``; flags after it belong to the chat subparser.
    """
    idx = _first_positional_index(argv)
    if idx is None or argv[idx] in known_commands:
        return argv
    end = idx
    while end + 1 < len(argv) and not argv[end + 1].startswith("-"):
        end += 1
    prompt = " ".join(argv[idx:end + 1])
    return argv[:idx] + ["chat", "-q", prompt] + argv[end + 1:]


def _plugin_cli_discovery_needed() -> bool:
    """True when the CLI might be invoking a plugin-registered subcommand.

    Returning False lets ``main()`` skip plugin discovery entirely during
    argparse setup, saving ~500-650ms per invocation for users whose
    enabled plugins don't contribute any CLI command.
    """
    first = _first_positional_argv()
    if first is None:
        # Bare ``son-of-anton`` or only flags → defaults to ``chat``.
        return False
    if first in _BUILTIN_SUBCOMMANDS:
        return False
    # Unknown token — could be a plugin subcommand, OR a chat prompt
    # starting with a non-flag word. Either way we need discovery: if it
    # IS a plugin command, argparse needs the subparser; if it's a chat
    # prompt, argparse will route it via positional handling and the
    # extra discovery cost is amortized over a full agent run anyway.
    return True


_AGENT_COMMANDS = {None, "chat"}
_AGENT_SUBCOMMANDS = {
    "cron": ("cron_command", {"run", "tick"}),
    "gateway": ("gateway_command", {"run"}),
    "mcp": ("mcp_action", {"serve"}),
}


def _command_has_dedicated_mcp_startup(args) -> bool:
    if args.command == "gateway" and getattr(args, "gateway_command", None) == "run":
        return True
    if args.command == "cron" and getattr(args, "cron_command", None) in {"run", "tick"}:
        return True
    return False


def _should_background_mcp_startup(args) -> bool:
    return args.command in {None, "chat"}


def _prepare_agent_startup(args) -> None:
    """Discover plugins/MCP/hooks for commands that can run an agent turn."""
    # --yolo: chokepoint guarantee that SON_OF_ANTON_YOLO_MODE is set before ANY
    # plugin/tool discovery below imports tools.approval, which freezes
    # _YOLO_MODE_FROZEN at import time (PR #7994 security design).  main()'s
    # dispatch path also sets this earlier, but _prepare_agent_startup() is
    # reachable from other launchers too, so the guarantee lives here where
    # the import is actually triggered (#60328).
    if getattr(args, "yolo", False):
        os.environ["SON_OF_ANTON_YOLO_MODE"] = "1"
    _apply_safe_mode(args)

    _sub_attr, _sub_set = _AGENT_SUBCOMMANDS.get(args.command, (None, None))
    if not (
        args.command in _AGENT_COMMANDS
        or (_sub_attr and getattr(args, _sub_attr, None) in _sub_set)
    ):
        return

    _accept_hooks = bool(getattr(args, "accept_hooks", False))
    try:
        from son_of_anton_cli.plugins import start_background_plugin_discovery

        # Discovery runs in a daemon thread so its ~150ms of manifest
        # scanning + plugin imports overlaps the rest of startup (cli /
        # prompt_toolkit imports, worktree git calls). Correctness is
        # unchanged: every synchronous reader goes through
        # discover_plugins(), which joins this thread first — including
        # the discover_plugins() call model_tools makes at import time,
        # which happens before any tool list is built.
        start_background_plugin_discovery()
    except Exception:
        logger.warning(
            "plugin discovery failed at CLI startup",
            exc_info=True,
        )
    _run_inline_mcp_discovery = True
    if _command_has_dedicated_mcp_startup(args):
        # These entrypoints already do their own MCP startup later on the real
        # runtime path (gateway executor, ACP launcher, cron job runner).
        _run_inline_mcp_discovery = False
    elif _should_background_mcp_startup(args):
        try:
            from son_of_anton_cli.mcp_startup import start_background_mcp_discovery

            start_background_mcp_discovery(
                logger=logger,
                thread_name="cli-mcp-discovery",
            )
        except Exception:
            logger.debug(
                "Background MCP tool discovery failed at CLI startup",
                exc_info=True,
            )
        _run_inline_mcp_discovery = False
    if _run_inline_mcp_discovery:
        try:
            # MCP tool discovery remains synchronous for entrypoints that do
            # not own a later bounded/executor startup path.
            from tools.mcp_tool import discover_mcp_tools

            discover_mcp_tools()
        except Exception:
            logger.debug(
                "MCP tool discovery failed at CLI startup",
                exc_info=True,
            )
    try:
        from son_of_anton_cli.config import load_config
        from agent.shell_hooks import register_from_config

        _hooks_cfg = load_config()
        register_from_config(_hooks_cfg, accept_hooks=_accept_hooks)

        from agent.outbound_webhooks import (
            register_from_config as register_outbound_webhooks,
        )

        register_outbound_webhooks(_hooks_cfg)
    except Exception:
        logger.debug(
            "shell-hook registration failed at CLI startup",
            exc_info=True,
        )


def _apply_safe_mode(args) -> None:
    if not getattr(args, "safe_mode", False):
        return
    os.environ["SON_OF_ANTON_SAFE_MODE"] = "1"
    os.environ["SON_OF_ANTON_IGNORE_USER_CONFIG"] = "1"
    os.environ["SON_OF_ANTON_IGNORE_RULES"] = "1"


def _set_chat_arg_defaults(args) -> None:
    for attr, default in [
        ("query", None),
        ("model", None),
        ("provider", None),
        ("toolsets", None),
        ("verbose", False),
        ("resume", None),
        ("continue_last", None),
        ("worktree", False),
    ]:
        if not hasattr(args, attr):
            setattr(args, attr, default)


def _try_fast_chat_launch() -> bool:
    """Fast path for unambiguous interactive chat launches (all hosts).

    ``son-of-anton`` / ``son-of-anton -w -s foo --yolo`` / ``son-of-anton chat`` don't need the
    full argparse tree: building all ~40 subcommand parsers costs ~140ms of
    pure-Python argparse setup plus their module imports, none of which the
    chat path uses. Parse the lightweight top-level/chat parser instead and
    dispatch straight to ``cmd_chat``.

    Bails out (returns False) whenever the invocation is not certainly a
    chat launch — a subcommand positional, ``--help``, unknown flags — so
    every other path still goes through the full parser unchanged.
    """
    if os.environ.get("SON_OF_ANTON_DISABLE_FAST_CHAT_LAUNCH") == "1":
        return False
    argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        return False
    if _first_positional_argv() not in {None, "chat"}:
        return False

    from son_of_anton_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=cmd_chat)
    try:
        args, unknown = parser.parse_known_args(_coalesce_session_name_args(argv))
    except SystemExit:
        return False
    if unknown:
        # Flags the light parser doesn't know — could belong to a plugin
        # subcommand or a newer full-parser flag. Fall back to full dispatch.
        return False
    if getattr(args, "version", False):
        return False
    if getattr(args, "command", None) not in {None, "chat"}:
        return False

    if getattr(args, "yolo", False):
        os.environ["SON_OF_ANTON_YOLO_MODE"] = "1"
    _prepare_agent_startup(args)

    if getattr(args, "oneshot", None):
        _confirm_startup_expensive_model_override(args)
        _run_and_exit_oneshot(
            args.oneshot,
            model=getattr(args, "model", None),
            provider=getattr(args, "provider", None),
            toolsets=getattr(args, "toolsets", None),
            usage_file=getattr(args, "usage_file", None),
        )

    if (args.resume or args.continue_last) and args.command is None:
        args.command = "chat"

    _set_chat_arg_defaults(args)
    cmd_chat(args)
    return True


def cmd_rag(args):
    """``son-of-anton rag index`` — build or refresh the retrieval index."""
    action = getattr(args, "rag_action", None)
    if action != "index":
        print("Usage: son-of-anton rag index [--rebuild]")
        return 0

    from agent.rag import get_rag_config, sync_index

    config = get_rag_config()
    # Waits for a sync already running (the gateway's periodic refresh) and
    # rebuilds under the same lock, so a forced run never races it.
    result = sync_index(config, rebuild=bool(getattr(args, "rebuild", False)))
    if not result.get("ok"):
        print(f"RAG index: {result.get('error')}")
        if "added" in result:
            print(f"  committed {result['added']} chunk(s) before stopping; rerun to resume")
        return 1
    print(
        f"RAG index: added {result['added']} chunk(s), pruned {result.get('pruned', 0)} — "
        f"{result['journals']} journal(s), {result['sources']} source file(s)"
    )
    if result.get("skipped"):
        print(
            f"  {result['skipped']} journal(s) skipped: their session origin could not "
            "be established (retried on the next sync)"
        )
    print(f"  {result['index_dir']}")
    return 0


def cmd_mcp(args):
    from son_of_anton_cli.mcp_config import mcp_command

    mcp_command(args)


def _advertise_agent_env() -> None:
    """Advertise the agent harness to child processes.

    ``AI_AGENT`` is the emerging cross-agent standard (huggingface_hub's agent
    detection reads it; pi and other agents set it — earendil-works/pi#7493)
    so generic tooling can attribute subprocesses to the harness that spawned
    them. The value must be our id in the public agent-harness registry
    (``son-of-anton`` in huggingface.js ``agent-harnesses.ts``): standard-var
    matching is exact, so any other value is counted as "unknown".
    ``SON_OF_ANTON_AGENT`` is the Son of Anton-specific marker. setdefault: never
    clobber an outer harness (e.g. Son of Anton running inside another agent's
    terminal).
    """
    os.environ.setdefault("AI_AGENT", "son-of-anton")
    os.environ.setdefault("SON_OF_ANTON_AGENT", "true")


def _guard_son_of_anton_home_access() -> None:
    """Fail fast with a clear message when SON_OF_ANTON_HOME is unusable.

    A stale/cross-user home (plain ``su`` without ``-`` carries
    ``SON_OF_ANTON_HOME`` from the other account) makes every home path
    PermissionError: .env, config.toml, .managed, plugins/, sessions. We
    cannot run in another account's 0700 home, so instead of a raw
    traceback from whichever module hits the wall first, one message
    naming the actual problem and the fix. ``--help`` / ``--version`` stay
    usable. A missing home is NOT guarded (first run; setup creates it).
    """
    try:
        home = get_son_of_anton_home()
        home.stat()
        if not os.access(home, os.R_OK | os.X_OK):
            raise PermissionError(f"home {home} is not readable/executable by this user")
    except FileNotFoundError:
        return  # first run — setup will create it
    except PermissionError as exc:
        if "-h" in sys.argv[1:] or "--help" in sys.argv[1:] or "--version" in sys.argv[1:]:
            return
        print(
            "error: SON_OF_ANTON_HOME points at a directory this user cannot access — "
            f"{get_son_of_anton_home()!s}\n"
            f"  {exc}\n"
            "This usually means the variable leaked across accounts (e.g. `su` "
            "without `-`, or a profile export). Fix:\n"
            "  su - <user>       # login shell resets the environment\n"
            "  unset SON_OF_ANTON_HOME   # use this account's default home\n"
            "  export SON_OF_ANTON_HOME=$HOME/.son-of-anton  # correct home",
            file=sys.stderr,
        )
        sys.exit(1)


def main():
    """Main entry point for son-of-anton CLI."""
    _guard_son_of_anton_home_access()

    # Cosmetic: make the process show up as 'son-of-anton' instead of 'python3.11'
    # in ps/top/htop.  Non-fatal — just a nicer UX.
    _set_process_title()

    # Let child processes (and tools like huggingface_hub) detect they run
    # under an AI agent harness.
    _advertise_agent_env()

    if _try_fast_chat_launch():
        return

    from son_of_anton_cli._parser import build_top_level_parser

    parser, subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=cmd_chat)

    # =========================================================================
    # gateway + proxy commands  (parsers built in son_of_anton_cli/subcommands/gateway.py)
    # =========================================================================
    build_gateway_parser(subparsers, cmd_gateway=cmd_gateway)

    # =========================================================================
    # status command  (parser built in son_of_anton_cli/subcommands/status.py)
    # =========================================================================
    build_status_parser(subparsers, cmd_status=cmd_status)

    # =========================================================================
    # pause / resume commands  (parser built in son_of_anton_cli/subcommands/pause.py)
    # =========================================================================
    build_pause_parser(subparsers)

    # =========================================================================
    # nuke command  (parser built in son_of_anton_cli/subcommands/nuke.py)
    # =========================================================================
    build_nuke_parser(subparsers)

    # =========================================================================
    # cron command  (parser built in son_of_anton_cli/subcommands/cron.py)
    # =========================================================================
    build_cron_parser(subparsers, cmd_cron=cmd_cron)

    # =========================================================================
    # config command  (parser built in son_of_anton_cli/subcommands/config.py)
    # =========================================================================
    build_config_parser(subparsers, cmd_config=cmd_config)

    # =========================================================================
    # rag command  (retrieval index management)
    # =========================================================================
    build_rag_parser(subparsers, cmd_rag=cmd_rag)

    # =========================================================================
    # Plugin CLI commands — dynamically registered by memory/general plugins.
    # Plugins provide a register_cli(subparser) function that builds their
    # own argparse tree.  No hardcoded plugin commands in main.py.
    #
    # Skipped when the invocation is already targeting a known built-in
    # subcommand — ``son-of-anton --help``, ``son-of-anton logs``,
    # etc.  This avoids eagerly importing every bundled plugin module
    # (google.cloud.pubsub_v1, aiohttp, grpc, PIL …) which costs
    # 500-650ms on typical installs.
    # =========================================================================
    if _plugin_cli_discovery_needed():
        try:
            from plugins.memory import discover_plugin_cli_commands
            from son_of_anton_cli.plugins import discover_plugins, get_plugin_manager

            seen_plugin_commands = set()
            for cmd_info in discover_plugin_cli_commands():
                plugin_parser = subparsers.add_parser(
                    cmd_info["name"],
                    help=cmd_info["help"],
                    description=cmd_info.get("description", ""),
                    formatter_class=__import__("argparse").RawDescriptionHelpFormatter,
                )
                cmd_info["setup_fn"](plugin_parser)
                if cmd_info.get("handler_fn") is not None:
                    plugin_parser.set_defaults(func=cmd_info["handler_fn"])
                seen_plugin_commands.add(cmd_info["name"])

            discover_plugins()
            for cmd_info in get_plugin_manager()._cli_commands.values():
                if cmd_info["name"] in seen_plugin_commands:
                    continue
                plugin_parser = subparsers.add_parser(
                    cmd_info["name"],
                    help=cmd_info["help"],
                    description=cmd_info.get("description", ""),
                    formatter_class=__import__("argparse").RawDescriptionHelpFormatter,
                )
                cmd_info["setup_fn"](plugin_parser)
                if cmd_info.get("handler_fn") is not None:
                    plugin_parser.set_defaults(func=cmd_info["handler_fn"])
        except Exception as _exc:
            logging.getLogger(__name__).debug("Plugin CLI discovery failed: %s", _exc)

    # =========================================================================
    # mcp command  (parser built in son_of_anton_cli/subcommands/mcp.py)
    # =========================================================================
    build_mcp_parser(subparsers, cmd_mcp=cmd_mcp)
    build_problem_parser(subparsers, cmd_problem=cmd_problem)

    # =========================================================================
    # sessions command
    # =========================================================================
    sessions_parser = subparsers.add_parser(
        "sessions",
        help="Manage session history (list, rename, export, prune, delete)",
        description="View and manage the SQLite session store",
    )
    sessions_subparsers = sessions_parser.add_subparsers(dest="sessions_action")

    sessions_list = sessions_subparsers.add_parser("list", help="List recent sessions")
    sessions_list.add_argument(
        "--source", help="Filter by source (cli, discord, slack, etc.)"
    )
    sessions_list.add_argument(
        "--limit", type=int, default=20, help="Max sessions to show"
    )
    sessions_list.add_argument(
        "--workspace",
        metavar="NEEDLE",
        help="Only sessions in one workspace: a git repo root or project dir "
        "(matched by path substring or basename).",
    )
    sessions_list.add_argument(
        "--here",
        action="store_true",
        help="Only sessions in the workspace containing the current directory "
        "(git repo root, else cwd; exact match).",
    )
    sessions_list.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (most recently active first).",
    )

    def _add_session_filter_args(p, default_older_help):
        p.add_argument(
            "--older-than",
            metavar="AGE",
            help=default_older_help,
        )
        p.add_argument(
            "--newer-than",
            metavar="AGE",
            help="Only match sessions active within the last AGE "
            "(e.g. '5h', '2d') or after an ISO timestamp",
        )
        p.add_argument(
            "--before",
            metavar="TIME",
            help="Only match sessions started before TIME "
            "(duration ago like '5h', or ISO timestamp like '2026-07-05 14:30')",
        )
        p.add_argument(
            "--after",
            metavar="TIME",
            help="Only match sessions started at/after TIME "
            "(duration ago like '5h', or ISO timestamp)",
        )
        p.add_argument("--source", help="Only match sessions from this source")
        p.add_argument(
            "--title", help="Only match sessions whose title contains this substring"
        )
        p.add_argument(
            "--end-reason", help="Only match sessions with this end reason"
        )
        p.add_argument(
            "--cwd", help="Only match sessions whose working directory is under this path"
        )
        p.add_argument(
            "--min-messages", type=int, help="Only match sessions with >= N messages"
        )
        p.add_argument(
            "--max-messages", type=int, help="Only match sessions with <= N messages"
        )
        p.add_argument(
            "--model",
            help="Only match sessions whose model name contains this substring "
            "(e.g. 'sonnet', 'gpt-5', 'son-of-anton')",
        )
        p.add_argument(
            "--provider",
            help="Only match sessions billed through this provider "
            "(e.g. openai-api, custom)",
        )
        p.add_argument(
            "--user", help="Only match sessions from this user ID"
        )
        p.add_argument(
            "--chat-id", help="Only match sessions from this chat/channel ID"
        )
        p.add_argument(
            "--chat-type",
            help="Only match sessions with this chat type (e.g. dm, group)",
        )
        p.add_argument(
            "--branch",
            help="Only match sessions whose git branch contains this substring",
        )
        p.add_argument(
            "--min-tokens", type=int,
            help="Only match sessions with >= N total tokens (input+output)",
        )
        p.add_argument(
            "--max-tokens", type=int,
            help="Only match sessions with <= N total tokens (input+output)",
        )
        p.add_argument(
            "--min-cost", type=float,
            help="Only match sessions costing >= N USD (actual or estimated)",
        )
        p.add_argument(
            "--max-cost", type=float,
            help="Only match sessions costing <= N USD (actual or estimated)",
        )
        p.add_argument(
            "--min-tool-calls", type=int,
            help="Only match sessions with >= N tool calls",
        )
        p.add_argument(
            "--max-tool-calls", type=int,
            help="Only match sessions with <= N tool calls",
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="List matching sessions without changing anything",
        )
        p.add_argument(
            "--yes", "-y", action="store_true", help="Skip confirmation"
        )

    sessions_export = sessions_subparsers.add_parser(
        "export", help="Export sessions to JSONL, Markdown, or QMD"
    )
    sessions_export.add_argument(
        "output",
        nargs="?",
        help=(
            "Output path. JSONL: file path (use - for stdout, required). "
            "md/qmd: output directory (default: <son-of-anton home>/session-exports)"
        ),
    )
    sessions_export.add_argument(
        "--format",
        choices=["jsonl", "md", "qmd", "html", "trace"],
        default="jsonl",
        help=(
            "Export format (default: jsonl). 'trace' emits Claude Code JSONL "
            "for the Hugging Face Agent Trace Viewer"
        ),
    )
    sessions_export.add_argument(
        "--upload",
        action="store_true",
        help=(
            "trace only: upload to your Hugging Face traces dataset instead "
            "of writing a local file (needs HF_TOKEN)"
        ),
    )
    sessions_export.add_argument(
        "--public",
        action="store_true",
        help="trace --upload only: create/update a public dataset instead of private",
    )
    sessions_export.add_argument(
        "--no-redact",
        action="store_true",
        help=(
            "trace only: skip the forced secret redaction; "
            "only use after manual review"
        ),
    )
    sessions_export.add_argument(
        "--only",
        choices=["user-prompts"],
        help=(
            "Export only a filtered view (user-prompts: one prompt record "
            "per line for jsonl, headed sections for md)"
        ),
    )
    sessions_export.add_argument(
        "--session-id", help="Session ID or unique prefix to export"
    )
    _add_session_filter_args(
        sessions_export,
        "Only export sessions older than AGE (duration like '5h'/'2d', "
        "bare number of days, or an ISO timestamp)",
    )
    sessions_export.add_argument(
        "--redact",
        action="store_true",
        help="Redact secrets (API keys, tokens, credentials) from exported content",
    )
    sessions_export.add_argument(
        "--lineage",
        choices=["single", "logical"],
        default="single",
        help="md/qmd only: export one row or its compaction lineage",
    )
    sessions_export.add_argument(
        "--delete-after-verified",
        action="store_true",
        help="md/qmd only: after verified single-session export, delete that session (needs --yes)",
    )
    sessions_export.add_argument(
        "--force",
        action="store_true",
        help="md/qmd only: overwrite an existing export file",
    )

    sessions_delete = sessions_subparsers.add_parser(
        "delete", help="Delete a specific session, or every session with --all"
    )
    sessions_delete.add_argument(
        "session_id", nargs="?", help="Session ID to delete"
    )
    sessions_delete.add_argument(
        "--all",
        dest="all_sessions",
        action="store_true",
        help="Delete every session in this home (confirms unless --yes)",
    )
    sessions_delete.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation"
    )

    sessions_prune = sessions_subparsers.add_parser(
        "prune",
        help="Delete old sessions (filterable by time window, source, title, ...)",
    )
    _add_session_filter_args(
        sessions_prune,
        "Delete sessions older than AGE — days if bare number, or a duration "
        "like '5h'/'2d'/'1w', or an ISO timestamp (bare prune with no filters "
        "defaults to 90 days; any filter matches all ages)",
    )
    sessions_prune.add_argument(
        "--include-archived",
        action="store_true",
        help="Also delete archived sessions (excluded by default)",
    )
    sessions_prune.add_argument(
        "--include-pinned",
        action="store_true",
        help="Also delete pinned sessions (excluded by default — pin is a keep flag)",
    )
    sessions_prune.add_argument(
        "--never-active",
        action="store_true",
        help=(
            "Instead of ended sessions, delete keyed gateway rows that were "
            "opened and never used (no messages, tokens, tool calls or title) "
            "and are older than AGE (default 30 days). Ordinary prune can "
            "never reach these — it only ever selects ended sessions"
        ),
    )

    sessions_archive = sessions_subparsers.add_parser(
        "archive",
        help="Bulk-archive (soft-hide) sessions matching filters — no deletion",
    )
    _add_session_filter_args(
        sessions_archive,
        "Only archive sessions older than AGE (duration like '5h'/'2d', "
        "bare number of days, or ISO timestamp)",
    )

    sessions_subparsers.add_parser(
        "optimize",
        help="Reclaim disk space: merge FTS5 segments + VACUUM (no data change)",
    )

    sessions_clean_markers = sessions_subparsers.add_parser(
        "clean-markers",
        help="Permanently clear stale tool-call marker content left by sessions from before #78148",
        description=(
            "Before the #78148 fix, a local tool-call template could persist a "
            "bare bracketed marker (e.g. \"[memory]\") as an assistant turn's "
            "content instead of real text. This is already repaired in memory "
            "on every session load, so running this is optional — it rewrites "
            "the affected rows once, in place, so long-lived sessions stop "
            "re-scanning/re-repairing the same rows on every resume. Only the "
            "content column is touched; tool_calls and every other column on "
            "the row are left untouched."
        ),
    )
    sessions_clean_markers.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report the affected row count without writing",
    )
    sessions_clean_markers.add_argument(
        "--no-backup",
        action="store_true",
        default=False,
        help="Skip the timestamped state.db backup taken before writing (not recommended)",
    )

    sessions_optimize_storage = sessions_subparsers.add_parser(
        "optimize-storage",
        help="Migrate the search index to the compact v23 layout (reclaims disk on large DBs)",
        description=(
            "Rebuild the full-text search index in the compact v23 "
            "external-content layout. On large databases this reclaims a "
            "large fraction of state.db (the old layout stored duplicate "
            "copies of every message and indexed tool output). Runs "
            "foreground with a progress bar, throttles so a running gateway "
            "stays responsive, and VACUUMs at the end. Safe to interrupt and "
            "re-run — it resumes where it left off. No conversation data is "
            "changed; only the search index is rebuilt."
        ),
    )
    sessions_optimize_storage.add_argument(
        "--no-vacuum",
        action="store_true",
        default=False,
        help="Skip the final VACUUM (index is rebuilt but freed pages aren't returned to the OS until a later VACUUM)",
    )
    sessions_optimize_storage.add_argument(
        "--yes", "-y",
        action="store_true",
        default=False,
        help="Skip the disk-space confirmation prompt",
    )

    sessions_repair = sessions_subparsers.add_parser(
        "repair",
        help="Repair a malformed state.db schema so hidden sessions reappear",
        description=(
            "Recover a state.db whose schema is malformed (e.g. 'table "
            "messages_fts already exists'), which makes Desktop/Dashboard show "
            "no sessions. A backup is made first; sessions and messages are "
            "preserved and the FTS search index is rebuilt if needed."
        ),
    )
    sessions_repair.add_argument(
        "--check-only",
        action="store_true",
        help="Only report whether the database opens cleanly; do not modify it",
    )
    sessions_repair.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the timestamped backup copy (not recommended)",
    )

    sessions_repair_routing = sessions_subparsers.add_parser(
        "repair-routing",
        help="Re-stamp gateway sessions that lost their routing identity",
        description=(
            "Find gateway conversations stranded in session rows whose "
            "routing identity (session_key/chat_id/origin) was never "
            "written — the damage a corrupt state.db write path leaves "
            "behind (#82616). Such a row is invisible to restart recovery, "
            "so the chat resumes an older session instead. Re-stamps each "
            "orphan from the keyed predecessor it continues, and only when "
            "that predecessor is unambiguous. Reports without touching the "
            "database unless --apply is given."
        ),
    )
    sessions_repair_routing.add_argument(
        "--apply",
        action="store_true",
        help="Perform the adoptions (default: report only)",
    )
    sessions_repair_routing.add_argument(
        "--max-gap-seconds",
        type=float,
        default=None,
        help=(
            "Window between a keyed predecessor's last activity and an "
            "orphan's start for them to count as the same conversation "
            "(default: 900)"
        ),
    )

    sessions_recover = sessions_subparsers.add_parser(
        "recover",
        help="Rebuild canonical session data into a separate clean database",
        description=(
            "Offline, non-destructive recovery for a damaged state.db. The "
            "source database and its WAL/SHM/rollback-journal sidecars are "
            "copied before SQLite opens anything. Canonical rows are rebuilt "
            "into a new output database; derived search indexes are recreated "
            "and the active database is never replaced automatically."
        ),
    )
    sessions_recover.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Source state.db or preserved backup to inspect/recover",
    )
    sessions_recover.add_argument(
        "--output",
        type=Path,
        help="New recovery database path (required unless --inspect-only)",
    )
    sessions_recover.add_argument(
        "--inspect-only",
        action="store_true",
        help="Only report canonical table readability; do not create an output database",
    )
    sessions_recover.add_argument(
        "--work-dir",
        type=Path,
        help="Existing directory for the disposable source copy (defaults beside the output)",
    )
    sessions_recover.add_argument(
        "--chunk-size",
        type=int,
        default=1000,
        help="Rows committed per recovery batch (default: 1000)",
    )
    sessions_recover.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Best-effort salvage across damaged row ranges; the output remains "
            "separate and every skipped range is recorded"
        ),
    )
    sessions_recover.add_argument(
        "--report",
        type=Path,
        help="JSON report path (defaults to <output>.recovery.json)",
    )

    sessions_subparsers.add_parser("stats", help="Show session store statistics")

    sessions_rename = sessions_subparsers.add_parser(
        "rename", help="Set or change a session's title"
    )
    sessions_rename.add_argument("session_id", help="Session ID to rename")
    sessions_rename.add_argument("title", nargs="+", help="New title for the session")

    sessions_pin = sessions_subparsers.add_parser(
        "pin",
        help="Pin session(s) — durable keep flag, exempt from auto-archive",
        description=(
            "Set the durable 'keep' flag on one or more sessions. Pinned "
            "sessions are exempt from the sessions.auto_archive stale sweep "
            "and always appear in listings. The same flag drives the Desktop "
            "sidebar's Pinned section — pin from either surface, both see it."
        ),
    )
    sessions_pin.add_argument(
        "session_ids", nargs="+", help="Session ID(s) or unique prefix(es) to pin"
    )

    sessions_unpin = sessions_subparsers.add_parser(
        "unpin", help="Remove the pin (durable keep flag) from session(s)"
    )
    sessions_unpin.add_argument(
        "session_ids", nargs="+", help="Session ID(s) or unique prefix(es) to unpin"
    )

    sessions_pinned = sessions_subparsers.add_parser(
        "pinned", help="List pinned sessions"
    )
    sessions_pinned.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (for backup/restore scripting)",
    )

    sessions_retitle = sessions_subparsers.add_parser(
        "retitle-skills",
        help="Re-title sessions whose auto-title came from a /skill's own text",
        description=(
            "Sessions opened with a /skill were auto-titled from the expanded "
            "message, which embeds the whole skill body — so the title "
            "describes the SKILL, not the request. This regenerates those "
            "titles from what the user actually typed. Lists what it would "
            "change unless --apply is passed."
        ),
    )
    sessions_retitle.add_argument(
        "--apply",
        action="store_true",
        help="Write the new titles (default: dry run)",
    )
    sessions_retitle.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum sessions to examine (default: 200)",
    )

    sessions_browse = sessions_subparsers.add_parser(
        "browse",
        help="Interactive session picker — browse, search, and resume sessions",
    )
    sessions_browse.add_argument(
        "--source", help="Filter by source (cli, discord, slack, etc.)"
    )
    sessions_browse.add_argument(
        "--limit", type=int, default=500, help="Max sessions to load (default: 500)"
    )

    sessions_import = sessions_subparsers.add_parser(
        "import",
        help="Import a Claude Code, Codex CLI, or opencode session",
        description=(
            "Pull conversations started in Claude Code (~/.claude/projects), "
            "Codex CLI (~/.codex/sessions), or opencode "
            "(~/.local/share/opencode/opencode.db) into the Son of Anton "
            "session store so they can be resumed with "
            "'son-of-anton --resume <id>'. The foreign stores are only read, "
            "never modified. Pass --all to import every session that is not "
            "in the store yet."
        ),
    )
    sessions_import.add_argument(
        "--from",
        dest="from_source",
        choices=["claude", "codex", "opencode"],
        help="Which tool to import from (default: pick across all)",
    )
    sessions_import.add_argument(
        "--all",
        action="store_true",
        help=(
            "Import every session from the chosen source(s) that is not "
            "already in the store (idempotent — re-running imports only what "
            "is new)"
        ),
    )
    sessions_import.add_argument(
        "--limit",
        type=int,
        default=0,
        help="With --all: import at most N sessions, newest first",
    )
    sessions_import.add_argument(
        "--force",
        action="store_true",
        help="With --all: re-import sessions already in the store",
    )
    sessions_import.add_argument(
        "--root",
        help=(
            "With --all: override the Claude Code / Codex sessions directory "
            "(e.g. another account's ~/.claude)"
        ),
    )
    sessions_import.add_argument(
        "--session",
        dest="foreign_session_id",
        help="Foreign session id to import directly (opencode: ses_...)",
    )
    sessions_import.add_argument(
        "--db",
        dest="opencode_db",
        help="opencode database path (default: auto-detect opencode.db)",
    )
    sessions_import.add_argument(
        "--analyze",
        action="store_true",
        help=(
            "After importing, run an agent pass over the transcript to write "
            "durable memories (uses the configured model)"
        ),
    )
    sessions_import.add_argument(
        "path",
        nargs="?",
        help=(
            "Claude/Codex JSONL file, or an opencode session id / database "
            "path (skips the picker)"
        ),
    )

    sessions_analyze = sessions_subparsers.add_parser(
        "analyze",
        help="Mine a stored session for durable memories",
        description=(
            "Run one agent pass over a session transcript and persist durable "
            "memories. 'sessions import --analyze' runs "
            "the same pass during import; this command re-runs it for any "
            "stored session."
        ),
    )
    sessions_analyze.add_argument(
        "session_id", help="Session ID or unique prefix to analyze"
    )


    # cmd_sessions lives in son_of_anton_cli/sessions_cmd.py (main.py decomposition).
    # sessions_parser is threaded in via functools.partial because the
    # fallthrough branch calls sessions_parser.print_help() (formerly a
    # closure capture of this main()-local). The indirection through _self()
    # keeps the sessions_cmd import lazy until the subcommand actually runs
    # and lets monkeypatches on son_of_anton_cli.main.cmd_sessions keep working.
    def _dispatch_sessions(_args, *, sessions_parser=sessions_parser):
        return _self().cmd_sessions(_args, sessions_parser=sessions_parser)

    sessions_parser.set_defaults(func=_dispatch_sessions)

    # NOTE: the `son-of-anton version` subcommand was removed — `son-of-anton --version`
    # / `-V` now carries the full output including update status.

    # =========================================================================
    # Parse and execute
    # =========================================================================
    # Pre-process argv so unquoted multi-word session names after -c / -r
    # are merged into a single token before argparse sees them.
    # e.g. ``son-of-anton -c Pokemon Agent Dev`` → ``son-of-anton -c 'Pokemon Agent Dev'``
    _processed_argv = _coalesce_session_name_args(sys.argv[1:])

    _known_cmds = (
        set(subparsers.choices.keys()) if hasattr(subparsers, "choices") else set()
    )
    # ``son-of-anton "prompt"`` → ``chat -q "prompt"``. Runs after the session
    # name coalescing so ``-c auth notes`` is not mistaken for a prompt, and
    # after the plugin subparsers are registered so a plugin command name is
    # never swallowed as prompt text.
    _processed_argv = _rewrite_bare_prompt(_processed_argv, _known_cmds)

    # ── Defensive subparser routing (bpo-9338 workaround) ───────────
    # On some Python versions (notably <3.11), argparse fails to route
    # subcommand tokens when the parent parser has nargs='?' optional
    # arguments (--continue).  The symptom: "unrecognized arguments: model"
    # even though 'model' is a registered subcommand.
    #
    # Fix: when argv contains a token matching a known subcommand, set
    # subparsers.required=True to force deterministic routing.  If that
    # fails (e.g. 'son-of-anton -c model' where 'model' is consumed as the
    # session name for --continue), fall back to the default behaviour.
    import io as _io

    _has_cmd_token = any(
        t in _known_cmds for t in _processed_argv if not t.startswith("-")
    )

    if _has_cmd_token:
        subparsers.required = True
        _saved_stderr = sys.stderr
        try:
            sys.stderr = _io.StringIO()
            args = parser.parse_args(_processed_argv)
            sys.stderr = _saved_stderr
        except SystemExit as exc:
            sys.stderr = _saved_stderr
            # Help/version flags (exit code 0) already printed output —
            # re-raise immediately to avoid a second parse_args printing
            # the same help text again (#10230).
            if exc.code == 0:
                raise
            # Subcommand name was consumed as a flag value (e.g. -c model).
            # Fall back to optional subparsers so argparse handles it normally.
            subparsers.required = False
            args = parser.parse_args(_processed_argv)
    else:
        subparsers.required = False
        args = parser.parse_args(_processed_argv)

    # Handle --version flag
    if args.version:
        _startup_fast.print_fast_version_info()
        return

    # --yolo: set SON_OF_ANTON_YOLO_MODE *before* plugin discovery.  The call to
    # _prepare_agent_startup() below triggers discover_plugins() → tool
    # imports, and tools.approval freezes _YOLO_MODE_FROZEN at module
    # import time (PR #7994, security hardening against prompt-injection).
    # If the env var is set only later (e.g. inside cmd_chat), the frozen
    # value is already False and --yolo silently does nothing.
    if getattr(args, "yolo", False):
        os.environ["SON_OF_ANTON_YOLO_MODE"] = "1"

    # Discover Python plugins and register shell hooks once, before any
    # command that can fire lifecycle hooks.  Both are idempotent; gated
    # so introspection/management commands (son-of-anton hooks list, cron
    # list, gateway status, mcp list, ...) don't pay discovery cost or
    # trigger consent prompts for hooks the user is still inspecting.
    _prepare_agent_startup(args)

    # Handle top-level --oneshot / -z: single-shot mode, stdout = final
    # response only, nothing else. Bypasses cli.py entirely.
    if getattr(args, "oneshot", None):
        _confirm_startup_expensive_model_override(args)
        _run_and_exit_oneshot(
            args.oneshot,
            model=getattr(args, "model", None),
            provider=getattr(args, "provider", None),
            toolsets=getattr(args, "toolsets", None),
            usage_file=getattr(args, "usage_file", None),
        )

    # Handle top-level --resume / --continue as shortcut to chat
    if (args.resume or args.continue_last) and args.command is None:
        args.command = "chat"
        for attr, default in [
            ("query", None),
            ("model", None),
            ("provider", None),
            ("toolsets", None),
            ("verbose", None),
            ("worktree", False),
        ]:
            if not hasattr(args, attr):
                setattr(args, attr, default)
        cmd_chat(args)
        return

    # Default to chat if no command specified
    if args.command is None:
        for attr, default in [
            ("query", None),
            ("model", None),
            ("provider", None),
            ("toolsets", None),
            ("verbose", None),
            ("resume", None),
            ("continue_last", None),
            ("worktree", False),
        ]:
            if not hasattr(args, attr):
                setattr(args, attr, default)
        cmd_chat(args)
        return

    # Execute the command.  Propagate the handler's return code as the
    # process exit code so subcommands that signal failure (e.g.
    # ``son-of-anton egress start`` refusing when credential_source=bitwarden
    # is misconfigured) actually exit non-zero.  Handlers that return
    # None are treated as success (exit 0).
    if hasattr(args, "func"):
        rc = args.func(args)
        if isinstance(rc, int) and rc != 0:
            sys.exit(rc)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
