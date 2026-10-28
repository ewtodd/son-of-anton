"""
Status command for son-of-anton CLI.

Shows the status of all Son of Anton Agent components.
"""

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

from son_of_anton_cli.colors import Colors, color
from son_of_anton_cli.config import get_env_path, get_son_of_anton_home, load_config
from son_of_anton_cli.runtime_provider import resolve_requested_provider


def check_mark(ok: bool) -> str:
    if ok:
        return color("✓", Colors.GREEN)
    return color("✗", Colors.RED)

def _format_relative_ts(ts: float) -> str:
    """Format an epoch timestamp as a short relative age for status output."""
    from son_of_anton_cli.timefmt import relative_time

    return relative_time(ts)


def _configured_model_label(config: dict) -> str:
    """Return the configured default model from config.toml."""
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        model = (model_cfg.get("default") or model_cfg.get("name") or "").strip()
    elif isinstance(model_cfg, str):
        model = model_cfg.strip()
    else:
        model = ""
    return model or "(not set)"


def _estop_status_line():
    """One-line pause banner for `son-of-anton status`, or None when not paused.

    Cheap: a single stat on $SON_OF_ANTON_HOME/ESTOP via agent.estop.
    """
    try:
        from agent.estop import get_state
    except ImportError:
        return None
    state = get_state()
    if state is None:
        return None
    reason = state.get("reason")
    suffix = f" — reason: {reason}" if reason else ""
    return f"⏸️  PAUSED (global emergency stop{suffix}; `son-of-anton resume` to lift)"


