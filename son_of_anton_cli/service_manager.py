"""Abstract service manager interface.

Wraps the existing systemd (Linux host), launchd (macOS host), Windows
Scheduled Task (native Windows host), and s6 (container) backends behind
a common Protocol. Only the s6 backend supports runtime registration
(for per-profile gateways) — host backends raise NotImplementedError
from those methods, and callers MUST check supports_runtime_registration()
before invoking them.

Host-side call sites (setup wizard, uninstall, status) continue to use
the existing module-level functions in son_of_anton_cli.gateway and
son_of_anton_cli.gateway_windows directly. This protocol is a thin facade
used by new code that needs to be backend-agnostic — specifically the
profile create/delete hooks (Phase 4) and the s6 dispatch path in
``son-of-anton gateway start/stop/restart`` when running inside a container.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

ServiceManagerKind = Literal["systemd", "launchd", "none"]

# Profile name → service directory mapping. Profile names must be safe
# as filesystem directory names because the s6 backend creates a service
# directory at ``<scandir>/gateway-<profile>/``. We reject anything that
# could traverse paths, span filesystems, or break s6's own naming rules.
_VALID_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MAX_PROFILE_LEN = 251  # s6-svscan default name_max


def validate_profile_name(name: str) -> None:
    """Raise ValueError if ``name`` is not usable as a profile name.

    Profile names are used as s6 service directory names, so they must
    match a conservative subset of filesystem-safe characters. Reject
    empty strings, uppercase, paths-traversal sequences, and anything
    longer than s6's default ``name_max``.
    """
    if not name:
        raise ValueError("profile name must not be empty")
    if len(name) > _MAX_PROFILE_LEN:
        raise ValueError(
            f"profile name too long ({len(name)} > {_MAX_PROFILE_LEN})"
        )
    if not _VALID_PROFILE_RE.match(name):
        raise ValueError(
            f"profile name must match [a-z0-9][a-z0-9_-]*, got {name!r}"
        )


@runtime_checkable
class ServiceManager(Protocol):
    """Abstract interface for init-system-specific service operations.

    Lifecycle methods (start / stop / restart / is_running) are
    implemented by every backend. Runtime registration
    (register_profile_gateway / unregister_profile_gateway /
    list_profile_gateways) is implemented only by the s6 backend —
    callers MUST check ``supports_runtime_registration()`` before
    invoking the registration methods.
    """

    kind: ServiceManagerKind

    # Lifecycle of a pre-declared service.
    def start(self, name: str) -> None: ...
    def stop(self, name: str) -> None: ...
    def restart(self, name: str) -> None: ...
    def is_running(self, name: str) -> bool: ...

    # Runtime registration (s6 only).
    def supports_runtime_registration(self) -> bool: ...
    def register_profile_gateway(
        self,
        profile: str,
        *,
        extra_env: dict[str, str] | None = None,
        start_now: bool = True,
    ) -> None: ...
    def unregister_profile_gateway(self, profile: str) -> None: ...
    def list_profile_gateways(self) -> list[str]: ...


def detect_service_manager() -> ServiceManagerKind:
    """Detect which service manager is available in this environment.

    Returns:
        "launchd" — macOS host
        "systemd" — Linux host with a working user/system bus
        "none" — anything else (sandbox shells, etc.)

    This function does NOT replace ``supports_systemd_services()`` —
    host call sites continue to use that.
    """
    # Imports deferred so importing this module doesn't drag in the
    # whole gateway dependency graph for callers that only need the
    # Protocol type or validate_profile_name().
    from son_of_anton_cli.gateway import (
        is_macos,
        supports_systemd_services,
    )

    if is_macos():
        return "launchd"
    if supports_systemd_services():
        return "systemd"
    return "none"



class _RegistrationUnsupportedMixin:
    """Mixin for host backends that don't support runtime registration."""

    def supports_runtime_registration(self) -> bool:
        return False

    def register_profile_gateway(
        self,
        profile: str,
        *,
        extra_env: dict[str, str] | None = None,
        start_now: bool = True,
    ) -> None:
        raise NotImplementedError(
            f"{type(self).__name__} does not support runtime profile "
            "gateway registration (container-only feature)"
        )

    def unregister_profile_gateway(self, profile: str) -> None:
        raise NotImplementedError(
            f"{type(self).__name__} does not support runtime profile "
            "gateway unregistration (container-only feature)"
        )

    def list_profile_gateways(self) -> list[str]:
        return []


