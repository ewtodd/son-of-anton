"""
Gateway runtime status helpers.

PID-file and runtime-lock detection of whether the gateway is running, the
persisted runtime-status file, and the machine-local scoped locks that keep
two gateways from listening on one external identity.

The PID file lives at ``{SON_OF_ANTON_HOME}/gateway.pid``, so each instance's
SON_OF_ANTON_HOME gets its own. Scoped locks live in
``SON_OF_ANTON_GATEWAY_LOCK_DIR``, which the NixOS module pins per instance.
"""

import fcntl
import hashlib
import json
import logging
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from son_of_anton_constants import get_son_of_anton_home, _get_platform_default_son_of_anton_home
from typing import Any, NamedTuple, Optional
from utils import atomic_json_write

_GATEWAY_KIND = "son-of-anton-gateway"
_RUNTIME_STATUS_FILE = "gateway_state.json"
_LOCKS_DIRNAME = "gateway-locks"
_UNSET = object()
_GATEWAY_LOCK_FILENAME = "gateway.lock"
_gateway_lock_handle = None

logger = logging.getLogger(__name__)


class StormInfo(NamedTuple):
    """Result of a respawn-storm check: how many starts, over what window, and
    the backoff the caller should sleep to break the storm."""

    count: int
    window_s: float
    backoff_s: float


def _get_starts_log_path() -> Path:
    """Path to the append-only gateway-start ledger used by the respawn-storm
    breaker. Distinct from ``restart_loop.json`` (the auto-resume guard) — no
    collision."""
    return get_son_of_anton_home() / "gateway-starts.log"


def record_start_and_check_storm(
    max_starts: int = 5, window_s: float = 120.0, *, backoff_cap_s: float = 300.0
) -> Optional[StormInfo]:
    """Record this gateway start and report whether a respawn storm is underway.

    Appends the current UTC timestamp to the starts-log, prunes entries older
    than ``window_s``, and ring-buffers the file so it can't grow unbounded.
    Returns a :class:`StormInfo` when more than ``max_starts`` starts landed in
    the window (with an exponential backoff capped at ``backoff_cap_s``), else
    ``None``.

    Best-effort: any bookkeeping failure is logged and swallowed so a broken
    ledger can never crash gateway startup.
    """
    try:
        path = _get_starts_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc).timestamp()

        existing: list[float] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing.append(float(line))
                except ValueError:
                    continue

        existing.append(now)

        # Keep only starts within the sliding window for the storm decision.
        recent = [ts for ts in existing if now - ts <= window_s]

        # Ring-buffer what we persist so the file stays bounded even if the
        # window is wide or starts are frequent.
        keep = max(max_starts * 4, 40)
        to_write = existing[-keep:]

        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            "\n".join(repr(ts) for ts in to_write) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)

        if len(recent) > max_starts:
            backoff = min(
                backoff_cap_s, 5.0 * (2 ** min(len(recent) - max_starts, 6))
            )
            return StormInfo(count=len(recent), window_s=window_s, backoff_s=backoff)
        return None
    except Exception as _e:
        logger.debug(
            "respawn-storm breaker bookkeeping failed (non-fatal): %s", _e
        )
        return None


def _get_process_son_of_anton_home() -> Path:
    """Return the process-level SON_OF_ANTON_HOME, skipping context-local overrides.

    Gateway identity files (PID, lock, runtime status) must always live in the
    directory the gateway process was launched with, not in whatever home a
    context-local override happens to name at write time.
    """
    val = os.environ.get("SON_OF_ANTON_HOME", "").strip()
    if val:
        return Path(val)
    return _get_platform_default_son_of_anton_home()


def _canonical_son_of_anton_home(path: Path | str) -> Path:
    """Return a stable absolute SON_OF_ANTON_HOME path for persisted identity data."""
    return Path(path).expanduser().resolve(strict=False)


