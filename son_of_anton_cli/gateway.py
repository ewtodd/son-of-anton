"""
Gateway subcommand for son-of-anton CLI.

Handles: son-of-anton gateway [run|start|stop|restart|status|install|uninstall|setup]
"""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Ensure /bin and /usr/bin are on PATH so launchctl/systemctl are discoverable
# when running under UV's bundled Python which ships a minimal PATH (#3849).
if os.name == "posix":
    _sys_dirs = {"/bin", "/usr/bin", "/usr/sbin", "/sbin"}
    _path_dirs = set(os.environ.get("PATH", "").split(os.pathsep))
    _missing = _sys_dirs - _path_dirs
    if _missing:
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + os.pathsep.join(sorted(_missing))

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

from gateway.config import coerce_systemd_watchdog_seconds, load_gateway_config
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    EXTERNAL_GATEWAY_SUPERVISOR_ENV,
    GATEWAY_FATAL_CONFIG_EXIT_CODE,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    is_gateway_supervisor_process,
    parse_restart_drain_timeout,
)
from son_of_anton_cli.config import (
    get_son_of_anton_home,
    read_raw_config,
)

from son_of_anton_cli.cli_output import (
    print_info,
    print_warning,
    print_error,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Process Management (for manual gateway runs)
# =============================================================================


@dataclass(frozen=True)
class GatewayRuntimeSnapshot:
    manager: str
    service_installed: bool = False
    service_running: bool = False
    gateway_pids: tuple[int, ...] = ()
    service_scope: str | None = None

    @property
    def running(self) -> bool:
        return self.service_running or bool(self.gateway_pids)

    @property
    def has_process_service_mismatch(self) -> bool:
        return self.service_installed and self.running and not self.service_running


def _get_service_pids(all_profiles: bool = False) -> set:
    """Return PIDs currently managed by systemd or launchd gateway services.

    Used to avoid killing freshly-restarted service processes when sweeping
    for stale manual gateway processes after a service restart.  Relies on
    the service manager having committed the new PID before the restart command
    returns (true for both systemd and launchd in practice).

    ``all_profiles`` widens the launchd branch to every installed
    ``ai.son-of-anton.gateway*`` LaunchAgent — the update path needs the whole
    fleet excluded from its sweep (#41403, #73626): sibling-profile launchd
    gateways found by the (BSD-fixed) ps scan must not be misclassified as
    manual processes and killed.  Default-scope callers (``gateway status``,
    cron checks) keep seeing only the current profile's service; the orphan
    reaper passes all_profiles=True for the same friendly-fire reason.  The
    systemd branch has always been fleet-wide (``son-of-anton-gateway*``) and is
    unaffected.
    """
    pids: set = set()

    # --- systemd (Linux): user and system scopes ---
    # systemd always lists every son-of-anton-gateway* unit regardless of scope.
    if supports_systemd_services():
        for scope_args in [["systemctl", "--user"], ["systemctl"]]:
            try:
                result = subprocess.run(
                    scope_args
                    + [
                        "list-units",
                        "son-of-anton-gateway*",
                        "--plain",
                        "--no-legend",
                        "--no-pager",
                    ],
                    capture_output=True,
                    text=True, encoding='utf-8', errors='replace',
                    timeout=5,
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if not parts or not parts[0].endswith(".service"):
                        continue
                    svc = parts[0]
                    try:
                        show = subprocess.run(
                            scope_args + ["show", svc, "--property=MainPID", "--value"],
                            capture_output=True,
                            text=True, encoding='utf-8', errors='replace',
                            timeout=5,
                        )
                        pid = int(show.stdout.strip())
                        if pid > 0:
                            pids.add(pid)
                    except (ValueError, subprocess.TimeoutExpired):
                        pass
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    # --- launchd (macOS) ---
    if is_macos():
        labels = {get_launchd_label()}
        if all_profiles:
            # Every gateway LaunchAgent, not just the invoking profile's —
            # mirrors the systemd branch's ``son-of-anton-gateway*`` pattern above.
            # The update path restarts the whole fleet, and its stale-process
            # sweep must not mistake a sibling service's fresh PID for a
            # manual gateway it should kill (#41403).
            labels.update(launchd_gateway_labels_for_install())
        for label in sorted(labels):
            try:
                _domain, pid = _locate_launchd_gateway_service(label)
            except subprocess.TimeoutExpired:
                continue
            if pid is not None and pid > 0:
                pids.add(pid)
        if all_profiles:
            # Belt-and-suspenders for the EXCLUDE use case (#74075): a bare
            # ``launchctl list`` prefix scan also catches ai.son-of-anton.gateway*
            # agents the label derivation can't map (renamed profiles, other
            # installs sharing this user).  Over-inclusion is safe here —
            # these PIDs are only ever protected from the kill sweep, never
            # targeted.  Restart paths use the label-derived set only.
            try:
                result = subprocess.run(
                    ["launchctl", "list"],
                    capture_output=True,
                    text=True, encoding='utf-8', errors='replace',
                    timeout=5,
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 3 and parts[-1].startswith(
                            "ai.son-of-anton.gateway"
                        ):
                            try:
                                pid = int(parts[0])
                                if pid > 0:
                                    pids.add(pid)
                            except ValueError:
                                pass
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    return pids


def _get_parent_pid(pid: int) -> int | None:
    """Return the parent PID for ``pid``, or ``None`` when unavailable.

    Uses psutil (core dependency) which works on every platform.  The
    older implementation shelled out to ``ps -o ppid= -p <pid>``, which
    silently fails on some hosts so the ancestor walk terminated at self —
    the caller's dedup / exclude logic then couldn't distinguish
    "son-of-anton CLI that invoked this scan" from "real gateway process".
    """
    if pid <= 1:
        return None
    try:
        import psutil  # type: ignore

        return psutil.Process(pid).ppid() or None
    except ImportError:
        pass
    except Exception:
        return None
    # Fallback: shell out to ps (POSIX only).
    if not shutil.which("ps"):
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        parent_pid = int(raw.splitlines()[-1].strip())
    except ValueError:
        return None
    return parent_pid if parent_pid > 0 else None


def _get_ancestor_pids() -> set[int]:
    """Return the set of PIDs in the current process's ancestor chain.

    Walks from the current PID up to PID 1 (init) so that process-table scans
    never match the calling CLI process or any of its parents.  This prevents
    ``son-of-anton gateway status`` from falsely counting the ``son-of-anton`` CLI that
    invoked it as a running gateway instance (see #13242).
    """
    ancestors: set[int] = set()
    pid = os.getpid()
    # Cap iterations to avoid infinite loops on exotic platforms.
    for _ in range(64):
        ancestors.add(pid)
        parent = _get_parent_pid(pid)
        if parent is None or parent <= 0 or parent in ancestors:
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


def _scan_gateway_pids(
    exclude_pids: set[int],
    all_profiles: bool = False,
    include_restart_managers: bool = False,
) -> list[int]:
    """Best-effort process-table scan for gateway PIDs.

    This supplements the profile-scoped PID file so status views can still spot
    a live gateway when the PID file is stale/missing, and ``--all`` sweeps can
    discover gateways outside the current profile.
    """
    # Exclude the entire ancestor chain so the CLI process that invoked this
    # scan (e.g. ``son-of-anton gateway status``) is never mistaken for a running
    # gateway.  See #13242.
    exclude_pids = exclude_pids | _get_ancestor_pids()
    pids: list[int] = []
    # Strict command-line matcher shared with gateway.status: requires the
    # actual ``gateway run`` subcommand (or the dedicated entrypoints), so this
    # scan no longer false-matches ``gateway status``/``dashboard`` siblings or
    # unrelated processes like ``python -m tui_gateway``. Lazy import mirrors the
    # circular-import avoidance used elsewhere in this module.
    from gateway.status import (
        looks_like_gateway_command_line,
        looks_like_gateway_runtime_command_line,
    )
    current_home = str(get_son_of_anton_home().resolve())
    # Forward slashes on both sides of the SON_OF_ANTON_HOME= match — see
    # gateway.status._command_line_belongs_to_profile, which this mirrors.
    current_home_lc = current_home.lower().replace("\\", "/")
    current_profile_arg = _profile_arg(current_home)
    current_profile_name = (
        current_profile_arg.split()[-1] if current_profile_arg else ""
    )
    current_profile_name_lc = current_profile_name.lower()

    def _matches_current_profile(command: str) -> bool:
        command_lc = command.lower().replace("\\", "/")
        if current_profile_name:
            return (
                f"--profile {current_profile_name_lc}" in command_lc
                or f"-p {current_profile_name_lc}" in command_lc
                or f"son_of_anton_home={current_home_lc}" in command_lc
            )

        # Default-profile case: no profile flag in argv. Accept as long as
        # the command doesn't advertise *some other* profile. SON_OF_ANTON_HOME
        # may be passed via env (not visible in wmic/CIM command line) so
        # its absence is NOT disqualifying — only a non-matching explicit
        # SON_OF_ANTON_HOME= in argv is.
        if "--profile " in command_lc or " -p " in command_lc:
            return False
        if (
            "son_of_anton_home=" in command_lc
            and f"son_of_anton_home={current_home_lc}" not in command_lc
        ):
            return False
        return True

    def _matches_gateway_runtime(command: str) -> bool:
        if looks_like_gateway_command_line(command):
            return True
        return include_restart_managers and looks_like_gateway_runtime_command_line(command)

    try:
        # Try /proc first (no external process tooling required),
        # fall back to `ps -Aww` (BSD-safe; see below).
        _found_via_proc = False
        if os.path.isdir("/proc"):
            try:
                my_pid = os.getpid()
                for entry in os.listdir("/proc"):
                    if not entry.isdigit():
                        continue
                    pid = int(entry)
                    if pid == my_pid or pid in exclude_pids:
                        continue
                    try:
                        with open(f"/proc/{pid}/cmdline", "rb") as _f:
                            cmdline = _f.read().decode("utf-8", errors="replace")
                        cmdline = cmdline.replace("\x00", " ")
                        if _matches_gateway_runtime(cmdline) and (
                            all_profiles or _matches_current_profile(cmdline)
                        ):
                            _append_unique_pid(pids, pid, exclude_pids)
                    except (OSError, PermissionError):
                        continue
                _found_via_proc = True
            except Exception:
                pass

        if not _found_via_proc:
            result = subprocess.run(
                # ``-Aww`` (not ``-A eww``): the BSD ``e`` flag (show
                # environment) is illegal on macOS/BSD ps and makes the
                # whole command fail with rc 1, silently returning [] on
                # every macOS machine (#73626).  The matcher only needs
                # argv, not env vars, so ``e`` is unnecessary.  ``-ww``
                # keeps unlimited-width output on both BSD and procps ps.
                ["ps", "-Aww", "-o", "pid=,command="],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=10,
            )
            if result.returncode != 0:
                return []
            for line in result.stdout.split("\n"):
                stripped = line.strip()
                if not stripped or "grep" in stripped:
                    continue

                pid = None
                command = ""

                parts = stripped.split(None, 1)
                if len(parts) == 2:
                    try:
                        pid = int(parts[0])
                        command = parts[1]
                    except ValueError:
                        pid = None

                if pid is None:
                    aux_parts = stripped.split()
                    if len(aux_parts) > 10 and aux_parts[1].isdigit():
                        pid = int(aux_parts[1])
                        command = " ".join(aux_parts[10:])

                if pid is None:
                    continue
                if _matches_gateway_runtime(command) and (
                    all_profiles or _matches_current_profile(command)
                ):
                    _append_unique_pid(pids, pid, exclude_pids)
    except (OSError, subprocess.TimeoutExpired):
        return []

    return pids


def find_gateway_pids(
    exclude_pids: set | None = None, all_profiles: bool = False
) -> list:
    """Find PIDs of running gateway processes.

    Args:
        exclude_pids: PIDs to exclude from the result (e.g. service-managed
            PIDs that should not be killed during a stale-process sweep).
        all_profiles: When ``True``, return gateway PIDs across **all**
            profiles (the pre-7923 global behaviour).  ``son-of-anton update``
            needs this because a code update affects every profile.
            When ``False`` (default), only PIDs belonging to the current
            Son of Anton profile are returned.
    """
    _exclude = set(exclude_pids or set())
    pids: list[int] = []
    if not all_profiles:
        try:
            from gateway.status import get_running_pid

            _append_unique_pid(pids, get_running_pid(), _exclude)
        except Exception:
            pass
    for pid in _get_service_pids(all_profiles=all_profiles):
        _append_unique_pid(pids, pid, _exclude)
    try:
        include_restart_managers = not supports_systemd_services()
    except Exception:
        include_restart_managers = False
    for pid in _scan_gateway_pids(
        _exclude,
        all_profiles=all_profiles,
        include_restart_managers=include_restart_managers,
    ):
        _append_unique_pid(pids, pid, _exclude)
    return pids


def _probe_systemd_service_running(system: bool = False) -> tuple[bool, bool]:
    selected_system = _select_systemd_scope(system)
    unit_exists = get_systemd_unit_path(system=selected_system).exists()
    if not unit_exists:
        return selected_system, False
    try:
        result = _run_systemctl(
            ["is-active", get_service_name()],
            system=selected_system,
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=10,
        )
    except (RuntimeError, subprocess.TimeoutExpired):
        return selected_system, False
    return selected_system, result.stdout.strip() == "active"


def _read_systemd_unit_environment(system: bool = False) -> dict[str, str]:
    """Parse the gateway unit's ``Environment=`` directives.

    ``systemctl show -p Environment`` returns a single line of
    space-separated ``KEY=VALUE`` pairs; values are not quoted in the output
    even when the unit file quoted them. We split on whitespace and ``=``.
    """
    selected_system = _select_systemd_scope(system)
    try:
        result = _run_systemctl(
            [
                "show",
                get_service_name(),
                "--no-pager",
                "--property",
                "Environment",
            ],
            system=selected_system,
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=10,
        )
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return {}
    if result.returncode != 0:
        return {}
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.startswith("Environment="):
            continue
        body = line[len("Environment=") :].strip()
        for token in body.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            parsed[key] = value
    return parsed


def _son_of_anton_home_from_systemd_unit_file(system: bool = False) -> str | None:
    """Read ``SON_OF_ANTON_HOME`` from the on-disk unit file (not ``systemctl show``).

    Prefer the file when refreshing/comparing: under ``sudo``, ``systemctl``
    may be slow/unavailable in tests, and the on-disk unit is what
    ``systemd_unit_is_current`` / ``refresh_systemd_unit_if_needed`` already
    compare against.
    """
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists():
        return None
    try:
        text = unit_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        body = stripped[len("Environment=") :].strip().strip('"')
        if body.startswith("SON_OF_ANTON_HOME="):
            value = body.split("=", 1)[1].strip().strip('"')
            return value or None
    return None


def _sync_son_of_anton_home_from_systemd_unit(system: bool) -> None:
    """When acting on a system-scope unit, adopt its ``SON_OF_ANTON_HOME``.

    Under ``sudo``, ``SON_OF_ANTON_HOME`` is stripped and ``HOME=/root``, so
    :func:`get_son_of_anton_home` falls back to ``/root/.son-of-anton`` — the wrong
    profile. The unit file pins ``SON_OF_ANTON_HOME`` for the actual gateway
    process, so we mirror that into our own environment to make
    ``read_runtime_status`` / ``get_running_pid`` read the correct files.
    """
    if not system:
        return
    # Prefer the on-disk unit (source of truth for refresh/compare). Fall
    # back to ``systemctl show`` for units that only exist in the manager.
    unit_home = (_son_of_anton_home_from_systemd_unit_file(system=True) or "").strip()
    if not unit_home:
        unit_home = _read_systemd_unit_environment(system=True).get("SON_OF_ANTON_HOME", "").strip()
    if not unit_home:
        return
    current = os.environ.get("SON_OF_ANTON_HOME", "").strip()
    if current == unit_home:
        return
    os.environ["SON_OF_ANTON_HOME"] = unit_home


def _read_systemd_unit_properties(
    system: bool = False,
    properties: tuple[str, ...] = (
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainStatus",
        "MainPID",
    ),
) -> dict[str, str]:
    """Return selected ``systemctl show`` properties for the gateway unit."""
    selected_system = _select_systemd_scope(system)
    try:
        result = _run_systemctl(
            [
                "show",
                get_service_name(),
                "--no-pager",
                "--property",
                ",".join(properties),
            ],
            system=selected_system,
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=10,
        )
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return {}

    if result.returncode != 0:
        return {}

    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key] = value.strip()
    return parsed


def _systemd_unit_is_start_limited(props: dict[str, str]) -> bool:
    result = props.get("Result", "").lower()
    sub_state = props.get("SubState", "").lower()
    return result == "start-limit-hit" or sub_state == "start-limit-hit"


def _parse_launchd_pid_from_list_output(output: str) -> int | None:
    """Extract the PID from ``launchctl list <label>`` output.

    When launchd is actively supervising a process, the output includes a
    ``"PID" = <number>;`` line.  When the service definition is only *registered*
    but not running (macOS 26+ with an unmanageable domain, fallback active),
    the output lacks a PID field entirely.  Returns ``None`` when no PID is
    found or the PID is non-positive (e.g. ``-1`` for a recently-crashed service).
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith('"PID"') or stripped.startswith("PID"):
            parts = stripped.split("=", 1)
            if len(parts) == 2:
                val = parts[1].strip().rstrip(";").strip('"')
                try:
                    pid = int(val)
                    return pid if pid > 0 else None
                except ValueError:
                    return None
    return None


def _parse_launchd_pid_from_print_output(output: str) -> int | None:
    """Extract the live PID from ``launchctl print`` output (``pid = <N>``).

    A bootstrapped-but-not-running service prints no ``pid =`` line; the
    first (service-level) occurrence wins over any nested endpoint state.
    Returns ``None`` when no PID is found or the PID is non-positive.
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            try:
                pid = int(stripped[len("pid = "):].strip())
                return pid if pid > 0 else None
            except ValueError:
                return None
    return None


def _launchd_print_service_pid(domain: str, label: str) -> tuple[bool, int | None]:
    """Return ``(loaded, pid)`` for ``domain/label`` via ``launchctl print``.

    Domain-explicit on purpose: legacy ``launchctl list`` infers its domain
    from the caller's execution context, which is exactly the ambiguity that
    sank the first fleet-restart attempt (#41403 review). ``TimeoutExpired``
    propagates — fleet-restart callers own per-label failure accounting (a
    wedged launchctl call must be reported, not read as "unloaded").
    """
    try:
        result = subprocess.run(
            ["launchctl", "print", f"{domain}/{label}"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=5,
        )
    except FileNotFoundError:
        return (False, None)
    if result.returncode != 0:
        return (False, None)
    return (True, _parse_launchd_pid_from_print_output(result.stdout))


def _locate_launchd_gateway_service(label: str) -> tuple[str | None, int | None]:
    """Return ``(domain, pid)`` for ``label``, probing both per-user domains.

    Probes ``gui/<uid>`` first (Aqua sessions), then ``user/<uid>``
    (Background/SSH sessions).  ``domain`` is None when the label is not
    bootstrapped in either; ``pid`` is None when the service has no live
    process.  Sibling profile services resolve independently — a fleet can
    legitimately mix domains (a profile installed over SSH lands in
    ``user/<uid>`` while the rest live in ``gui/<uid>``), so the current
    profile's cached domain (``_launchd_domain()``) is never consulted.
    ``TimeoutExpired`` propagates (see ``_launchd_print_service_pid``).
    """
    uid = os.getuid()  # windows-footgun: ok — POSIX launchd (macOS) helper, never invoked on Windows
    for domain in (f"gui/{uid}", f"user/{uid}"):
        loaded, pid = _launchd_print_service_pid(domain, label)
        if loaded:
            return (domain, pid)
    return (None, None)


def _probe_launchd_service_running() -> bool:
    """Return True when launchd is actively supervising the gateway process.

    ``launchctl list <label>`` returns exit 0 whenever the service definition is
    registered with launchd — even when ``state = not running`` (macOS 26+).
    We additionally require a PID in the output to confirm launchd is actually
    managing a live process, not just holding a static definition.
    """
    if not get_launchd_plist_path().exists():
        return False
    try:
        result = subprocess.run(
            ["launchctl", "list", get_launchd_label()],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        return False
    return _parse_launchd_pid_from_list_output(result.stdout) is not None


def get_gateway_runtime_snapshot(system: bool = False) -> GatewayRuntimeSnapshot:
    """Return a unified view of gateway liveness for the current profile."""
    gateway_pids = tuple(find_gateway_pids())

    if supports_systemd_services():
        selected_system, service_running = _probe_systemd_service_running(system=system)
        scope_label = _service_scope_label(selected_system)
        return GatewayRuntimeSnapshot(
            manager=f"systemd ({scope_label})",
            service_installed=get_systemd_unit_path(system=selected_system).exists(),
            service_running=service_running,
            gateway_pids=gateway_pids,
            service_scope=scope_label,
        )

    if is_macos():
        return GatewayRuntimeSnapshot(
            manager="launchd",
            service_installed=get_launchd_plist_path().exists(),
            service_running=_probe_launchd_service_running(),
            gateway_pids=gateway_pids,
            service_scope="launchd",
        )

    return GatewayRuntimeSnapshot(
        manager="manual process",
        gateway_pids=gateway_pids,
    )


def _format_gateway_pids(
    pids: tuple[int, ...] | list[int], *, limit: int | None = 3
) -> str:
    rendered = (
        [str(pid) for pid in pids[:limit] if pid > 0]
        if limit is not None
        else [str(pid) for pid in pids if pid > 0]
    )
    if limit is not None and len(pids) > limit:
        rendered.append("...")
    return ", ".join(rendered)


def _print_gateway_process_mismatch(snapshot: GatewayRuntimeSnapshot) -> None:
    if not snapshot.has_process_service_mismatch:
        return
    print()
    # Distinguish the managed detached fallback (macOS launchd exit-5 path)
    # from a genuinely manual foreground/tmux/nohup run.
    if _launchd_unsupported_marker_exists():
        print(
            "⚠ Gateway is running as a detached fallback process — "
            "launchd cannot supervise it"
        )
        print(f"  PID(s): {_format_gateway_pids(snapshot.gateway_pids, limit=None)}")
        print("  Auto-start at login and auto-restart on crash are NOT available.")
        print("  Stop it with: son-of-anton gateway stop")
    else:
        print(
            "⚠ Gateway process is running for this profile, but the service is not active"
        )
        print(f"  PID(s): {_format_gateway_pids(snapshot.gateway_pids, limit=None)}")
        print("  This is usually a manual foreground/tmux/nohup run, so `son-of-anton gateway`")
        print("  can refuse to start another copy until this process stops.")


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def supports_systemd_services() -> bool:
    if not is_linux():
        return False
    if shutil.which("systemctl") is None:
        return False
    return True


def is_macos() -> bool:
    return sys.platform == "darwin"


def _profile_suffix() -> str:
    """Derive a service-name suffix from the current SON_OF_ANTON_HOME.

    Returns ``""`` for the default root, the profile name for
    ``<root>/profiles/<name>``, or a short hash for any other path.
    Works correctly in Docker (SON_OF_ANTON_HOME=/opt/data) and standard deployments.
    """
    import hashlib
    import re
    from son_of_anton_constants import get_default_son_of_anton_root

    home = get_son_of_anton_home().resolve()
    default = get_default_son_of_anton_root().resolve()
    if home == default:
        return ""
    # Detect <root>/profiles/<name> pattern → use the profile name
    profiles_root = (default / "profiles").resolve()
    try:
        rel = home.relative_to(profiles_root)
        parts = rel.parts
        if len(parts) == 1 and re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", parts[0]):
            return parts[0]
    except ValueError:
        pass
    # Fallback: short hash for arbitrary SON_OF_ANTON_HOME paths
    return hashlib.sha256(str(home).encode()).hexdigest()[:8]


def _profile_arg(son_of_anton_home: str | None = None, default_root: str | Path | None = None) -> str:
    """Return ``--profile <name>`` only when SON_OF_ANTON_HOME is a named profile.

    For ``~/.son-of-anton/profiles/<name>``, returns ``"--profile <name>"``.
    For the default profile or hash-based custom paths, returns the empty string.

    Args:
        son_of_anton_home: Optional explicit SON_OF_ANTON_HOME path. Defaults to the current
            ``get_son_of_anton_home()`` value. Should be passed when generating a
            service definition for a different user (e.g. system service).
        default_root: Optional Son of Anton root to compare against. Used when
            generating a system service for another user from a sudo/root
            process, where ``Path.home()`` and ``get_default_son_of_anton_root()``
            refer to root but the target profile lives under the service user.
    """
    import re
    from son_of_anton_constants import get_default_son_of_anton_root

    home = Path(son_of_anton_home or str(get_son_of_anton_home())).resolve()
    default = Path(default_root).resolve() if default_root else get_default_son_of_anton_root().resolve()
    if home == default:
        return ""
    profiles_root = (default / "profiles").resolve()
    try:
        rel = home.relative_to(profiles_root)
        parts = rel.parts
        if len(parts) == 1 and re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", parts[0]):
            return f"--profile {parts[0]}"
    except ValueError:
        pass
    return ""


def _profile_arg_for_target_user(son_of_anton_home: str, target_home_dir: str) -> str:
    """Return the profile arg for a system service running as another user."""
    target_root = Path(target_home_dir) / ".son-of-anton"
    try:
        Path(son_of_anton_home).resolve().relative_to(target_root.resolve())
        return _profile_arg(son_of_anton_home, default_root=target_root)
    except ValueError:
        return _profile_arg(son_of_anton_home)


# Systemd/launchd service identity for this SON_OF_ANTON_HOME (restored: the
# service-name helpers below reference them; the unit templates embed
# SERVICE_DESCRIPTION).
_SERVICE_BASE = "son-of-anton-gateway"
SERVICE_DESCRIPTION = "Son of Anton Agent Gateway - Messaging Platform Integration"


def get_service_name() -> str:
    """Derive a systemd service name scoped to this SON_OF_ANTON_HOME.

    Default ``~/.son-of-anton`` returns ``son-of-anton-gateway`` (backward compatible).
    Profile ``~/.son-of-anton/profiles/coder`` returns ``son-of-anton-gateway-coder``.
    Any other SON_OF_ANTON_HOME appends a short hash for uniqueness.
    """
    suffix = _profile_suffix()
    if not suffix:
        return _SERVICE_BASE
    return f"{_SERVICE_BASE}-{suffix}"


def get_systemd_unit_path(system: bool = False) -> Path:
    name = get_service_name()
    if system:
        return Path("/etc/systemd/system") / f"{name}.service"
    return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"


def _path_exists_safe(path: Path) -> bool:
    """``Path.exists()`` that treats an inaccessible path as absent.

    ``Path.exists()`` only swallows a subset of ``OSError`` (ENOENT/ENOTDIR/
    EBADF/ELOOP); ``EACCES`` still propagates. When ``XDG_RUNTIME_DIR`` leaks
    from another user — the classic ``su``/``sudo -u`` from a root shell case,
    where ``/run/user/0`` is ``0700 root:root`` — stat-ing a socket underneath
    it raises ``PermissionError`` that escapes the systemd preflight as a raw
    traceback (#86558). An unreadable path is, for our purposes, not reachable.
    """
    try:
        return path.exists()
    except OSError:  # e.g. EACCES on another user's runtime dir
        return False


def _runtime_dir_is_ours(runtime_dir: str) -> bool:
    """True when *runtime_dir* exists and is owned by the current uid.

    A leaked ``XDG_RUNTIME_DIR`` belonging to another user must not be trusted:
    its sockets are unreadable (``EACCES``) and its bus is not ours to drive.
    """
    try:
        return Path(runtime_dir).stat().st_uid == os.getuid()  # windows-footgun: ok — POSIX systemd helper, never invoked on Windows
    except OSError:
        return False


def _ensure_user_systemd_env() -> None:
    """Ensure DBUS_SESSION_BUS_ADDRESS and XDG_RUNTIME_DIR are set for systemctl --user.

    On headless servers (SSH sessions), these env vars may be missing even when
    the user's systemd instance is running (via linger).  Without them,
    ``systemctl --user`` fails with "Failed to connect to bus: No medium found".
    We detect the standard socket path and set the vars so all subsequent
    subprocess calls inherit them.

    An ``XDG_RUNTIME_DIR`` that leaked from another user (``su``/``sudo -u``
    from root, where the env still points at ``/run/user/0``) is dropped in
    favour of our own ``/run/user/{uid}`` so ``systemctl --user`` targets the
    right instance instead of an unreadable foreign socket (#86558).
    """
    uid = os.getuid()  # windows-footgun: ok — POSIX systemd helper, never invoked on Windows
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if not xdg or not _runtime_dir_is_ours(xdg):
        runtime_dir = f"/run/user/{uid}"
        if _runtime_dir_is_ours(runtime_dir):
            os.environ["XDG_RUNTIME_DIR"] = runtime_dir

    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        bus_path = Path(xdg_runtime) / "bus"
        if _path_exists_safe(bus_path):
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"


def _systemctl_cmd(system: bool = False) -> list[str]:
    if not system:
        _ensure_user_systemd_env()
    return ["systemctl"] if system else ["systemctl", "--user"]


def _journalctl_cmd(system: bool = False) -> list[str]:
    return ["journalctl"] if system else ["journalctl", "--user"]


def _run_systemctl(
    args: list[str], *, system: bool = False, **kwargs
) -> subprocess.CompletedProcess:
    """Run a systemctl command, raising RuntimeError if systemctl is missing.

    Defense-in-depth: callers are gated by ``supports_systemd_services()``,
    but this ensures any future caller that bypasses the gate still gets a
    clear error instead of a raw ``FileNotFoundError`` traceback.
    """
    try:
        return subprocess.run(_systemctl_cmd(system) + args, **kwargs)
    except FileNotFoundError:
        raise RuntimeError("systemctl is not available on this system") from None


def _service_scope_label(system: bool = False) -> str:
    return "system" if system else "user"


def get_installed_systemd_scopes() -> list[str]:
    scopes = []
    seen_paths: set[Path] = set()
    for system, label in ((False, "user"), (True, "system")):
        unit_path = get_systemd_unit_path(system=system)
        if unit_path in seen_paths:
            continue
        if unit_path.exists():
            scopes.append(label)
            seen_paths.add(unit_path)
    return scopes


def has_conflicting_systemd_units() -> bool:
    return len(get_installed_systemd_scopes()) > 1


# Legacy service names from older Son of Anton installs that predate the
# son-of-anton-gateway rename. Kept as an explicit allowlist (NOT a glob) so
# profile units (son-of-anton-gateway-*.service) and unrelated third-party
# "son-of-anton" units are never matched.
_LEGACY_SERVICE_NAMES: tuple[str, ...] = ("son-of-anton.service",)

# ExecStart content markers that identify a unit as running our gateway.
# A legacy unit is only flagged when its file contains one of these.
_LEGACY_UNIT_EXECSTART_MARKERS: tuple[str, ...] = (
    "son_of_anton_cli.main gateway",
    "son_of_anton_cli/main.py gateway",
    "gateway/run.py",
    " son-of-anton gateway ",
    "/son-of-anton gateway ",
)


def _legacy_unit_search_paths() -> list[tuple[bool, Path]]:
    """Return ``[(is_system, base_dir), ...]`` — directories to scan for legacy units.

    Factored out so tests can monkeypatch the search roots without touching
    real filesystem paths.
    """
    return [
        (False, Path.home() / ".config" / "systemd" / "user"),
        (True, Path("/etc/systemd/system")),
    ]


def _find_legacy_son_of_anton_units() -> list[tuple[str, Path, bool]]:
    """Return ``[(unit_name, unit_path, is_system)]`` for legacy Son of Anton gateway units.

    Detects unit files installed by older Son of Anton versions that used a
    different service name (e.g. ``son-of-anton.service`` before the rename to
    ``son-of-anton-gateway.service``). When both a legacy unit and the current
    ``son-of-anton-gateway.service`` are active, they fight over the same bot
    token — the PR #5646 signal-recovery change turns this into a 30-second
    SIGTERM flap loop.

    Safety guards:

    * Explicit allowlist of legacy names (no globbing). Profile units such
      as ``son-of-anton-gateway-coder.service`` and unrelated third-party
      ``son-of-anton-*`` services are never matched.
    * ExecStart content check — only flag units that invoke our gateway
      entrypoint. A user-created ``son-of-anton.service`` running an unrelated
      binary is left untouched.
    * Results are returned purely for caller inspection; this function
      never mutates or removes anything.
    """
    results: list[tuple[str, Path, bool]] = []
    for is_system, base in _legacy_unit_search_paths():
        for name in _LEGACY_SERVICE_NAMES:
            unit_path = base / name
            try:
                if not unit_path.exists():
                    continue
                text = unit_path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, PermissionError):
                continue
            if not any(marker in text for marker in _LEGACY_UNIT_EXECSTART_MARKERS):
                # Not our gateway — leave alone
                continue
            results.append((name, unit_path, is_system))
    return results


def has_legacy_son_of_anton_units() -> bool:
    """Return True when any legacy Son of Anton gateway unit files exist."""
    return bool(_find_legacy_son_of_anton_units())


def print_legacy_unit_warning() -> None:
    """Warn about legacy Son of Anton gateway unit files if any are installed.

    Idempotent: prints nothing when no legacy units are detected. Safe to
    call from any status/install/setup path.
    """
    legacy = _find_legacy_son_of_anton_units()
    if not legacy:
        return
    print_warning("Legacy Son of Anton gateway unit(s) detected from an older install:")
    for name, path, is_system in legacy:
        scope = "system" if is_system else "user"
        print_info(f"    {path}  ({scope} scope)")
    print_info("  These run alongside the current son-of-anton-gateway service and")
    print_info("  cause SIGTERM flap loops — both try to use the same bot token.")
    print_info("  Remove them with:")
    print_info("    son-of-anton gateway migrate-legacy")


def print_systemd_scope_conflict_warning() -> None:
    scopes = get_installed_systemd_scopes()
    if len(scopes) < 2:
        return

    rendered_scopes = " + ".join(scopes)
    print_warning(
        f"Both user and system gateway services are installed ({rendered_scopes})."
    )
    print_info("  This is confusing and can make start/stop/status behavior ambiguous.")
    print_info(
        "  Default gateway commands target the user service unless you pass --system."
    )
    print_info("  Keep one of these:")
    print_info("    son-of-anton gateway uninstall")
    print_info("    sudo son-of-anton gateway uninstall --system")


def _system_service_identity(run_as_user: str | None = None) -> tuple[str, str, str]:
    import getpass
    import grp
    import pwd

    username = (
        run_as_user
        or os.getenv("SUDO_USER")
        or os.getenv("USER")
        or os.getenv("LOGNAME")
        or getpass.getuser()
    ).strip()
    if not username:
        raise ValueError(
            "Could not determine which user the gateway service should run as"
        )
    if username == "root" and not run_as_user:
        raise ValueError(
            "Refusing to install the gateway system service as root; pass --run-as-user root to override (e.g. in LXC containers)"
        )
    if username == "root":
        print_warning("Installing gateway service to run as root.")
        print_info(
            "  This is fine for LXC/container environments but not recommended on bare-metal hosts."
        )

    try:
        user_info = pwd.getpwnam(username)
    except KeyError as e:
        raise ValueError(f"Unknown user: {username}") from e

    group_name = grp.getgrgid(user_info.pw_gid).gr_name
    return username, group_name, user_info.pw_dir


def _read_systemd_user_from_unit(unit_path: Path) -> str | None:
    if not unit_path.exists():
        return None

    for line in unit_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("User="):
            value = line.split("=", 1)[1].strip()
            return value or None
    return None


def get_systemd_linger_status() -> tuple[bool | None, str]:
    """Return systemd linger status for the current user.

    Returns:
        (True, "") when linger is enabled.
        (False, "") when linger is disabled.
        (None, detail) when the status could not be determined.
    """
    if not is_linux():
        return None, "not supported on this platform"

    if not shutil.which("loginctl"):
        return None, "loginctl not found"

    username = os.getenv("USER") or os.getenv("LOGNAME")
    if not username:
        try:
            import pwd

            username = pwd.getpwuid(os.getuid()).pw_name  # windows-footgun: ok — POSIX loginctl helper, never invoked on Windows
        except Exception:
            return None, "could not determine current user"

    try:
        result = subprocess.run(
            ["loginctl", "show-user", username, "--property=Linger", "--value"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            check=False,
            timeout=10,
        )
    except Exception as e:
        return None, str(e)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        return None, detail or "loginctl query failed"

    value = (result.stdout or "").strip().lower()
    if value in {"yes", "true", "1"}:
        return True, ""
    if value in {"no", "false", "0"}:
        return False, ""

    rendered = value or "<empty>"
    return None, f"unexpected loginctl output: {rendered}"


def print_systemd_linger_guidance() -> None:
    """Print the current linger status and the fix when it is disabled."""
    linger_enabled, linger_detail = get_systemd_linger_status()
    if linger_enabled is True:
        print("✓ Systemd linger is enabled (service survives logout)")
    elif linger_enabled is False:
        print("⚠ Systemd linger is disabled (gateway may stop when you log out)")
        print("  Run: sudo loginctl enable-linger $USER")
    else:
        print(f"⚠ Could not verify systemd linger ({linger_detail})")
        print("  If you want the gateway user service to survive logout, run:")
        print("  sudo loginctl enable-linger $USER")


def _launchd_user_home() -> Path:
    """Return the real macOS user home for launchd artifacts.

    Profile-mode Son of Anton often sets ``HOME`` to a profile-scoped directory, but
    launchd user agents still live under the actual account home.
    """
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)  # windows-footgun: ok — POSIX launchd (macOS) helper, never invoked on Windows


def get_launchd_plist_path() -> Path:
    """Return the launchd plist path, scoped per profile.

    Default ``~/.son-of-anton`` → ``ai.son-of-anton.gateway.plist`` (backward compatible).
    Profile ``~/.son-of-anton/profiles/coder`` → ``ai.son-of-anton.gateway-coder.plist``.
    """
    suffix = _profile_suffix()
    name = f"ai.son-of-anton.gateway-{suffix}" if suffix else "ai.son-of-anton.gateway"
    return _launchd_user_home() / "Library" / "LaunchAgents" / f"{name}.plist"


def launchd_gateway_labels_for_install() -> list[str]:
    """Return this install's launchd gateway label.

    NOT derived by globbing ``~/Library/LaunchAgents``: that directory is
    shared per-user, so a sandboxed ``SON_OF_ANTON_HOME`` (tests, capture
    sandboxes, side-by-side installs) must never enumerate — let alone
    restart — another install's services.
    """
    return ["ai.son-of-anton.gateway"]


def _detect_venv_dir() -> Path | None:
    """Detect the active virtualenv directory.

    Checks ``sys.prefix`` first (works regardless of the directory name),
    then ``VIRTUAL_ENV`` env var (covers uv-managed environments where
    sys.prefix == sys.base_prefix), then falls back to probing common
    directory names under PROJECT_ROOT.
    Returns ``None`` when no virtualenv can be found.
    """
    # If we're running inside a virtualenv, sys.prefix points to it.
    if sys.prefix != sys.base_prefix:
        venv = Path(sys.prefix)
        if venv.is_dir():
            return venv

    # uv and some other tools set VIRTUAL_ENV without changing sys.prefix.
    # This catches `uv run` where sys.prefix == sys.base_prefix but the
    # environment IS a venv.  (#8620)
    _virtual_env = os.environ.get("VIRTUAL_ENV")
    if _virtual_env:
        venv = Path(_virtual_env)
        if venv.is_dir():
            return venv

    return None


def get_python_path() -> str:
    venv = _detect_venv_dir()
    if venv is not None:
        from son_of_anton_constants import venv_python_path

        venv_python = venv_python_path(venv)
        if venv_python.exists():
            return str(venv_python)
    return sys.executable


# =============================================================================
# Systemd (Linux)
# =============================================================================


def _build_user_local_paths(home: Path, path_entries: list[str]) -> list[str]:
    """Return user-local bin dirs that exist and aren't already in *path_entries*."""
    candidates = [
        str(home / ".local" / "bin"),  # uv, uvx, pip-installed CLIs
        str(home / ".cargo" / "bin"),  # Rust/cargo tools
        str(home / "go" / "bin"),  # Go tools
        str(home / ".npm-global" / "bin"),  # npm global packages
    ]
    return [p for p in candidates if p not in path_entries and Path(p).exists()]


def _remap_path_for_user(path: str, target_home_dir: str) -> str:
    """Remap *path* from the current user's home to *target_home_dir*.

    If *path* lives under ``Path.home()`` the corresponding prefix is swapped
    to *target_home_dir*; otherwise the path is returned unchanged.

      /root/.son-of-anton/son-of-anton  -> /home/alice/.son-of-anton/son-of-anton
      /opt/son-of-anton                 -> /opt/son-of-anton  (kept as-is)

    Note: this function intentionally does NOT resolve symlinks. A venv's
    ``bin/python`` is typically a symlink to the base interpreter (e.g. a
    uv-managed CPython at ``~/.local/share/uv/python/.../python3.11``);
    resolving that symlink swaps the unit's ``ExecStart`` to a bare Python
    that has none of the venv's site-packages, so the service crashes on
    the first ``import``. Keep the symlinked path so the venv activates
    its own environment. Lexical expansion only via ``expanduser``.
    """
    current_home = Path.home()
    p = Path(path).expanduser()
    try:
        relative = p.relative_to(current_home)
        return str(Path(target_home_dir) / relative)
    except ValueError:
        return str(p)


def _son_of_anton_home_for_target_user(target_home_dir: str) -> str:
    """Remap the current SON_OF_ANTON_HOME to the equivalent under a target user's home.

    When installing a system service via sudo, get_son_of_anton_home() resolves to
    root's home.  This translates it to the target user's equivalent path:
      /root/.son-of-anton                    → /home/alice/.son-of-anton
      /root/.son-of-anton/profiles/coder     → /home/alice/.son-of-anton/profiles/coder
      /opt/custom-son-of-anton               → /opt/custom-son-of-anton  (kept as-is)
    """
    current_son_of_anton_raw = os.environ.get("SON_OF_ANTON_HOME", "").strip()
    current_son_of_anton = (
        Path(current_son_of_anton_raw).expanduser()
        if current_son_of_anton_raw
        else get_son_of_anton_home()
    )
    # Keep explicit custom paths lexical. Resolving a non-existent custom path
    # can rewrite it through host-specific path mappings, which would bake a
    # different SON_OF_ANTON_HOME into the generated service unit.
    current_default = Path.home() / ".son-of-anton"
    target_default = Path(target_home_dir) / ".son-of-anton"

    # Default ~/.son-of-anton → remap to target user's default
    if current_son_of_anton == current_default:
        return str(target_default)

    # Profile or subdir of ~/.son-of-anton → preserve the relative structure
    try:
        relative = current_son_of_anton.relative_to(current_default)
        return str(target_default / relative)
    except ValueError:
        # Completely custom path (not under ~/.son-of-anton) — keep as-is
        return str(current_son_of_anton)


def _build_service_path_dirs(project_root: Path | None = None) -> list[str]:
    """Build PATH directory list for service units, excluding non-existent dirs."""
    if project_root is None:
        project_root = PROJECT_ROOT

    def _is_dir(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False

    candidates = []

    venv_bin = project_root / "venv" / "bin"
    if _is_dir(venv_bin):
        candidates.append(str(venv_bin))
    elif sys.prefix != sys.base_prefix:
        candidates.append(str(Path(sys.prefix) / "bin"))

    node_bin = project_root / "node_modules" / ".bin"
    if _is_dir(node_bin):
        candidates.append(str(node_bin))

    son_of_anton_home = get_son_of_anton_home()
    son_of_anton_node = son_of_anton_home / "node" / "bin"
    if _is_dir(son_of_anton_node):
        candidates.append(str(son_of_anton_node))
    son_of_anton_nm = son_of_anton_home / "node_modules" / ".bin"
    if _is_dir(son_of_anton_nm):
        candidates.append(str(son_of_anton_nm))

    return candidates


def _stable_service_working_dir() -> str:
    """Return a WorkingDirectory that will not disappear out from under systemd.

    The gateway does NOT need its cwd to be the source checkout — ``ExecStart``
    uses an absolute python interpreter and ``-m son_of_anton_cli.main``, so module
    resolution does not depend on cwd. Pinning ``WorkingDirectory`` to
    ``PROJECT_ROOT`` (``Path(__file__).parent.parent``) is actively harmful:
    when the unit is generated from a transient checkout — a ``.worktrees/``
    dir, or a clone that ``son-of-anton update`` later relocates/removes — the path
    rots. systemd then fails the start at the CHDIR step (``status=200/CHDIR``,
    "Changing to the requested working directory failed") *before* Python
    loads, so the on-boot ``refresh_systemd_unit_if_needed()`` self-heal never
    runs and ``Restart=always`` crash-loops forever on a dead directory.

    ``SON_OF_ANTON_HOME`` is the stable anchor: it is where config/state/logs live,
    it never moves, and it is guaranteed to exist whenever the gateway is
    meaningfully installed. Fall back to ``PROJECT_ROOT`` only if SON_OF_ANTON_HOME
    cannot be resolved (it always can in practice).
    """
    try:
        home = get_son_of_anton_home()
        if home and Path(home).is_dir():
            return str(Path(home).resolve())
    except Exception:
        pass
    return str(PROJECT_ROOT)


def _systemd_watchdog_seconds(son_of_anton_home: str | Path | None = None) -> int:
    """Resolve the managed-overlay-aware watchdog setting for a service home."""
    override_token = None
    reset_home_override = None
    if son_of_anton_home is not None:
        from son_of_anton_constants import (
            reset_son_of_anton_home_override,
            set_son_of_anton_home_override,
        )

        override_token = set_son_of_anton_home_override(son_of_anton_home)
        reset_home_override = reset_son_of_anton_home_override
    try:
        config = load_gateway_config()
        return coerce_systemd_watchdog_seconds(
            getattr(config, "systemd_watchdog_seconds", 0)
        )
    except Exception:
        logger.debug(
            "Could not resolve effective systemd watchdog configuration",
            exc_info=True,
        )
        return 0
    finally:
        if override_token is not None and reset_home_override is not None:
            reset_home_override(override_token)


def _systemd_watchdog_service_fields(
    son_of_anton_home: str | Path | None = None,
) -> tuple[str, str]:
    """Return systemd service fields for the effective gateway config."""
    seconds = _systemd_watchdog_seconds(son_of_anton_home)
    if seconds <= 0:
        return "simple", ""
    return "notify", f"NotifyAccess=main\nWatchdogSec={seconds}s\n"


def _append_node_dir_for_service(path_entries: list[str]) -> None:
    """Add the directory of the ``node`` on PATH to a generated unit's *path_entries*.

    Node comes from the system (Nix) like every other tool; nothing provisions
    a private copy at runtime.
    """
    resolved_node = shutil.which("node")
    if not resolved_node:
        return

    # Use the directory where ``node`` is *found on PATH*, NOT the symlink's
    # resolved target. ``~/.local/bin/node`` is often a symlink into a
    # specific install; calling .resolve() here would chase that symlink and
    # bake one install's node path into the unit, which makes
    # systemd_unit_is_current() perpetually false, so each gateway rewrites
    # its unit + daemon-reload on every boot.
    resolved_node_dir = str(Path(resolved_node).parent)
    if resolved_node_dir not in path_entries:
        path_entries.append(resolved_node_dir)


def generate_systemd_unit(system: bool = False, run_as_user: str | None = None) -> str:
    python_path = get_python_path()
    working_dir = _stable_service_working_dir()
    detected_venv = _detect_venv_dir()
    venv_dir = str(detected_venv) if detected_venv else str(PROJECT_ROOT / "venv")

    path_entries = _build_service_path_dirs()
    if not system:
        # System units add Node later, ahead of the target user's remapped
        # PATH entries.
        _append_node_dir_for_service(path_entries)

    common_bin_paths = [
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    # Preserve 30s for post-drain cleanup before systemd escalates, with a
    # 60s minimum for installs that use the default immediate drain. Positive
    # drain values extend the deadline directly instead of inheriting a second
    # 60s floor, so a configured 45s drain yields 75s rather than 90s.
    _drain_timeout = int(_get_restart_drain_timeout() or 0)
    restart_timeout = max(60, _drain_timeout + 30)

    if system:
        username, group_name, home_dir = _system_service_identity(run_as_user)
        son_of_anton_home = _son_of_anton_home_for_target_user(home_dir)
        systemd_type, systemd_watchdog_directives = _systemd_watchdog_service_fields(
            son_of_anton_home
        )
        profile_arg = _profile_arg_for_target_user(son_of_anton_home, home_dir)
        # Remap all paths that may resolve under the calling user's home
        # (e.g. /root/) to the target user's home so the service can
        # actually access them.
        python_path = _remap_path_for_user(python_path, home_dir)
        # Anchor cwd to the target user's SON_OF_ANTON_HOME (stable, always exists)
        # rather than a remapped source-checkout path that can rot. See
        # _stable_service_working_dir() for the full rationale.
        working_dir = str(son_of_anton_home) if son_of_anton_home else _remap_path_for_user(working_dir, home_dir)
        venv_dir = _remap_path_for_user(venv_dir, home_dir)
        path_entries = [_remap_path_for_user(p, home_dir) for p in path_entries]
        # Node first, outranking remapped shell-PATH entries, matching the
        # user-unit ordering where it's appended before PATH capture.
        _target_node_entries: list[str] = []
        _append_node_dir_for_service(_target_node_entries)
        path_entries = [
            e for e in _target_node_entries if e not in path_entries
        ] + path_entries
        path_entries.extend(_build_user_local_paths(Path(home_dir), path_entries))
        path_entries.extend(common_bin_paths)
        sane_path = ":".join(path_entries)
        return f"""[Unit]
Description={SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type={systemd_type}
{systemd_watchdog_directives}User={username}
Group={group_name}
ExecStart={python_path} -m son_of_anton_cli.main{f" {profile_arg}" if profile_arg else ""} gateway run
WorkingDirectory={working_dir}
Environment="HOME={home_dir}"
Environment="USER={username}"
Environment="LOGNAME={username}"
Environment="PATH={sane_path}"
Environment="VIRTUAL_ENV={venv_dir}"
Environment="SON_OF_ANTON_HOME={son_of_anton_home}"
Restart=always
RestartSec=5
RestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}
RestartPreventExitStatus={GATEWAY_FATAL_CONFIG_EXIT_CODE}
KillMode=mixed
KillSignal=SIGTERM
ExecReload=/bin/kill -USR1 $MAINPID
ExecStopPost=-{python_path} -m gateway.cgroup_cleanup
TimeoutStopSec={restart_timeout}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

    son_of_anton_home = str(get_son_of_anton_home().resolve())
    systemd_type, systemd_watchdog_directives = _systemd_watchdog_service_fields(
        son_of_anton_home
    )
    profile_arg = _profile_arg(son_of_anton_home)
    path_entries.extend(_build_user_local_paths(Path.home(), path_entries))
    path_entries.extend(common_bin_paths)
    sane_path = ":".join(path_entries)
    return f"""[Unit]
Description={SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type={systemd_type}
{systemd_watchdog_directives}ExecStart={python_path} -m son_of_anton_cli.main{f" {profile_arg}" if profile_arg else ""} gateway run
WorkingDirectory={working_dir}
Environment="PATH={sane_path}"
Environment="VIRTUAL_ENV={venv_dir}"
Environment="SON_OF_ANTON_HOME={son_of_anton_home}"
Restart=always
RestartSec=5
RestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}
RestartPreventExitStatus={GATEWAY_FATAL_CONFIG_EXIT_CODE}
KillMode=mixed
KillSignal=SIGTERM
ExecReload=/bin/kill -USR1 $MAINPID
ExecStopPost=-{python_path} -m gateway.cgroup_cleanup
TimeoutStopSec={restart_timeout}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def _normalize_service_definition(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


# Directives that older systemd versions silently ignore/strip.  Normalize
# them out of stale-check comparisons so a unit that differs only by these
# directives is not perpetually flagged as outdated.
_SYSTEMD_OPTIONAL_DIRECTIVES = (
    "RestartMaxDelaySec",
    "RestartSteps",
)


def _strip_optional_systemd_directives(text: str) -> str:
    """Remove systemd directives that older hosts silently drop."""
    lines = text.splitlines()
    filtered = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in _SYSTEMD_OPTIONAL_DIRECTIVES:
                continue
        filtered.append(line)
    return "\n".join(filtered)


def _normalize_launchd_plist_for_comparison(text: str) -> str:
    """Normalize launchd plist text for staleness checks.

    The generated plist intentionally captures a broad PATH assembled from the
    invoking shell so user-installed tools remain reachable under launchd.
    That makes raw text comparison unstable across shells, so ignore the PATH
    payload when deciding whether the installed plist is stale.
    """
    import re

    normalized = _normalize_service_definition(text)
    return re.sub(
        r"(<key>PATH</key>\s*<string>)(.*?)(</string>)",
        r"\1__SON_OF_ANTON_PATH__\3",
        normalized,
        flags=re.S,
    )


def systemd_unit_is_current(system: bool = False) -> bool:
    # ── SON_OF_ANTON_HOME sync chokepoint ──────────────────────────────────────
    # Every path that compares OR regenerates the unit funnels through here:
    # ``refresh_systemd_unit_if_needed`` gates on this before rewriting, and
    # ``systemd_status`` / ``systemd_install`` call it directly. Doing the
    # sync here — and ONLY here — enforces the invariant "the operator's
    # pinned SON_OF_ANTON_HOME is adopted before any compare/regenerate" at a single
    # site, so a future callsite cannot regress it by forgetting to pre-sync.
    #
    # Under ``sudo son-of-anton gateway … --system``, SON_OF_ANTON_HOME is often stripped
    # and falls back to ``/root/.son-of-anton``. Adopting the unit's pinned home
    # first makes TimeoutStopSec / WorkingDirectory / SON_OF_ANTON_HOME comparisons
    # use the real operator config — otherwise start/restart "refresh" rewrites
    # a correct unit from root's defaults and ``status`` keeps warning forever.
    # ``_sync_...`` is idempotent (early-returns once os.environ matches), so
    # the mutation persists for callers that read runtime state after this
    # (e.g. ``systemd_restart``'s post-refresh get_running_pid / drain-timeout).
    _sync_son_of_anton_home_from_systemd_unit(system=system)

    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists():
        return False

    installed = unit_path.read_text(encoding="utf-8")
    expected_user = _read_systemd_user_from_unit(unit_path) if system else None
    expected = generate_systemd_unit(system=system, run_as_user=expected_user)
    # Normalize out directives that older systemd versions silently drop
    # (RestartMaxDelaySec, RestartSteps) so a unit that differs only by
    # those directives is not perpetually flagged as outdated.
    norm_installed = _normalize_service_definition(
        _strip_optional_systemd_directives(installed)
    )
    norm_expected = _normalize_service_definition(
        _strip_optional_systemd_directives(expected)
    )
    return norm_installed == norm_expected


def _temp_home_in_service_definition(definition: str) -> str | None:
    """Return the temp-dir SON_OF_ANTON_HOME baked into a service definition, or None.

    A generated systemd unit / launchd plist carries the resolved SON_OF_ANTON_HOME
    in its environment block. If that path lives under the system temp dir,
    the definition was almost certainly generated by a test/E2E harness that
    exported a throwaway ``SON_OF_ANTON_HOME=/tmp/...`` — writing it to the real
    service file silently breaks the user's gateway on the next (re)start:
    the gateway comes back "active (running)" but pointed at an empty temp
    home ("No messaging platforms enabled"), deaf to every platform.
    Seen live 2026-06-11: an E2E guard probe ran ``son-of-anton gateway restart``
    with ``SON_OF_ANTON_HOME=/tmp/son-of-anton-e2e-<pr>`` exported; the restart path's
    unit refresh baked the temp path into the production unit and the
    post-update restart produced a zombie gateway for 7+ hours.

    Matches both systemd ``Environment="SON_OF_ANTON_HOME=..."`` lines and launchd
    ``<key>SON_OF_ANTON_HOME</key><string>...</string>`` pairs.
    """
    import re
    import tempfile

    candidates = re.findall(r'SON_OF_ANTON_HOME=([^"\n]+)', definition)
    candidates += re.findall(
        r"<key>SON_OF_ANTON_HOME</key>\s*<string>(.*?)</string>", definition, flags=re.S
    )
    temp_roots = {
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp"),
        Path("/var/tmp"),
        Path("/private/tmp"),
        Path("/private/var/tmp"),
    }
    for raw in candidates:
        try:
            resolved = Path(raw.strip().strip('"')).resolve()
        except (OSError, ValueError):
            continue
        for root in temp_roots:
            if resolved == root or root in resolved.parents:
                return raw.strip()
    return None


def _refuse_temp_home_service_write(definition: str, kind: str) -> bool:
    """Refuse (with guidance) when a service definition carries a temp SON_OF_ANTON_HOME."""
    temp_home = _temp_home_in_service_definition(definition)
    if temp_home is None:
        return False
    print(
        f"✗ Refusing to write the gateway {kind}: SON_OF_ANTON_HOME resolves to a "
        f"temporary directory ({temp_home})."
    )
    print(
        "  This usually means a test/E2E environment exported SON_OF_ANTON_HOME. "
        "Unset it (or run from a clean shell) and retry."
    )
    return True


def refresh_systemd_unit_if_needed(system: bool = False) -> bool:
    """Rewrite the installed systemd unit when the generated definition has changed."""
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists():
        return False

    # The gate below funnels through ``systemd_unit_is_current``, which is the
    # single SON_OF_ANTON_HOME-sync chokepoint (adopts the unit's pinned home before
    # any compare/regenerate). No separate pre-sync needed here — and the env
    # mutation it performs persists for the regenerate path below.
    if systemd_unit_is_current(system=system):
        return False

    expected_user = _read_systemd_user_from_unit(unit_path) if system else None
    new_unit = generate_systemd_unit(system=system, run_as_user=expected_user)

    # ── Test-environment safety belt ─────────────────────────────────────
    # The user-scope unit path resolves under ``Path.home()``, which is NOT
    # sandboxed by the test conftest (only SON_OF_ANTON_HOME is). If a test
    # exercises ``run_gateway()`` with a pytest-tmp SON_OF_ANTON_HOME, the freshly
    # generated unit bakes that ``/tmp/pytest-of-.../son_of_anton_test`` path into
    # ``Environment="SON_OF_ANTON_HOME=..."``. Writing that to the developer's
    # real user systemd unit file silently breaks their gateway on the next
    # reboot (systemd loads the polluted env, the gateway looks at an empty
    # tmp dir, and Discord/Slack/etc. all show as "not configured").
    # Refuse to write when the generated unit references a pytest tmpdir.
    # Detection sniffs the unit body — tests that legitimately exercise the
    # refresh flow patch ``generate_systemd_unit`` to return synthetic
    # content (``"new unit\n"``) which doesn't contain these markers and
    # still works.
    if not system and (
        "/pytest-of-" in new_unit
        or '/son_of_anton_test"' in new_unit
        or "/son_of_anton_test/" in new_unit
    ):
        return False

    # Structural variant of the same belt: refuse to bake ANY temp-dir
    # SON_OF_ANTON_HOME into the unit (manual E2E homes like /tmp/son-of-anton-e2e-NNN
    # don't carry the pytest markers above but poison the unit identically).
    if _refuse_temp_home_service_write(new_unit, "systemd unit"):
        return False

    unit_path.write_text(new_unit, encoding="utf-8")
    _run_systemctl(["daemon-reload"], system=system, check=True, timeout=30)
    print(
        f"↻ Updated gateway {_service_scope_label(system)} service definition to match the current Son of Anton install"
    )
    return True


def _select_systemd_scope(system: bool = False) -> bool:
    if system:
        return True
    return (
        get_systemd_unit_path(system=True).exists()
        and not get_systemd_unit_path(system=False).exists()
    )


def _get_restart_drain_timeout() -> float:
    """Return the configured gateway restart drain timeout in seconds."""
    raw = os.getenv("SON_OF_ANTON_RESTART_DRAIN_TIMEOUT", "").strip()
    if not raw:
        cfg = read_raw_config()
        agent_cfg = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
        raw = str(
            agent_cfg.get(
                "restart_drain_timeout", DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
            )
        )
    return parse_restart_drain_timeout(raw)


def systemd_status(deep: bool = False, system: bool = False, full: bool = False):
    system = _select_systemd_scope(system)
    unit_path = get_systemd_unit_path(system=system)
    scope_flag = " --system" if system else ""

    if not unit_path.exists():
        print("✗ Gateway service is not installed")
        print(f"  Run: {'sudo ' if system else ''}son-of-anton gateway install{scope_flag}")
        return

    if has_conflicting_systemd_units():
        print_systemd_scope_conflict_warning()
        print()

    if has_legacy_son_of_anton_units():
        print_legacy_unit_warning()
        print()

    if not systemd_unit_is_current(system=system):
        print("⚠ Installed gateway service definition is outdated")
        print(
            f"  Run: {'sudo ' if system else ''}son-of-anton gateway restart{scope_flag}  # auto-refreshes the unit"
        )
        print()

    status_cmd = ["status", get_service_name(), "--no-pager"]
    if full:
        status_cmd.append("-l")

    _run_systemctl(
        status_cmd,
        system=system,
        capture_output=False,
        timeout=10,
    )

    result = _run_systemctl(
        ["is-active", get_service_name()],
        system=system,
        capture_output=True,
        text=True, encoding='utf-8', errors='replace',
        timeout=10,
    )

    status = result.stdout.strip()

    if status == "active":
        print(
            f"✓ {_service_scope_label(system).capitalize()} gateway service is running"
        )
    else:
        print(
            f"✗ {_service_scope_label(system).capitalize()} gateway service is stopped"
        )
        print(f"  Run: {'sudo ' if system else ''}son-of-anton gateway start{scope_flag}")

    configured_user = _read_systemd_user_from_unit(unit_path) if system else None
    if configured_user:
        print(f"Configured to run as: {configured_user}")

    runtime_lines = _runtime_health_lines()
    if runtime_lines:
        print()
        print("Recent gateway health:")
        for line in runtime_lines:
            print(f"  {line}")

    unit_props = _read_systemd_unit_properties(system=system)
    active_state = unit_props.get("ActiveState", "")
    sub_state = unit_props.get("SubState", "")
    exec_main_status = unit_props.get("ExecMainStatus", "")
    result_code = unit_props.get("Result", "")
    if active_state == "activating" and sub_state == "auto-restart":
        print("  ⏳ Restart pending: systemd is waiting to relaunch the gateway")
    elif _systemd_unit_is_start_limited(unit_props):
        print("  ⏳ Restart pending: systemd is temporarily rate-limiting starts")
        print(
            f"  Run after the start-limit window expires: {'sudo ' if system else ''}son-of-anton gateway restart{scope_flag}"
        )
        print(
            f"  Or clear it manually: systemctl {'--user ' if not system else ''}reset-failed {get_service_name()}"
        )
    elif active_state == "failed" and exec_main_status == str(
        GATEWAY_SERVICE_RESTART_EXIT_CODE
    ):
        print("  ⚠ Planned restart is stuck in systemd failed state (exit 75)")
        print(
            f"  Run: systemctl {'--user ' if not system else ''}reset-failed {get_service_name()} && {'sudo ' if system else ''}son-of-anton gateway start{scope_flag}"
        )
    elif active_state == "failed" and result_code:
        print(f"  ⚠ Systemd unit result: {result_code}")

    if system:
        print("✓ System service starts at boot without requiring systemd linger")
    elif deep:
        print_systemd_linger_guidance()
    else:
        linger_enabled, _ = get_systemd_linger_status()
        if linger_enabled is True:
            print("✓ Systemd linger is enabled (service survives logout)")
        elif linger_enabled is False:
            print("⚠ Systemd linger is disabled (gateway may stop when you log out)")
            print("  Run: sudo loginctl enable-linger $USER")

    if deep:
        print()
        print("Recent logs:")
        log_cmd = _journalctl_cmd(system) + [
            "-u",
            get_service_name(),
            "-n",
            "20",
            "--no-pager",
        ]
        if full:
            log_cmd.append("-l")
        subprocess.run(log_cmd, timeout=10)


# =============================================================================
# Launchd (macOS)
# =============================================================================


def get_launchd_label() -> str:
    """Return the launchd service label, scoped per profile."""
    suffix = _profile_suffix()
    return f"ai.son-of-anton.gateway-{suffix}" if suffix else "ai.son-of-anton.gateway"


# ── launchd unsupported marker ─────────────────────────────────────────────
# When launchd can't manage the domain on this host (error 5/125, macOS 26+),
# we write a persistent marker so `launchd_status()` can explain that launchd
# supervision is unavailable regardless of whether a fallback process is
# currently running.  The marker is cleared when bootstrap/kickstart succeeds,
# so an OS update that fixes the underlying issue allows automatic recovery.


def _launchd_unsupported_marker_path() -> Path:
    return get_son_of_anton_home() / ".gateway-launchd-unsupported"


def _launchd_unsupported_marker_exists() -> bool:
    return _launchd_unsupported_marker_path().exists()


def _gateway_run_command() -> list[str]:
    """Build the `python -m son_of_anton_cli.main [--profile X] gateway run --replace` argv.

    Profile-aware: honors the active SON_OF_ANTON_HOME via `_profile_arg()` so the
    detached fallback launches into the same profile as the CLI invocation.
    """
    cmd = [get_python_path(), "-m", "son_of_anton_cli.main"]
    profile_arg = _profile_arg()
    if profile_arg:
        cmd.extend(profile_arg.split())
    cmd.extend(["gateway", "run", "--replace"])
    return cmd


def _timestamped_stderr_gateway_command(
    error_log: Path,
    *,
    external_supervisor: bool = False,
) -> list[str]:
    """Wrap gateway run so raw stderr lines are timestamped before file write.

    ``external_supervisor=True`` is for launchd ProgramArguments only: the
    inner ``gateway run`` must carry ``--external-supervisor`` so
    ``son-of-anton update`` sees the flag on the live grandchild argv and hands
    the process back to launchd instead of starting a detached watcher
    (#86893 / #87005). The detached nohup fallback stays unmarked.
    """
    inner = _gateway_run_command()
    if external_supervisor and "--external-supervisor" not in inner:
        inner = [*inner, "--external-supervisor"]
    return [
        get_python_path(),
        "-m",
        "son_of_anton_cli.stderr_timestamp",
        "--error-log",
        str(error_log),
        "--",
        *inner,
    ]


def generate_launchd_plist() -> str:
    # Stable cwd anchor — never the volatile source checkout. See
    # _stable_service_working_dir() for the rationale (same rot risk applies
    # to launchd's WorkingDirectory as to systemd's).
    working_dir = _stable_service_working_dir()
    son_of_anton_home = str(get_son_of_anton_home().resolve())
    log_dir = get_son_of_anton_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    label = get_launchd_label()
    # Build a sane PATH for the launchd plist.  launchd provides only a
    # minimal default (/usr/bin:/bin:/usr/sbin:/sbin) which misses Homebrew,
    # nvm, cargo, etc.  We prepend venv/bin and node_modules/.bin (matching
    # the systemd unit), then capture the user's full shell PATH so every
    # user-installed tool (node, ffmpeg, …) is reachable.
    detected_venv = _detect_venv_dir()
    venv_dir = str(detected_venv) if detected_venv else str(PROJECT_ROOT / "venv")
    # Resolve the directory containing the node binary (e.g. Homebrew, nvm)
    # so it's explicitly in PATH even if the user's shell PATH changes later.
    priority_dirs = _build_service_path_dirs()
    _append_node_dir_for_service(priority_dirs)
    sane_path = ":".join(
        dict.fromkeys(
            priority_dirs + [p for p in os.environ.get("PATH", "").split(":") if p]
        )
    )

    err_path = log_dir / "gateway.error.log"

    # Build ProgramArguments array, including --profile when using a named profile.
    # The stderr wrapper preserves launchd's restart semantics while adding
    # timestamps to raw stderr lines before they land in gateway.error.log.
    prog_args = [
        f"<string>{part}</string>"
        for part in _timestamped_stderr_gateway_command(
            err_path, external_supervisor=True
        )
    ]
    prog_args_xml = "\n        ".join(prog_args)

    # Persist the configured RLIMIT_NOFILE floor into the service definition
    # itself. launchd starts children with a soft limit of 256 by default;
    # without this block every plist rewrite (e.g. `son-of-anton gateway start`)
    # would silently strip a manually-added limit and reintroduce EMFILE
    # crashes under load. The in-process floor (resource_limits.py) still
    # applies as a second layer for non-launchd launches.
    nofile_block = ""
    try:
        from son_of_anton_cli.resource_limits import configured_nofile_soft_limit

        nofile_target = configured_nofile_soft_limit()
    except Exception:
        nofile_target = None
    if nofile_target:
        nofile_block = f"""
    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>{nofile_target}</integer>
    </dict>
"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        {prog_args_xml}
    </array>
    
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{sane_path}</string>
        <key>VIRTUAL_ENV</key>
        <string>{venv_dir}</string>
        <key>SON_OF_ANTON_HOME</key>
        <string>{son_of_anton_home}</string>
    </dict>

    <key>LimitLoadToSessionType</key>
    <array>
        <string>Aqua</string>
        <string>Background</string>
    </array>
    
    <key>RunAtLoad</key>
    <true/>
    
    <key>KeepAlive</key>
    <true/>

    <!-- ThrottleInterval raises launchd's default 10s minimum respawn interval
         to 30s so a crash-looping gateway can't hammer launchd into a rapid
         respawn storm; ExitTimeOut gives the gateway 25s of graceful-drain
         headroom before launchd escalates from SIGTERM to SIGKILL on stop. -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>ExitTimeOut</key>
    <integer>25</integer>
{nofile_block}
    <key>StandardOutPath</key>
    <string>{log_dir}/gateway.log</string>
    
    <key>StandardErrorPath</key>
    <string>{log_dir}/gateway.error.log</string>
</dict>
</plist>
"""


def launchd_plist_is_current() -> bool:
    """Check if the installed launchd plist matches the currently generated one."""
    plist_path = get_launchd_plist_path()
    if not plist_path.exists():
        return False

    installed = plist_path.read_text(encoding="utf-8")
    expected = generate_launchd_plist()
    return _normalize_launchd_plist_for_comparison(
        installed
    ) == _normalize_launchd_plist_for_comparison(expected)


def launchd_status(deep: bool = False):
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=10,
        )
        service_listed = result.returncode == 0
        list_output = result.stdout
    except subprocess.TimeoutExpired:
        service_listed = False
        list_output = ""

    # Determine whether launchd is actively supervising a process.
    # ``launchctl list`` returns exit 0 whenever the service definition is
    # registered — even when ``state = not running`` (macOS 26+ with an
    # unmanageable domain).  A PID in the output confirms a live process.
    launchd_pid = _parse_launchd_pid_from_list_output(list_output) if service_listed else None

    # Son of Anton PID tracking — may be a detached fallback process spawned when
    # launchd cannot manage the domain on this host.
    from gateway.status import get_running_pid
    fallback_pid = get_running_pid(cleanup_stale=False)

    # Avoid double-counting: when launchd IS supervising, fallback_pid and
    # launchd_pid point at the same process (the gateway writes both the
    # launchd PID and the Son of Anton PID file).
    if launchd_pid is not None and fallback_pid == launchd_pid:
        fallback_pid = None

    # Persistent marker written when launchd bootstrap/kickstart fails with
    # exit 5/125 on this host.  Lets us explain *why* launchd can't supervise
    # even when no fallback process is currently running.
    launchd_unsupported = _launchd_unsupported_marker_exists()

    # ── Report ──
    print(f"Launchd plist: {plist_path}")
    if launchd_plist_is_current():
        print("✓ Service definition matches the current Son of Anton install")
    else:
        print("⚠ Service definition is stale relative to the current Son of Anton install")
        print("  Run: son-of-anton gateway start")

    if service_listed:
        if launchd_pid is not None:
            print(f"✓ Gateway is supervised by launchd (PID {launchd_pid})")
            print("  Auto-start at login and auto-restart on crash are available.")
            if launchd_unsupported:
                print("  (launchd domain was previously unavailable but is now working)")
        elif launchd_unsupported:
            print("⚠ Gateway service is registered but launchd is not supervising it")
            print("  launchd cannot manage the gateway on this macOS version.")
            if fallback_pid:
                print(f"✓ Detached fallback process is running (PID {fallback_pid})")
                print("  Cron jobs will fire. Stop with: son-of-anton gateway stop")
            else:
                print("✗ No fallback process is running")
                print("  Run: son-of-anton gateway start")
            print("  ⚠ Auto-start at login and auto-restart on crash are NOT available.")
        else:
            print("✓ Gateway service is registered with launchd")
            print(list_output)
            if fallback_pid:
                print(f"  Detached gateway process is running (PID {fallback_pid})")
    else:
        print("✗ Gateway service is not loaded")
        print("  Service definition exists locally but launchd has not loaded it.")
        print("  Run: son-of-anton gateway start")
        if fallback_pid:
            print(f"  Note: a detached gateway process is running (PID {fallback_pid})")

    if deep:
        log_file = get_son_of_anton_home() / "logs" / "gateway.log"
        if log_file.exists():
            print()
            print("Recent logs:")
            subprocess.run(["tail", "-20", str(log_file)], timeout=10)


# =============================================================================
# Gateway Runner
# =============================================================================


def _running_under_gateway_supervisor() -> bool:
    """Return True when this process IS the gateway a service manager launched.

    The conflict guard below must never fire on the service's own startup, or
    it would wedge the unit into a respawn/refuse loop. Each supervisor exports
    a reliable marker into the child's environment:

      - systemd sets ``INVOCATION_ID`` for every unit it launches (the same
        marker ``gateway/run.py`` already uses to pick the restart path).
      - launchd sets ``XPC_SERVICE_NAME`` to the job label for jobs it spawns;
        interactive shells inherit the sentinel ``"0"`` instead.
      - the s6-overlay container longrun exports ``SON_OF_ANTON_S6_SUPERVISED_CHILD``.
      - wrapped services can opt in with ``--external-supervisor`` when their
        launcher strips the native systemd/launchd marker.
    """
    return is_gateway_supervisor_process()


def _guard_supervised_gateway_conflict(force: bool = False) -> None:
    """Refuse a foreground gateway when a service manager already supervises one.

    Running ``son-of-anton gateway run [--replace]`` (or the manual-restart fallback)
    from a shell on a systemd/launchd host spawns a second, long-lived
    dispatcher that escapes the service cgroup, survives
    ``systemctl restart``, and becomes a silent concurrent writer on the shared
    kanban DB — the documented root cause of multi-writer SQLite WAL corruption
    (issue #35240). Pass ``--force`` to start anyway.
    """
    if force or _running_under_gateway_supervisor():
        return
    try:
        snapshot = get_gateway_runtime_snapshot()
    except Exception:
        # Best-effort guard: a probe failure must never block a real startup.
        logger.debug("Supervised-gateway conflict probe failed", exc_info=True)
        return
    if not (snapshot.service_installed and snapshot.service_running):
        return

    print_error(
        f"A gateway is already running under {snapshot.manager} for this profile."
    )
    print(
        "  Starting another one from a shell leaves an orphan dispatcher that\n"
        "  escapes the service, survives restarts, and writes to the same kanban\n"
        "  DB concurrently — which can corrupt it. Restart the supervised gateway\n"
        "  instead:"
    )
    print()
    print("    son-of-anton gateway restart")
    print()
    print(
        "  Pass --force to start a foreground gateway anyway (not recommended\n"
        "  while the service is running)."
    )
    sys.exit(1)


def _guard_existing_gateway_process_conflict(replace: bool = False) -> None:
    """Refuse duplicate foreground startup before importing gateway.run.

    ``gateway.run`` performs the authoritative PID/lock check, but importing it
    is expensive: it pulls in model_tools/plugin discovery first. On small
    instances, a supervisor or dashboard loop repeatedly running bare
    ``son-of-anton gateway run`` can burn memory/CPU just to fail with "already
    running" after plugin discovery. This cheap PID-file preflight preserves the
    same user-facing contract while avoiding that startup work without scanning
    unrelated gateway processes from other SON_OF_ANTON_HOME roots.
    """
    if replace or _running_under_gateway_supervisor():
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
    print("  Use 'son-of-anton gateway restart' to replace it,")
    print("  or 'son-of-anton gateway stop' first.")
    print("  Or use 'son-of-anton gateway run --replace' to auto-replace.")
    sys.exit(1)


def run_gateway(verbose: int = 0, quiet: bool = False, replace: bool = False, force: bool = False):
    """Run the gateway in foreground.

    Args:
        verbose: Stderr log verbosity count added on top of default WARNING (0=WARNING, 1=INFO, 2+=DEBUG).
        quiet: Suppress all stderr log output.
        replace: If True, kill any existing gateway instance before starting.
                 This prevents systemd restart loops when the old process
                 hasn't fully exited yet.
        force: Skip the supervised-gateway conflict guard and start even when a
               systemd/launchd service is already supervising this profile.
    """
    _guard_supervised_gateway_conflict(force=force)
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

    # Refresh the systemd unit definition on every boot so that restart
    # settings (RestartSec, StartLimitIntervalSec, etc.) stay current even
    # when the process was respawned via exit-code-75 (stale-code or
    # /restart) rather than through `son-of-anton gateway restart` which already
    # calls refresh_systemd_unit_if_needed().  Without this, a code update
    # that ships new unit settings won't take effect until the next manual
    # `son-of-anton gateway start/restart` — leaving the gateway vulnerable to
    # the exact failure mode the new settings were meant to prevent.
    if supports_systemd_services():
        try:
            refresh_systemd_unit_if_needed(system=False)
        except Exception:
            pass  # best-effort; don't block gateway startup

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
            from son_of_anton_constants import get_son_of_anton_home as _ghh

            log_dir = _ghh() / "logs"
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

    # Portable, app-level respawn-storm circuit breaker. launchd/systemd have
    # their own throttles, but this backstop works on every platform (and covers
    # supervisors that lack a respawn floor). Configured via
    # ``gateway.respawn_storm`` in config.toml (``max_starts`` / ``window_seconds``);
    # the env vars ``SON_OF_ANTON_GATEWAY_MAX_STARTS`` /
    # ``SON_OF_ANTON_GATEWAY_START_WINDOW_S`` override for escape-hatch use.
    # Set max_starts <= 0 to disable. Best-effort: a bookkeeping failure must
    # never block startup.
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
        success = asyncio.run(start_gateway(replace=replace, verbosity=verbosity))
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


# =============================================================================
# Gateway Setup (Interactive Messaging Platform Configuration)
# =============================================================================

# Per-platform config: each entry defines the env vars, setup instructions,
# and prompts needed to configure a messaging platform.
_PLATFORMS = [
    # Discord and Slack are bundled platform plugins — their setup metadata
    # is discovered dynamically via _all_platforms() from the platform
    # registry entries registered by their adapter.py::register(). #41112.
    {
        "key": "signal",
        "label": "Signal",
        "emoji": "📡",
        "token_var": "SIGNAL_HTTP_URL",
    },
]


def _all_platforms() -> list[dict]:
    """Return the full list of platforms for setup menus.

    Combines the built-in ``_PLATFORMS`` with plugin platforms registered via
    ``platform_registry``. Plugins are discovered on first call so bundled
    platform plugins (which auto-load via ``kind: platform``) appear in
    ``son-of-anton setup gateway`` without needing the gateway to be running.
    Built-ins keep their dict shape; plugin entries are adapted to the same
    shape with ``_registry_entry`` holding the source.
    """
    # Populate the registry so plugin platforms are visible. Idempotent.
    # Bundled platform plugins (``kind: platform``) auto-load unconditionally,
    # so every shipped messaging channel appears in the setup menu by default.
    # User-installed platform plugins under ~/.son-of-anton/plugins/ still require
    # opt-in via ``plugins.enabled`` (untrusted code).
    try:
        from son_of_anton_cli.plugins import discover_plugins

        discover_plugins()
    except Exception as e:
        logger.debug("plugin discovery failed during platform enumeration: %s", e)

    platforms = [dict(p) for p in _PLATFORMS]

    by_key = {p["key"]: p for p in platforms}

    try:
        from gateway.platform_registry import platform_registry
    except Exception:
        return platforms

    for entry in platform_registry.all_entries():
        if entry.name in by_key:
            continue  # built-in already covers it
        platforms.append(
            {
                "key": entry.name,
                "label": entry.label,
                "emoji": entry.emoji,
                "token_var": entry.required_env[0] if entry.required_env else "",
                "install_hint": entry.install_hint,
                "_registry_entry": entry,
            }
        )
    return platforms


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
    """Handle gateway subcommands (run, status).

    The service itself is declared by the NixOS / Home Manager module, so
    there is nothing to install, start or stop from here.
    """
    return _gateway_command_inner(args)


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


def _gateway_command_inner(args):
    subcmd = getattr(args, "gateway_command", None)

    # Default to run if no subcommand
    if subcmd is None or subcmd == "run":
        if getattr(args, "external_supervisor", False):
            os.environ[EXTERNAL_GATEWAY_SUPERVISOR_ENV] = "1"
        verbose = getattr(args, "verbose", 0)
        quiet = getattr(args, "quiet", False)
        replace = getattr(args, "replace", False)
        force = getattr(args, "force", False)
        run_gateway(verbose, quiet=quiet, replace=replace, force=force)
        return

    if subcmd == "status":
        deep = getattr(args, "deep", False)
        full = getattr(args, "full", False)
        system = getattr(args, "system", False)
        snapshot = get_gateway_runtime_snapshot(system=system)

        # Check for service first
        if supports_systemd_services() and (
            get_systemd_unit_path(system=False).exists()
            or get_systemd_unit_path(system=True).exists()
        ):
            systemd_status(deep, system=system, full=full)
            _print_gateway_process_mismatch(snapshot)
        elif is_macos() and get_launchd_plist_path().exists():
            launchd_status(deep)
            _print_gateway_process_mismatch(snapshot)
        else:
            # Check for manually running processes
            pids = list(snapshot.gateway_pids)
            if pids:
                units = systemd_units_of_pids(pids)
                print(f"✓ Gateway is running (PID: {', '.join(map(str, pids))})")
                if units:
                    print(f"  Supervised by systemd: {', '.join(units)}")
                else:
                    print("  (Running manually, not as a system service)")
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print("Recent gateway health:")
                    for line in runtime_lines:
                        print(f"  {line}")
                if not units:
                    print()
                    print("  Run it as a service by enabling it in the NixOS / Home Manager module.")
            else:
                print("✗ Gateway is not running")
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print("Recent gateway health:")
                    for line in runtime_lines:
                        print(f"  {line}")
                print()
                print("To start:")
                print("  son-of-anton gateway run      # Run in foreground")
                print("  or enable the service in the NixOS / Home Manager module")