def show_status(args):
    """Show status of all Son of Anton Agent components."""
    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                 ⚛ Son of Anton Agent Status                  │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.CYAN))

    _paused_line = _estop_status_line()
    if _paused_line:
        print()
        print(color(_paused_line, Colors.YELLOW, Colors.BOLD))

    # =========================================================================
    # Environment
    # =========================================================================
    print()
    print(color("◆ Environment", Colors.CYAN, Colors.BOLD))
    print(f"  Project:      {PROJECT_ROOT}")
    print(f"  Python:       {sys.version.split()[0]}")

    env_path = get_env_path()
    print(f"  .env file:    {check_mark(env_path.exists())} {'exists' if env_path.exists() else 'not found'}")

    try:
        config = load_config()
    except Exception:
        config = {}

    print(f"  Model:        {_configured_model_label(config)}")
    print(f"  Provider:     {resolve_requested_provider()}")

    # =========================================================================
    # Terminal Configuration
    # =========================================================================
    print()
    print(color("◆ Terminal Backend", Colors.CYAN, Colors.BOLD))

    terminal_cfg = config.get("terminal", {}) if isinstance(config.get("terminal"), dict) else {}
    terminal_env = os.getenv("TERMINAL_ENV", "")
    if not terminal_env:
        terminal_env = terminal_cfg.get("backend", "local")
    print(f"  Backend:      {terminal_env}")

    if terminal_env == "ssh":
        ssh_host = os.getenv("TERMINAL_SSH_HOST", "")
        ssh_user = os.getenv("TERMINAL_SSH_USER", "")
        print(f"  SSH Host:     {ssh_host or '(not set)'}")
        print(f"  SSH User:     {ssh_user or '(not set)'}")

    # =========================================================================
    # Messaging
    # =========================================================================
    print()
    print(color("◆ Messaging Platforms", Colors.CYAN, Colors.BOLD))
    signal_url = os.getenv("SIGNAL_HTTP_URL", "")
    signal_status = "configured" if signal_url else "not configured"
    if os.getenv("SIGNAL_HOME_CHANNEL"):
        signal_status += f" (home: {os.getenv('SIGNAL_HOME_CHANNEL')})"
    print(f"  {'Signal':<12}  {check_mark(bool(signal_url))} {signal_status}")

    # =========================================================================
    # Gateway Status
    # =========================================================================
    print()
    print(color("◆ Gateway Service", Colors.CYAN, Colors.BOLD))

    try:
        from son_of_anton_cli.gateway import find_gateway_pids, systemd_units_of_pids

        pids = find_gateway_pids()
        print(f"  Status:       {check_mark(bool(pids))} {'running' if pids else 'stopped'}")
        if pids:
            shown = [str(pid) for pid in pids[:3]] + (["..."] if len(pids) > 3 else [])
            print(f"  PID(s):       {', '.join(shown)}")
            units = systemd_units_of_pids(pids)
            print(f"  Manager:      {'systemd: ' + ', '.join(units) if units else 'manual process'}")
    except Exception:
        print(f"  Status:       {color('unknown', Colors.DIM)}")

    # =========================================================================
    # Cron Jobs
    # =========================================================================
    print()
    print(color("◆ Scheduled Jobs", Colors.CYAN, Colors.BOLD))

    jobs_file = get_son_of_anton_home() / "cron" / "jobs.json"
    if jobs_file.exists():
        import json
        try:
            # utf-8-sig: same dialect as cron/jobs.load_jobs — Windows editors
            # may leave a UTF-8 BOM that plain utf-8 json.load rejects.
            with open(jobs_file, encoding="utf-8-sig") as f:
                data = json.load(f)
                jobs = data.get("jobs", [])
                enabled_jobs = [j for j in jobs if j.get("enabled", True)]
                print(f"  Jobs:         {len(enabled_jobs)} active, {len(jobs)} total")
        except Exception:
            print("  Jobs:         (error reading jobs file)")
    else:
        print("  Jobs:         0")

    # =========================================================================
    # Sessions
    # =========================================================================
    print()
    print(color("◆ Sessions", Colors.CYAN, Colors.BOLD))

    # Gateway session count: state.db is the source of truth (#9006);
    # fall back to sessions.json for pre-migration installs.
    _session_count = None
    _gateway_rows = []
    try:
        from son_of_anton_state import SessionDB
        _db = SessionDB()
        try:
            _lister = getattr(_db, "list_gateway_sessions", None)
            if callable(_lister):
                _gateway_rows = _lister(active_only=True) or []
                _session_count = len(_gateway_rows)
        finally:
            _db.close()
    except Exception:
        _session_count = None
        _gateway_rows = []

    if _session_count is not None and _session_count > 0:
        print(f"  Active:       {_session_count} session(s)")
        freshest = max(
            (float(r.get("last_active") or 0) for r in _gateway_rows),
            default=0.0,
        )
        if freshest > 0:
            print(f"  Last activity:{_format_relative_ts(freshest):>13}")
    else:
        sessions_file = get_son_of_anton_home() / "sessions" / "sessions.json"
        if sessions_file.exists():
            import json
            try:
                with open(sessions_file, encoding="utf-8") as f:
                    data = json.load(f)
                    _entries = {
                        k: v for k, v in data.items()
                        if not str(k).startswith("_")
                    } if isinstance(data, dict) else {}
                    print(f"  Active:       {len(_entries)} session(s)")
            except Exception:
                print("  Active:       (error reading sessions file)")
        else:
            print(f"  Active:       {_session_count if _session_count is not None else 0}")

    # Slot usage, only when max_concurrent_sessions is set. The cap is shared
    # across CLI, desktop/TUI and the messaging gateway, so the surface that
    # gets rejected is rarely the one holding the slots — without this the only
    # way to find out is reading runtime/active_sessions.json by hand.
    try:
        from son_of_anton_cli.active_sessions import (
            active_session_registry_snapshot,
            format_age,
            resolve_max_concurrent_sessions,
        )

        _cap = resolve_max_concurrent_sessions(config)
    except Exception:
        _cap = None
    if _cap:
        try:
            _held = active_session_registry_snapshot()
        except Exception:
            _held = []
        _full = len(_held) >= _cap
        print(
            "  Slots:        "
            + color(
                f"{len(_held)}/{_cap} in use", Colors.YELLOW if _full else Colors.GREEN
            )
        )
        _now = time.time()
        for _entry in sorted(_held, key=lambda e: e.get("started_at") or 0):
            _age = format_age(_now - float(_entry.get("started_at") or _now))
            print(
                f"                {_entry.get('surface') or 'unknown':<17} "
                f"{_entry.get('session_id') or '?':<24} {_age}"
            )

    print()
    print(color("─" * 60, Colors.DIM))
    print(color("  Configuration is declared in config.toml (the Nix module's settings)", Colors.DIM))
    print()