def _profile_label_for_home(home: Path | str) -> Optional[str]:
    """Best-effort owner label for a SON_OF_ANTON_HOME path.

    Scoped credential locks are machine-global, so a lock the caller did not
    take may be held by a gateway serving a DIFFERENT home — under the
    per-account services that is the common case, several gateways sharing one
    Signal account. The home path is what distinguishes them, so it is the
    label: ``"default"`` for this deployment's root home, otherwise the
    canonical path. Never raises — this feeds diagnostics only.
    """
    try:
        canonical = _canonical_son_of_anton_home(home)
    except Exception:
        return None
    try:
        from son_of_anton_constants import get_default_son_of_anton_root

        if canonical == _canonical_son_of_anton_home(get_default_son_of_anton_root()):
            return "default"
    except Exception:
        pass
    try:
        if canonical == _canonical_son_of_anton_home(_get_platform_default_son_of_anton_home()):
            return "default"
    except Exception:
        pass
    return str(canonical)


def scoped_lock_owner_label(record: Optional[dict[str, Any]]) -> Optional[str]:
    """Label for the gateway that owns a scoped credential lock.

    Scoped locks are machine-global, so the holder is often a gateway serving a
    DIFFERENT SON_OF_ANTON_HOME — with one service per account that is the
    normal case.  Prefers the explicit ``profile`` field stamped by
    :func:`acquire_scoped_lock`; falls back to the persisted
    ``son_of_anton_home``.  Returns ``None`` for legacy or malformed records so
    callers keep their PID-only wording.
    """
    if not isinstance(record, dict):
        return None
    profile = record.get("profile")
    if isinstance(profile, str) and profile.strip():
        # Lock files are plain JSON on disk and this string flows into log
        # lines, so keep it to one line of printable text.
        label = profile.strip().splitlines()[0]
        return label[:200] or None
    home = record.get("son_of_anton_home")
    if isinstance(home, str) and home.strip():
        return _profile_label_for_home(home)
    return None


def _get_pid_path() -> Path:
    """Return the path to the gateway PID file, respecting SON_OF_ANTON_HOME."""
    home = _get_process_son_of_anton_home()
    return home / "gateway.pid"


def _get_gateway_lock_path(pid_path: Optional[Path] = None) -> Path:
    """Return the path to the runtime gateway lock file."""
    if pid_path is not None:
        return pid_path.with_name(_GATEWAY_LOCK_FILENAME)
    home = _get_process_son_of_anton_home()
    return home / _GATEWAY_LOCK_FILENAME


def _get_runtime_status_path() -> Path:
    """Return the persisted runtime health/status file path."""
    return _get_pid_path().with_name(_RUNTIME_STATUS_FILE)


def _get_lock_dir() -> Path:
    """Return the machine-local directory for token-scoped gateway locks."""
    override = os.getenv("SON_OF_ANTON_GATEWAY_LOCK_DIR")
    if override:
        return Path(override)
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "son-of-anton" / _LOCKS_DIRNAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _get_scope_lock_path(scope: str, identity: str) -> Path:
    return _get_lock_dir() / f"{scope}-{_scope_hash(identity)}.lock"