class SystemdServiceManager(_RegistrationUnsupportedMixin):
    """Thin wrapper around the ``systemd_*`` functions in son_of_anton_cli.gateway.

    Existing host call sites continue to use those functions directly;
    this wrapper exists for new code that needs to be backend-agnostic
    (the Phase 4 profile create/delete hooks).
    """

    kind: ServiceManagerKind = "systemd"

    def start(self, name: str) -> None:
        from son_of_anton_cli.gateway import systemd_start
        systemd_start()

    def stop(self, name: str) -> None:
        from son_of_anton_cli.gateway import systemd_stop
        systemd_stop()

    def restart(self, name: str) -> None:
        from son_of_anton_cli.gateway import systemd_restart
        systemd_restart()

    def is_running(self, name: str) -> bool:
        from son_of_anton_cli.gateway import _probe_systemd_service_running
        _, running = _probe_systemd_service_running()
        return running


class LaunchdServiceManager(_RegistrationUnsupportedMixin):
    """Thin wrapper around the ``launchd_*`` functions in son_of_anton_cli.gateway."""

    kind: ServiceManagerKind = "launchd"

    def start(self, name: str) -> None:
        from son_of_anton_cli.gateway import launchd_start
        launchd_start()

    def stop(self, name: str) -> None:
        from son_of_anton_cli.gateway import launchd_stop
        launchd_stop()

    def restart(self, name: str) -> None:
        from son_of_anton_cli.gateway import launchd_restart
        launchd_restart()

    def is_running(self, name: str) -> bool:
        from son_of_anton_cli.gateway import _probe_launchd_service_running
        return _probe_launchd_service_running()



def get_service_manager() -> ServiceManager:
    """Return the ServiceManager instance for the current environment.

    Raises:
        RuntimeError: when no supported backend is available.
    """
    kind = detect_service_manager()
    if kind == "systemd":
        return SystemdServiceManager()
    if kind == "launchd":
        return LaunchdServiceManager()
    raise RuntimeError("no supported service manager detected")


# ---------------------------------------------------------------------------
# S6ServiceManager (container-only)
#
# Per-profile gateways are registered dynamically when `son-of-anton profile create`
# runs inside the container (Phase 4). Static services (main-son-of-anton, dashboard)
# live in /etc/s6-overlay/s6-rc.d/ and are NOT managed by this class — they're
# part of the image, not runtime-created.
# ---------------------------------------------------------------------------


# s6-overlay's dynamic scandir for runtime-registered services. Lives on
# tmpfs and is the directory s6-svscan watches. Writes here trigger
# automatic supervision on the next rescan.
S6_DYNAMIC_SCANDIR = Path("/run/service")
S6_SERVICE_PREFIX = "gateway-"


def _profile_dir_for_gateway_service(name: str) -> Path:
    """Resolve ``gateway-<profile>`` to its persistent profile directory.

    s6 lifecycle commands may be invoked from any active profile, including
    ``gateway stop --all``. Do not write the caller's SON_OF_ANTON_HOME blindly;
    derive the shared profile root from the current SON_OF_ANTON_HOME and map the
    service suffix to either the root default profile or
    ``<root>/profiles/<profile>``.
    """
    import os

    profile = name[len(S6_SERVICE_PREFIX):] if name.startswith(S6_SERVICE_PREFIX) else name
    validate_profile_name(profile)
    son_of_anton_home = Path(os.environ.get("SON_OF_ANTON_HOME", "/opt/data"))
    if son_of_anton_home.parent.name == "profiles":
        root = son_of_anton_home.parent.parent
    else:
        root = son_of_anton_home
    return root if profile == "default" else root / "profiles" / profile


