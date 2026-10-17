"""
Gateway subcommand for son-of-anton CLI.

Handles: son-of-anton gateway [run|status]

The service itself is declared by the NixOS / Home Manager module
(``son-of-anton-<instance>.service``); ``run`` is its ExecStart and ``status``
reports what is running. There is nothing to install, start or stop here.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

from gateway.restart import is_gateway_supervisor_process
from son_of_anton_cli.config import get_son_of_anton_home
from son_of_anton_cli.cli_output import print_error

logger = logging.getLogger(__name__)


# =============================================================================
# Process discovery
# =============================================================================


def _get_ancestor_pids() -> set[int]:
    """Return the set of PIDs in the current process's ancestor chain.

    Walks from the current PID up to PID 1 (init) so that process-table scans
    never match the calling CLI process or any of its parents.  This prevents
    ``son-of-anton gateway status`` from falsely counting the ``son-of-anton`` CLI that
    invoked it as a running gateway instance (see #13242).
    """
    import psutil

    ancestors: set[int] = set()
    pid = os.getpid()
    # Cap iterations to avoid infinite loops.
    for _ in range(64):
        ancestors.add(pid)
        try:
            parent = psutil.Process(pid).ppid() if pid > 1 else None
        except Exception:
            parent = None
        if not parent or parent in ancestors:
            break
        pid = parent
    return ancestors


def _append_unique_pid(
    pids: list[int], pid: int | None, exclude_pids: set[int]
) -> None:
    if pid is None or pid <= 0:
        return
    if pid == os.getpid() or pid in exclude_pids or pid in pids:
        return
    pids.append(pid)


def _scan_gateway_pids(exclude_pids: set[int]) -> list[int]:
    """Best-effort ``/proc`` scan for gateway PIDs.

    Supplements the home-scoped PID file so status views still spot a live
    gateway when the PID file is stale or missing. A command line that names
    a different ``SON_OF_ANTON_HOME=`` is someone else's gateway; the home is
    usually passed through the environment, so its absence is not.
    """
    # Exclude the entire ancestor chain so the CLI process that invoked this
    # scan (e.g. ``son-of-anton gateway status``) is never mistaken for a running
    # gateway.  See #13242.
    exclude_pids = exclude_pids | _get_ancestor_pids()
    from gateway.status import looks_like_gateway_command_line

    home_lc = str(get_son_of_anton_home().resolve()).lower()
    pids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude_pids:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().decode("utf-8", errors="replace").replace("\x00", " ")
        except OSError:
            continue
        if not looks_like_gateway_command_line(cmdline):
            continue
        lowered = cmdline.lower()
        if "son_of_anton_home=" in lowered and f"son_of_anton_home={home_lc}" not in lowered:
            continue
        _append_unique_pid(pids, pid, exclude_pids)
    return pids


def find_gateway_pids(exclude_pids: set | None = None) -> list:
    """Find PIDs of running gateway processes for this SON_OF_ANTON_HOME."""
    _exclude = set(exclude_pids or set())
    pids: list[int] = []
    try:
        from gateway.status import get_running_pid

        _append_unique_pid(pids, get_running_pid(), _exclude)
    except Exception:
        pass
    for pid in _scan_gateway_pids(_exclude):
        _append_unique_pid(pids, pid, _exclude)
    return pids


def systemd_units_of_pids(pids) -> list[str]:
    """The systemd services supervising *pids*, in order, deduplicated.

    Read from ``/proc/<pid>/cgroup`` (world-readable), so a gateway run by the
    NixOS / Home Manager module (``son-of-anton-<instance>.service``) is
    recognised whatever its unit is called. Only the last path component
    counts: a gateway started from a terminal sits in a ``.scope`` under the
    user manager's own ``user@<uid>.service``, which is not supervision.
    """
    units: list[str] = []
    for pid in pids:
        try:
            text = Path(f"/proc/{int(pid)}/cgroup").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        unit = service_of_cgroup(text)
        if unit and unit not in units:
            units.append(unit)
    return units


def service_of_cgroup(text: str) -> str | None:
    """The ``.service`` a ``/proc/<pid>/cgroup`` listing places the process in."""
    for line in text.splitlines():
        leaf = line.rsplit(":", 1)[-1].rstrip("/").rsplit("/", 1)[-1]
        if leaf.endswith(".service"):
            return leaf
    return None


# =============================================================================
# Gateway Runner
# =============================================================================


def _guard_existing_gateway_process_conflict(replace: bool = False) -> None:
    """Refuse duplicate foreground startup before importing gateway.run.

    ``gateway.run`` performs the authoritative PID/lock check, but importing it
    is expensive: it pulls in model_tools/plugin discovery first. This cheap
    PID-file preflight preserves the same user-facing contract while avoiding
    that startup work. The service's own start (systemd sets ``INVOCATION_ID``)
    is never refused here.
    """
    if replace or is_gateway_supervisor_process():
        return
    try:
        from gateway.status import get_running_pid

        pid = get_running_pid()
    except Exception:
        logger.debug("Existing-gateway process probe failed", exc_info=True)
        return
    if pid is None:
        return

    print_error(
        f"Another gateway instance is already running (PID {pid})."
    )
    print("  Use 'son-of-anton gateway run --replace' to replace it.")
    sys.exit(1)


def run_gateway(verbose: int = 0, quiet: bool = False, replace: bool = False):
    """Run the gateway in foreground.

    Args:
        verbose: Stderr log verbosity count added on top of default WARNING (0=WARNING, 1=INFO, 2+=DEBUG).
        quiet: Suppress all stderr log output.
        replace: If True, kill any existing gateway instance before starting.
                 This prevents systemd restart loops when the old process
                 hasn't fully exited yet.
    """
    _guard_existing_gateway_process_conflict(replace=replace)
    sys.path.insert(0, str(PROJECT_ROOT))

    try:
        _stdin_is_tty = bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        _stdin_is_tty = False
    _gateway_detached = (
        os.getenv("SON_OF_ANTON_GATEWAY_DETACHED", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    # Mark this process as a real gateway BEFORE importing gateway.run.
    # gateway.run's config->env bridge (terminal.cwd -> TERMINAL_CWD) is gated
    # on this marker, because the module sets _SON_OF_ANTON_GATEWAY on import
    # and so cannot distinguish a genuine gateway from an incidental import.
    os.environ["_SON_OF_ANTON_GATEWAY_PROC"] = "1"
    from gateway.run import start_gateway

    print("┌─────────────────────────────────────────────────────────┐")
    print("│           ⚛ Son of Anton Gateway Starting...                 │")
    print("├─────────────────────────────────────────────────────────┤")
    print("│  Messaging platforms + cron scheduler                    │")
    print("│  Press Ctrl+C to stop                                   │")
    print("└─────────────────────────────────────────────────────────┘")
    print()

    # Exit with code 1 if gateway fails to connect any platform,
    # so systemd Restart=always will retry on transient errors
    verbosity = None if quiet else verbose

    # ── Exit-path diagnostics ────────────────────────────────────────────
    # When the gateway dies silently (no shutdown log, no traceback in
    # gateway.log / errors.log), we're usually blind to the cause. The code
    # below captures *every* way the asyncio.run() call below can return,
    # with full context dumped to a dedicated log so the next silent death
    # yields evidence instead of a mystery. This is diagnostic scaffolding;
    # cheap to keep on, costs nothing during normal operation, and the
    # emitted lines are opt-in via the SON_OF_ANTON_GATEWAY_EXIT_DIAG env var.
    import atexit as _atexit
    import traceback as _traceback
    from datetime import datetime as _dt, timezone as _tz

    def _exit_diag(tag: str, **extra: object) -> None:
        if os.environ.get("SON_OF_ANTON_GATEWAY_EXIT_DIAG", "1") != "1":
            return
        try:
            log_dir = get_son_of_anton_home() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.now(_tz.utc).isoformat()
            line = {
                "ts": ts,
                "tag": tag,
                "pid": os.getpid(),
                "python": sys.version.split()[0],
                "platform": sys.platform,
                **extra,
            }
            import json as _json

            with open(log_dir / "gateway-exit-diag.log", "a", encoding="utf-8") as f:
                f.write(_json.dumps(line, default=str) + "\n")
        except Exception:
            pass  # never let the diagnostic itself crash the gateway

    _exit_diag(
        "gateway.start",
        replace=replace,
        argv=sys.argv,
        stdin_is_tty=_stdin_is_tty,
        detached=_gateway_detached,
    )

    def _atexit_hook() -> None:
        _exit_diag("atexit.hook", sys_exc=repr(sys.exc_info()))

    _atexit.register(_atexit_hook)

    # App-level respawn-storm circuit breaker, a backstop under systemd's own
    # throttle. Configured via ``gateway.respawn_storm`` in config.toml
    # (``max_starts`` / ``window_seconds``); the env vars
    # ``SON_OF_ANTON_GATEWAY_MAX_STARTS`` / ``SON_OF_ANTON_GATEWAY_START_WINDOW_S``
    # override for escape-hatch use. Set max_starts <= 0 to disable.
    # Best-effort: a bookkeeping failure must never block startup.
    try:
        import time as _time

        from gateway.status import record_start_and_check_storm

        # Defaults mirror config.toml DEFAULT_CONFIG ``gateway.respawn_storm``.
        _max_starts = 5
        _win = 120.0
        try:
            from son_of_anton_cli.config import load_config

            _cfg = load_config()
            _gw = _cfg.get("gateway") if isinstance(_cfg, dict) else None
            _rs = _gw.get("respawn_storm") if isinstance(_gw, dict) else None
            if isinstance(_rs, dict):
                if isinstance(_rs.get("max_starts"), int):
                    _max_starts = _rs["max_starts"]
                if isinstance(_rs.get("window_seconds"), (int, float)):
                    _win = float(_rs["window_seconds"])
        except Exception:
            pass
        # Env vars override config for escape-hatch use.
        try:
            _env_starts = os.getenv("SON_OF_ANTON_GATEWAY_MAX_STARTS")
            if _env_starts is not None:
                _max_starts = int(_env_starts)
        except ValueError:
            pass
        try:
            _env_win = os.getenv("SON_OF_ANTON_GATEWAY_START_WINDOW_S")
            if _env_win is not None:
                _win = float(_env_win)
        except ValueError:
            pass
        _storm = (
            record_start_and_check_storm(max_starts=_max_starts, window_s=_win)
            if _max_starts > 0
            else None
        )
        if _storm is not None:
            logger.warning(
                "Gateway (re)started %d times in %.0fs — backing off %.0fs to break a respawn storm.",
                _storm.count,
                _storm.window_s,
                _storm.backoff_s,
            )
            _time.sleep(_storm.backoff_s)
    except Exception as _be:
        logger.debug("respawn-storm breaker check failed (non-fatal): %s", _be)

    def _hard_exit_after_gateway_teardown(code: int) -> None:
        # ``son-of-anton gateway run`` enters through this CLI wrapper, not through
        # ``gateway.run.main()``.  Mirror that module's wedge-proof exit path:
        # once start_gateway() has completed graceful teardown, bypass Python
        # finalization so non-daemon worker threads (notably in-flight cron
        # ThreadPoolExecutor jobs) cannot keep the old gateway alive and delay a
        # service-managed /restart by minutes.
        from gateway.run import _exit_after_graceful_shutdown

        _exit_after_graceful_shutdown(code)

    success = False
    try:
        success = asyncio.run(start_gateway(verbosity=verbosity))
        _exit_diag("asyncio.run.returned", success=success)
    except KeyboardInterrupt:
        # On detached runs this shouldn't fire (we absorb SIGINT above),
        # but keep the handler for console runs.
        _exit_diag(
            "asyncio.run.KeyboardInterrupt",
            traceback=_traceback.format_exc(),
        )
        print("\nGateway stopped.")
        _hard_exit_after_gateway_teardown(0)
        return  # unreachable in production (os._exit); guard for test stubs
    except SystemExit as e:
        _exit_diag(
            "asyncio.run.SystemExit",
            code=getattr(e, "code", None),
            traceback=_traceback.format_exc(),
        )
        if e.code is None:
            _code = 0
        elif isinstance(e.code, int):
            _code = e.code
        else:
            _code = 1
        _hard_exit_after_gateway_teardown(_code)
    except BaseException as e:
        # Absolutely everything else: Exception, asyncio.CancelledError,
        # even exotic BaseException subclasses. We want the cause logged.
        _exit_diag(
            "asyncio.run.exception",
            exc_type=type(e).__name__,
            exc_repr=repr(e),
            traceback=_traceback.format_exc(),
        )
        raise
    if not success:
        _exit_diag("gateway.exit_nonzero")
        _hard_exit_after_gateway_teardown(1)
    _exit_diag("gateway.exit_clean")
    _hard_exit_after_gateway_teardown(0)


def _runtime_health_lines() -> list[str]:
    """Summarize the latest persisted gateway runtime health state."""
    try:
        from gateway.status import (
            read_runtime_status,
            runtime_status_is_stale,
            runtime_status_pid_is_live,
        )
    except Exception:
        return []

    state = read_runtime_status()
    if not state:
        return []

    lines: list[str] = []
    gateway_state = state.get("gateway_state")
    exit_reason = state.get("exit_reason")
    active_agents = state.get("active_agents")
    restart_requested = state.get("restart_requested")
    platforms = state.get("platforms", {}) or {}

    for platform, pdata in platforms.items():
        if pdata.get("state") == "fatal":
            message = pdata.get("error_message") or "unknown error"
            lines.append(f"⚠ {platform}: {message}")

    # A persisted snapshot that still claims liveness can outlive an
    # ungracefully-killed gateway (SIGKILL, OOM, power loss) whose shutdown
    # handler never ran.  When the record is past its freshness TTL AND the
    # recorded PID is gone, the file is contradicting reality — surface that
    # explicitly instead of rendering the misleading live-state summary.
    if (
        gateway_state in ("running", "starting", "draining")
        and runtime_status_is_stale(state)
        and not runtime_status_pid_is_live(state)
    ):
        lines.append(
            f"⚠ Stale gateway_state.json: recorded state '{gateway_state}' but the "
            "recorded process is gone (likely an ungraceful shutdown)"
        )
        return lines

    if gateway_state == "startup_failed" and exit_reason:
        lines.append(f"⚠ Last startup issue: {exit_reason}")
    elif gateway_state == "draining":
        action = "restart" if restart_requested else "shutdown"
        from gateway.status import parse_active_agents

        count = parse_active_agents(active_agents)
        lines.append(f"⏳ Gateway draining for {action} ({count} active agent(s))")
    elif gateway_state == "stopped" and exit_reason:
        lines.append(f"⚠ Last shutdown reason: {exit_reason}")

    return lines


# =============================================================================
# Main Command Handler
# =============================================================================


def gateway_command(args):
    """Handle gateway subcommands (run, status)."""
    subcmd = getattr(args, "gateway_command", None)

    # Default to run if no subcommand
    if subcmd is None or subcmd == "run":
        run_gateway(
            getattr(args, "verbose", 0),
            quiet=getattr(args, "quiet", False),
            replace=getattr(args, "replace", False),
        )
        return

    if subcmd == "status":
        pids = find_gateway_pids()
        runtime_lines = _runtime_health_lines()
        if pids:
            units = systemd_units_of_pids(pids)
            print(f"✓ Gateway is running (PID: {', '.join(map(str, pids))})")
            if units:
                print(f"  Supervised by systemd: {', '.join(units)}")
            else:
                print("  (Running manually, not as a system service)")
        else:
            print("✗ Gateway is not running")
        if runtime_lines:
            print()
            print("Recent gateway health:")
            for line in runtime_lines:
                print(f"  {line}")
        if not pids:
            print()
            print("To start:")
            print("  son-of-anton gateway run      # Run in foreground")
            print("  or enable the service in the NixOS / Home Manager module")
        elif not units:
            print()
            print("  Run it as a service by enabling it in the NixOS / Home Manager module.")