def _get_process_start_time(pid: int) -> Optional[int]:
    """Return a stable per-process start-time fingerprint, or None.

    Used as a PID-reuse guard: a ``(pid, start_time)`` pair uniquely identifies
    a process, so a recycled PID (same number, different process) yields a
    different value and is never mistaken for the original. This is field 22
    of ``/proc/<pid>/stat`` (start time in clock ticks since boot).
    """
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        return int(stat_path.read_text(encoding="utf-8").split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        return None


def get_process_start_time(pid: int) -> Optional[int]:
    """Public wrapper for retrieving a process start time when available."""
    return _get_process_start_time(pid)


def _read_process_cmdline(pid: int) -> Optional[str]:
    """Return ``/proc/<pid>/cmdline`` as a space-separated string, or None."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not raw:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()


def looks_like_gateway_command_line(command: str | None) -> bool:
    """Return True only for a real ``gateway run`` process command line.

    Lifecycle decisions (is the gateway up?) must not fire on loose substring
    matches: ``... gateway`` also matches ``gateway status`` and unrelated
    processes that merely contain the word. This requires the ``son-of-anton``
    entrypoint followed by the ``gateway`` subcommand and then ``run`` or
    nothing (a bare ``son-of-anton gateway`` runs it). Tokenizes quote-aware
    (``shlex``) so paths with spaces survive.
    """
    if not command:
        return False

    try:
        raw_tokens = shlex.split(command, posix=False)
    except ValueError:
        raw_tokens = command.split()
    tokens = [t.strip("\"'").lower() for t in raw_tokens]
    if not tokens:
        return False

    joined = " ".join(tokens)
    has_gateway_entry = (
        "son_of_anton_cli.main" in joined
        or "son_of_anton_cli/main.py" in joined
        or any(t.rsplit("/", 1)[-1] == "son-of-anton" for t in tokens)
    )
    if not has_gateway_entry:
        return False

    for i, token in enumerate(tokens):
        if token != "gateway":
            continue
        return i + 1 >= len(tokens) or tokens[i + 1] == "run"
    return False


def _looks_like_gateway_process(pid: int) -> bool:
    """Return True when the live PID still looks like the Son of Anton gateway."""
    cmdline = _read_process_cmdline(pid)
    if not cmdline:
        return False
    return looks_like_gateway_command_line(cmdline)


def _record_looks_like_gateway(record: dict[str, Any]) -> bool:
    """Validate gateway identity from PID-file metadata when cmdline is unavailable."""
    if record.get("kind") != _GATEWAY_KIND:
        return False

    argv = record.get("argv")
    if not isinstance(argv, list) or not argv:
        return False

    cmdline = " ".join(str(part) for part in argv)
    return looks_like_gateway_command_line(cmdline)


def _record_matches_live_gateway_pid(record: dict[str, Any], pid: int) -> bool:
    """Return True when a live PID still identifies as this gateway record.

    Prefer the live command line whenever it is readable. Runtime status files
    can outlive the gateway process they describe; if PID reuse leaves the same
    PID occupied by an unrelated process, the stale record's argv must not make
    it count as a running gateway. When the live command line cannot be read
    (permission), fall back to the persisted record.
    """
    live_cmdline = _read_process_cmdline(pid)
    if live_cmdline:
        return looks_like_gateway_command_line(live_cmdline)
    return _record_looks_like_gateway(record)


def _build_pid_record() -> dict:
    return {
        "pid": os.getpid(),
        "kind": _GATEWAY_KIND,
        "argv": list(sys.argv),
        "start_time": _get_process_start_time(os.getpid()),
        # Scoped credential locks are machine-global rather than
        # SON_OF_ANTON_HOME-local.  Persist the owning gateway's process home so
        # a conflict message can name which gateway holds the lock.
        "son_of_anton_home": str(_canonical_son_of_anton_home(_get_process_son_of_anton_home())),
    }


def _build_runtime_status_record() -> dict[str, Any]:
    payload = _build_pid_record()
    payload.update({
        "gateway_state": "starting",
        "exit_reason": None,
        "restart_requested": False,
        "active_agents": 0,
        "platforms": {},
        "updated_at": _utc_now_iso(),
    })
    return payload


def _read_json_file(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        # OSError: file vanished or permission flipped between exists() and
        # read. UnicodeDecodeError: file holds non-UTF-8 / binary garbage
        # (a truncated or clobbered status file). Either way it's unusable.
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    atomic_json_write(path, payload, indent=None, separators=(",", ":"))


def _read_pid_record(pid_path: Optional[Path] = None) -> Optional[dict]:
    pid_path = pid_path or _get_pid_path()
    if not pid_path.exists():
        return None

    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        # File was deleted between exists() and read_text(), permission
        # flipped, or it holds non-UTF-8 / binary garbage.
        return None
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        try:
            return {"pid": int(raw)}
        except ValueError:
            return None

    if isinstance(payload, int):
        return {"pid": payload}
    if isinstance(payload, dict):
        return payload
    return None


def _pid_from_record(record: Optional[dict[str, Any]]) -> Optional[int]:
    if not record:
        return None
    try:
        return int(record["pid"])
    except (KeyError, TypeError, ValueError):
        return None


def _cleanup_invalid_pid_path(pid_path: Path, *, cleanup_stale: bool) -> None:
    """Delete a stale gateway PID file (and its sibling lock metadata).

    Called from ``get_running_pid()`` after the runtime lock has already been
    confirmed inactive, so the on-disk metadata is known to belong to a dead
    process.  Unlike ``remove_pid_file()`` (which refuses to delete a PID file
    whose ``pid`` field differs from ``os.getpid()``), this path force-unlinks
    both files so the next startup sees a clean slate.
    """
    if not cleanup_stale:
        return
    try:
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        _get_gateway_lock_path(pid_path).unlink(missing_ok=True)
    except Exception:
        pass


def _write_gateway_lock_record(handle) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(_build_pid_record(), handle)
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass


def _try_acquire_file_lock(handle) -> bool:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _pid_exists(pid: int) -> bool:
    """Is this PID alive? A check that does NOT kill the target.

    A zombie (defunct) process is still in the process table, so
    ``psutil.pid_exists()`` returns True for it — but it is already dead: it
    cannot be a running gateway or worker. Report zombies as dead. Any failure
    to read status (access denied, transient race) falls through to the
    authoritative ``pid_exists()``.
    """
    import psutil

    try:
        if psutil.Process(int(pid)).status() == psutil.STATUS_ZOMBIE:
            return False
    except psutil.NoSuchProcess:
        return False
    except Exception:
        pass
    return bool(psutil.pid_exists(int(pid)))


def _release_file_lock(handle) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def acquire_gateway_runtime_lock() -> bool:
    """Claim the cross-process runtime lock for the gateway.

    Unlike the PID file, the lock is owned by the live process itself. If the
    process dies abruptly, the OS releases the lock automatically.
    """
    global _gateway_lock_handle
    if _gateway_lock_handle is not None:
        return True

    path = _get_gateway_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    if not _try_acquire_file_lock(handle):
        handle.close()
        return False
    _write_gateway_lock_record(handle)
    _gateway_lock_handle = handle
    return True


def release_gateway_runtime_lock() -> None:
    """Release the gateway runtime lock when owned by this process."""
    global _gateway_lock_handle
    handle = _gateway_lock_handle
    if handle is None:
        return
    _gateway_lock_handle = None
    _release_file_lock(handle)
    try:
        handle.close()
    except OSError:
        pass


def is_gateway_runtime_lock_active(lock_path: Optional[Path] = None) -> bool:
    """Return True when some process currently owns the gateway runtime lock."""
    resolved_lock_path = lock_path or _get_gateway_lock_path()
    if _gateway_lock_handle is not None and resolved_lock_path == _get_gateway_lock_path():
        return True

    if not resolved_lock_path.exists():
        return False

    handle = open(resolved_lock_path, "a+", encoding="utf-8")
    try:
        if _try_acquire_file_lock(handle):
            _release_file_lock(handle)
            return False
        return True
    finally:
        try:
            handle.close()
        except OSError:
            pass


def write_pid_file() -> None:
    """Write the current process PID and metadata to the gateway PID file.

    Uses atomic O_CREAT | O_EXCL creation so that two concurrent starts race:
    exactly one process wins and the rest get FileExistsError.
    """
    path = _get_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps(_build_pid_record())
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(record)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_runtime_status(
    *,
    gateway_state: Any = _UNSET,
    exit_reason: Any = _UNSET,
    restart_requested: Any = _UNSET,
    active_agents: Any = _UNSET,
    platform: Any = _UNSET,
    platform_state: Any = _UNSET,
    error_code: Any = _UNSET,
    error_message: Any = _UNSET,
    needs_attention: Any = _UNSET,
    retrying_since: Any = _UNSET,
) -> None:
    """Persist gateway runtime health information for diagnostics/status."""
    path = _get_runtime_status_path()
    payload = _read_json_file(path) or _build_runtime_status_record()
    current_record = _build_pid_record()
    payload.setdefault("platforms", {})
    payload["kind"] = current_record["kind"]
    payload["pid"] = current_record["pid"]
    payload["argv"] = current_record["argv"]
    payload["start_time"] = current_record["start_time"]
    payload["updated_at"] = _utc_now_iso()

    if gateway_state is not _UNSET:
        payload["gateway_state"] = gateway_state
    if exit_reason is not _UNSET:
        payload["exit_reason"] = exit_reason
    if restart_requested is not _UNSET:
        payload["restart_requested"] = bool(restart_requested)
    if active_agents is not _UNSET:
        payload["active_agents"] = parse_active_agents(active_agents)

    if platform is not _UNSET:
        platform_payload = payload["platforms"].get(platform, {})
        if platform_state is not _UNSET:
            platform_payload["state"] = platform_state
        if error_code is not _UNSET:
            platform_payload["error_code"] = error_code
        if error_message is not _UNSET:
            platform_payload["error_message"] = error_message
        if needs_attention is not _UNSET:
            # Long-lived reconnect-loop escalation: True once a platform has
            # been continuously failing/retrying past the attention threshold.
            # Retry never stops — this is a signal for the owner, not a
            # circuit breaker. Cleared (False) on successful reconnect.
            platform_payload["needs_attention"] = bool(needs_attention)
        if retrying_since is not _UNSET:
            # ISO timestamp of when the platform entered its current
            # continuous retry episode; None clears it on reconnect.
            platform_payload["retrying_since"] = retrying_since
        platform_payload["updated_at"] = _utc_now_iso()
        # Writer identity: which PROCESS wrote this entry. The top-level
        # pid/start_time are refreshed on every write, so they only identify
        # the file's most recent writer.
        platform_payload["writer_pid"] = current_record["pid"]
        platform_payload["writer_start_time"] = current_record["start_time"]
        payload["platforms"][platform] = platform_payload

    _write_json_file(path, payload)


def read_runtime_status(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Read the persisted gateway runtime health/status information."""
    return _read_json_file(path or _get_runtime_status_path())


# Max age of a persisted ``gateway_state.json`` snapshot before its liveness
# claim is treated as suspect.  A healthy gateway rewrites the file (advancing
# ``updated_at``) far more often than this; a record older than the TTL whose
# PID is also dead almost certainly outlived an ungracefully-killed writer
# (SIGKILL, OOM, power loss) that never ran its shutdown handler.
_RUNTIME_STATUS_STALE_TTL_S = 120


def runtime_status_is_stale(
    record: Optional[dict[str, Any]],
    ttl_s: int = _RUNTIME_STATUS_STALE_TTL_S,
) -> bool:
    """Return True when the runtime-status snapshot is older than ``ttl_s``.

    A missing or unparseable ``updated_at`` is treated as stale (the freshness
    signal is absent, so it cannot vouch for the record).
    """
    if not isinstance(record, dict):
        return True
    try:
        written_dt = datetime.fromisoformat(record.get("updated_at") or "")
        age = (datetime.now(timezone.utc) - written_dt).total_seconds()
        return age > ttl_s
    except (TypeError, ValueError):
        return True


def runtime_status_pid_is_live(record: Optional[dict[str, Any]]) -> bool:
    """Return True when the PID recorded in the snapshot is still alive.

    Uses the no-kill :func:`_pid_exists` probe and the same ``start_time``
    PID-reuse guard as :func:`get_runtime_status_running_pid`: when both the
    recorded and live start-times are known they must match, so a recycled PID
    is not mistaken for the original.  Degrades to ``False`` when the record has
    no usable PID.
    """
    pid = _pid_from_record(record)
    if pid is None or not _pid_exists(pid):
        return False
    recorded_start = (record or {}).get("start_time")
    current_start = _get_process_start_time(pid)
    if (
        recorded_start is not None
        and current_start is not None
        and current_start != recorded_start
    ):
        return False
    return True


def parse_active_agents(raw: Any) -> int:
    """Coerce a persisted ``active_agents`` value to a clamped non-negative int.

    Never negative, never raising on a manually-edited or otherwise
    non-numeric value (degrades to ``0``).
    """
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def get_runtime_status_running_pid(
    runtime: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """Return a live gateway PID from the runtime status record, if valid.

    ``get_running_pid()`` is the primary liveness source because it verifies the
    runtime lock and PID file. A live process can still have a fresh
    ``gateway_state.json`` but no ``gateway.pid``; use this as a conservative
    fallback by checking both the persisted state and the OS process identity.
    """
    payload = runtime if runtime is not None else read_runtime_status()
    if not isinstance(payload, dict):
        return None
    if payload.get("gateway_state") in {None, "stopped", "startup_failed"}:
        return None

    pid = _pid_from_record(payload)
    if pid is None or not _pid_exists(pid):
        return None

    recorded_start = payload.get("start_time")
    current_start = _get_process_start_time(pid)
    if (
        recorded_start is not None
        and current_start is not None
        and current_start != recorded_start
    ):
        return None

    if _record_matches_live_gateway_pid(payload, pid):
        return pid
    return None


def remove_pid_file() -> None:
    """Remove the gateway PID file, but only if it belongs to this process.

    A late exit handler of an old process must not delete a newer process's
    record, leaving that gateway running with no PID file (invisible to
    ``get_running_pid()``).
    """
    try:
        path = _get_pid_path()
        record = _read_json_file(path)
        if record is not None:
            try:
                file_pid = int(record["pid"])
            except (KeyError, TypeError, ValueError):
                file_pid = None
            if file_pid is not None and file_pid != os.getpid():
                # PID file belongs to a different process — leave it alone.
                return
        path.unlink(missing_ok=True)
    except Exception:
        pass


def acquire_scoped_lock(scope: str, identity: str, metadata: Optional[dict[str, Any]] = None) -> tuple[bool, Optional[dict[str, Any]]]:
    """Acquire a machine-local lock keyed by scope + identity.

    Used to prevent multiple local gateways from using the same external identity
    at once (e.g. the same Signal account across different SON_OF_ANTON_HOME dirs).
    """
    lock_path = _get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **_build_pid_record(),
        "scope": scope,
        "identity_hash": _scope_hash(identity),
        "metadata": metadata or {},
        "updated_at": _utc_now_iso(),
    }
    # Human-readable owner label for conflict diagnostics: a bare PID gives
    # an operator no way to tell WHICH gateway owns the credential. Readers
    # fall back to deriving it from son_of_anton_home.
    profile = _profile_label_for_home(_get_process_son_of_anton_home())
    if profile:
        record["profile"] = profile

    existing = _read_json_file(lock_path)
    if existing is None and lock_path.exists():
        # Lock file exists but is empty or contains invalid JSON — treat as
        # stale.  This happens when a previous process was killed between
        # O_CREAT|O_EXCL and the subsequent json.dump().
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    if existing:
        try:
            existing_pid = int(existing["pid"])
        except (KeyError, TypeError, ValueError):
            existing_pid = None

        # Same live PID as this process: always self-reacquire.
        # ``start_time`` is a PID-reuse guard for *other* PIDs; it cannot
        # distinguish two processes that share the caller's own PID (impossible
        # while we are alive). Requiring start_time equality here falsely
        # rejects reconnects when the on-disk record has ``start_time: null``
        # while the freshly built record has a real value — the gateway then
        # reports itself as the foreign squatter of its own token.
        if existing_pid == os.getpid():
            _write_json_file(lock_path, record)
            return True, existing

        stale = existing_pid is None
        if not stale:
            if not _pid_exists(existing_pid):
                stale = True
            else:
                current_start = _get_process_start_time(existing_pid)
                if (
                    existing.get("start_time") is not None
                    and current_start is not None
                    and current_start != existing.get("start_time")
                ):
                    stale = True
                # When start_time comparison is unavailable on either side
                # (the record's start_time may be None, or /proc unreadable),
                # fall back to checking the live process command line. When
                # cmdline is also unreadable, consult the lock record's own
                # argv — the gateway writes it at startup.  Both oracles must
                # indicate "not a gateway" to mark stale.
                if (
                    not stale
                    and (existing.get("start_time") is None or current_start is None)
                    and not _looks_like_gateway_process(existing_pid)
                ):
                    live_cmdline = _read_process_cmdline(existing_pid)
                    if live_cmdline is not None or not _record_looks_like_gateway(existing):
                        stale = True
                # Secondary defence against boot-time PID+start_time collisions:
                # systemd spawns core services deterministically, so an unrelated
                # process (e.g. cron) can land on the exact same PID and jiffy
                # count as a previous gateway. If both start_times are known and
                # match but the live process is not a gateway, and we can confirm
                # that by reading its cmdline, the lock is stale.
                if (
                    not stale
                    and existing.get("start_time") is not None
                    and current_start is not None
                    and not _looks_like_gateway_process(existing_pid)
                ):
                    live_cmdline = _read_process_cmdline(existing_pid)
                    if live_cmdline is not None:
                        stale = True
                # Check if process is stopped (Ctrl+Z / SIGTSTP) — stopped
                # processes still appear alive to _pid_exists but are not
                # actually running. Treat them as stale.
                if not stale:
                    try:
                        _proc_status = Path(f"/proc/{existing_pid}/status")
                        if _proc_status.exists():
                            for _line in _proc_status.read_text(encoding="utf-8").splitlines():
                                if _line.startswith("State:"):
                                    _state = _line.split()[1]
                                    if _state in {"T", "t"}:  # stopped or tracing stop
                                        stale = True
                                    break
                    except (OSError, PermissionError):
                        pass
        if stale:
            # Remove the stale lock ATOMICALLY by renaming it to a tombstone
            # instead of unlinking. With unlink()+O_EXCL, two racing starters
            # could both observe "removed" (the second unlink() silently
            # deleting the first racer's freshly-created lock) and both win.
            # os.replace() is atomic: exactly one racer claims the stale
            # file; the loser gets FileNotFoundError and falls through to
            # the O_EXCL create below, where at most one process succeeds.
            tombstone = lock_path.with_name(lock_path.name + ".stale")
            try:
                os.replace(lock_path, tombstone)
            except FileNotFoundError:
                # Another racer already claimed the stale lock (and may have
                # created a fresh one) — let O_EXCL below decide the winner.
                pass
            except OSError:
                pass
            else:
                try:
                    tombstone.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            return False, existing

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False, _read_json_file(lock_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True, None


def release_scoped_lock(scope: str, identity: str) -> None:
    """Release a previously-acquired scope lock when owned by this process."""
    lock_path = _get_scope_lock_path(scope, identity)
    existing = _read_json_file(lock_path)
    if not existing:
        return
    if existing.get("pid") != os.getpid():
        return
    # Same PID as the live process means we own the lock. Do not require
    # start_time equality (see acquire_scoped_lock): start_time only guards
    # PID reuse for *other* PIDs.
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def get_running_pid(
    pid_path: Optional[Path] = None,
    *,
    cleanup_stale: bool = True,
) -> Optional[int]:
    """Return the PID of a running gateway instance, or ``None``.

    Checks the PID file and verifies the process is actually alive.
    Cleans up stale PID files automatically.
    """
    resolved_pid_path = pid_path or _get_pid_path()
    resolved_lock_path = _get_gateway_lock_path(resolved_pid_path)
    lock_active = is_gateway_runtime_lock_active(resolved_lock_path)
    if not lock_active:
        if pid_path is None:
            runtime_pid = get_runtime_status_running_pid()
            if runtime_pid is not None:
                return runtime_pid
        _cleanup_invalid_pid_path(resolved_pid_path, cleanup_stale=cleanup_stale)
        return None

    primary_record = _read_pid_record(resolved_pid_path)
    fallback_record = _read_pid_record(resolved_lock_path)

    for record in (primary_record, fallback_record):
        pid = _pid_from_record(record)
        if pid is None:
            continue

        if not _pid_exists(pid):
            continue

        recorded_start = record.get("start_time")
        current_start = _get_process_start_time(pid)
        if recorded_start is not None and current_start is not None and current_start != recorded_start:
            continue

        if _record_matches_live_gateway_pid(record, pid):
            return pid

    _cleanup_invalid_pid_path(resolved_pid_path, cleanup_stale=cleanup_stale)
    if pid_path is None:
        runtime_pid = get_runtime_status_running_pid()
        if runtime_pid is not None:
            return runtime_pid
    return None