def _write_gateway_desired_state(name: str, desired_state: str) -> None:
    """Persist durable s6 gateway intent next to runtime status.

    ``gateway_state`` remains the volatile runtime field written by the
    gateway process. ``desired_state`` records the operator's start/stop
    intent so container-boot reconciliation can restore the correct s6
    want-up/want-down state after pod recreation even if the previous runtime
    state was transient (draining, startup_failed, etc.). The write is
    best-effort: a failed persistence attempt must not prevent immediate s6
    lifecycle control.
    """
    import json
    import time

    profile_dir = _profile_dir_for_gateway_service(name)
    state_file = profile_dir / "gateway_state.json"
    try:
        if not profile_dir.exists():
            return
        try:
            data = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
        data["desired_state"] = desired_state
        data["updated_at"] = int(time.time())
        tmp = state_file.with_suffix(state_file.suffix + ".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
        tmp.replace(state_file)
    except OSError:
        return


# s6-overlay installs its binaries under /command/ and only adds that
# directory to PATH for processes started under the supervision tree
# (services started by s6-svscan, cont-init.d scripts, etc.). Code
# that runs via `docker exec` or any other out-of-tree entry point —
# notably our Phase 4 profile create/delete hooks — inherits the
# container's base PATH which does NOT include /command/.
#
# Rather than asking every caller to fix up its environment, the
# S6ServiceManager calls s6-* binaries by absolute path via this
# constant. We don't use `/usr/bin/s6-…` symlinks because the
# s6-overlay-symlinks-noarch tarball only links a subset, and we
# want every s6 invocation to be guaranteed-findable.
_S6_BIN_DIR = "/command"


# UID/GID of the in-image ``son-of-anton`` user. Hardcoded to match what
# ``stage2-hook.sh`` enforces (the runtime invariant — see also
# tests/docker/test_uid_remap.py). The container starts s6-supervise
# under root and immediately drops to this UID via ``s6-setuidgid``.
_SON_OF_ANTON_UID = 10000
_SON_OF_ANTON_GID = 10000


def _seed_supervise_skeleton(svc_dir: Path) -> None:
    """Pre-create the ``supervise/`` and top-level ``event/`` skeleton
    inside a service directory, owned by the son-of-anton user.

    Why this exists
    ---------------
    When s6-supervise spawns a service it tries to ``mkdir`` two
    directories: ``<svc>/event`` and ``<svc>/supervise``, both with mode
    ``0700``. It also ``mkfifo``s ``<svc>/supervise/control`` with mode
    ``0600``. Because s6-supervise runs as PID 1's effective UID (root)
    these dirs end up root-owned mode 0700, and an unprivileged client
    (the ``son-of-anton`` user — UID 10000 — running every Son of Anton runtime
    operation via ``s6-setuidgid``) gets ``EACCES`` on any ``s6-svc``,
    ``s6-svstat``, or ``s6-svwait`` invocation against the slot.

    The PR #30136 review surfaced this as a real product gap: the
    entire S6ServiceManager lifecycle (``register/start/stop/unregister
    _profile_gateway``) was inert in production because every operation
    is dispatched as the son-of-anton user.

    Why this works
    --------------
    Reading s6's source (src/supervision/s6-supervise.c::trymkdir +
    control_init): the ``mkdir`` and ``mkfifo`` calls both treat
    ``EEXIST`` as success. If the directory is already present, the
    chown/chmod fix-up that would normally make event/ ``03730
    root:root`` is **skipped** entirely — s6-supervise just opens the
    pre-existing FIFOs and proceeds. So if we lay the skeleton down
    with son-of-anton ownership before triggering ``s6-svscanctl -a``,
    s6-supervise inherits our layout and never touches it.

    Layout produced
    ---------------
    ``svc_dir/``                           son-of-anton:son-of-anton, 0755 (parent must already exist)
    ``svc_dir/event/``                     son-of-anton:son-of-anton, 03730   (setgid + g+rwx + sticky)
    ``svc_dir/supervise/``                 son-of-anton:son-of-anton, 0755
    ``svc_dir/supervise/event/``           son-of-anton:son-of-anton, 03730
    ``svc_dir/supervise/control``          son-of-anton:son-of-anton, 0660    (FIFO)

    The ``death_tally``, ``lock``, and ``status`` regular files end up
    written by s6-supervise itself (as root), but those land mode 0644 —
    world-readable — and ``s6-svstat`` only needs read access, so the
    son-of-anton user reads them fine.

    If ``svc_dir/log/`` is present (the canonical s6 logger pattern —
    one s6-supervise instance per service, plus a second for its
    logger), the same skeleton is seeded under ``log/`` as well:
    ``log/event/``, ``log/supervise/``, ``log/supervise/event/``,
    ``log/supervise/control``. Without this, unregister teardown
    would EACCES on the logger's supervise dir even after the parent
    slot's supervise/ was son-of-anton-owned.

    Idempotency
    -----------
    Safe to call against a directory where the skeleton already exists.
    Existing entries are left untouched (the helper doesn't try to
    re-chown / re-chmod live FIFOs that s6-supervise may have already
    opened).

    Reference
    ---------
    Discussed at length on the skarnet `skaware` mailing list in 2020
    (`<http://skarnet.org/lists/skaware/1424.html>`_); see also
    just-containers/s6-overlay#130. The pre-creation pattern was
    historically called out as forward-compatibility-fragile, but the
    EEXIST handling in s6-supervise has been stable since 2015 — it's
    the same pattern ``s6-svperms`` and ``fix-attrs.d`` rely on.
    """
    import os

    def _mkdir_owned(path: Path, mode: int) -> None:
        if path.exists():
            return
        path.mkdir(parents=False, exist_ok=False)
        path.chmod(mode)
        try:
            os.chown(path, _SON_OF_ANTON_UID, _SON_OF_ANTON_GID)
        except PermissionError:
            # Running as the son-of-anton user already — directory is son-of-anton-
            # owned by default. The chown is a no-op in that case, so
            # swallowing this keeps both root and unprivileged callers
            # on one code path.
            pass

    # Top-level event/ dir (this is the s6-svlisten1 event-subscription
    # dir at the service root, distinct from supervise/event/).
    _mkdir_owned(svc_dir / "event", 0o3730)

    # supervise/ dir + its inner event/ dir.
    supervise = svc_dir / "supervise"
    _mkdir_owned(supervise, 0o755)
    _mkdir_owned(supervise / "event", 0o3730)

    # supervise/control FIFO. Same EEXIST-safe pattern: if it's already
    # there (s6-supervise has already started against this slot), leave
    # it alone. The explicit chmod after mkfifo is required because
    # mkfifo honors the process umask, which can strip group-write
    # (e.g. the default 0022 on most dev hosts → 0o660 becomes 0o640).
    # The container runs with umask 0 inside s6-overlay's stage2, but
    # being defensive here keeps the helper consistent under any
    # invocation context.
    control = supervise / "control"
    if not control.exists():
        os.mkfifo(control, 0o660)
        control.chmod(0o660)
        try:
            os.chown(control, _SON_OF_ANTON_UID, _SON_OF_ANTON_GID)
        except PermissionError:
            pass

    # If a log/ subdir is present (the canonical s6 logger pattern —
    # see servicedir(7)), it gets its own s6-supervise instance and
    # needs the same skeleton. Without this, unregister teardown
    # would EACCES on the logger's root-owned supervise/ dir even
    # when the parent slot's supervise/ is son-of-anton-owned.
    log_dir = svc_dir / "log"
    if log_dir.is_dir():
        _mkdir_owned(log_dir / "event", 0o3730)
        log_supervise = log_dir / "supervise"
        _mkdir_owned(log_supervise, 0o755)
        _mkdir_owned(log_supervise / "event", 0o3730)
        log_control = log_supervise / "control"
        if not log_control.exists():
            os.mkfifo(log_control, 0o660)
            log_control.chmod(0o660)
            try:
                os.chown(log_control, _SON_OF_ANTON_UID, _SON_OF_ANTON_GID)
            except PermissionError:
                pass


class S6Error(RuntimeError):
    """Base error for S6ServiceManager lifecycle failures.

    Concrete subclasses carry the slot name (and, where useful, the
    underlying subprocess output) so the CLI can render an actionable
    message instead of leaking a raw ``CalledProcessError`` traceback.
    """

    def __init__(self, message: str, *, service: str | None = None) -> None:
        super().__init__(message)
        self.service = service


class GatewayNotRegisteredError(S6Error):
    """Raised when a lifecycle method targets a slot that doesn't exist.

    Most commonly: ``son-of-anton -p typo gateway start`` when no profile
    ``typo`` exists. Carries the unprefixed profile name (not the
    full ``gateway-<profile>`` service-dir name) so callers can phrase
    a user-facing message like "no such gateway 'typo'".
    """

    def __init__(self, profile: str) -> None:
        self.profile = profile
        super().__init__(
            f"no such gateway {profile!r}: register it with "
            f"`son-of-anton profile create {profile}` first, or pass "
            "an existing profile name via `-p <name>`",
            service=f"gateway-{profile}",
        )


class S6CommandError(S6Error):
    """Raised when an s6 command fails for a reason other than a
    missing slot — e.g. permission denied on the supervise control
    FIFO, or s6-svc returning a non-zero exit for an unexpected
    reason. Carries the stderr from the failing command so callers
    can surface it.
    """

    def __init__(
        self, *, service: str, action: str, returncode: int, stderr: str,
    ) -> None:
        self.action = action
        self.returncode = returncode
        self.stderr = stderr
        message = (
            f"s6-svc {action} on {service!r} failed (rc={returncode})"
        )
        if stderr.strip():
            message += f": {stderr.strip()}"
        super().__init__(message, service=service)


