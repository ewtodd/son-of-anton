"""
Backup and import commands for son-of-anton CLI.

`son-of-anton backup` creates a zip archive of the entire ~/.son-of-anton/ directory
(excluding the son-of-anton repo and transient files).

`son-of-anton import` restores from a backup zip, overlaying onto the current
SON_OF_ANTON_HOME root.
"""

import logging
from pathlib import Path


# Shared formatter; the private alias is kept because claw.py and the backup
# tests import ``_format_size`` from this module.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exclusion rules
# ---------------------------------------------------------------------------

# Directory names to skip entirely (matched against each path component)
# ``son-of-anton`` is special-cased to root level only in ``_should_exclude``
# so that skill directories like ``skills/autonomous-ai-agents/son-of-anton/``
# are not accidentally excluded.
#
# The dependency/cache entries below matter for more than tidiness: without
# them a single plugin venv, MCP-server install, or pip/uv cache living under
# SON_OF_ANTON_HOME gets walked file-by-file, ballooning a backup to hundreds of
# thousands of entries that crawl for hours — the exact "backup stuck for
# days / 426543 files" symptom users hit. The dependency/test-env names mostly
# mirror ``agent.skill_utils.EXCLUDED_SKILL_DIRS`` (the project's canonical
# "regeneratable dir" set); ``.cache`` is an additional backup-only entry, as
# it names a broad regeneratable cache convention (pip/uv/etc.) that the skill
# scanner doesn't need to prune but a backup walk does. We deliberately do NOT
# exclude ``.archive`` here because the curator's ``skills/.archive/`` holds
# restorable user skills that must survive a backup.

# File-name suffixes to skip

# File names to skip (runtime state that's meaningless on another machine)

# File names that ``son-of-anton import`` must never overwrite, matched by basename so
# they're caught for the root profile (``gateway_state.json``) and for named
# profiles alike (``profiles/<name>/gateway_state.json``).
#
# These hold *volatile gateway/process runtime state that is namespaced to the
# machine the backup was taken on* — PIDs in a dead process namespace, a
# runtime lock, the process registry, and the gateway's last recorded
# run/desired state. Restoring them onto a different host is at best
# meaningless and at worst actively harmful:
#
#   - ``gateway_state.json`` drives the boot reconciler, which only
#     auto-starts a gateway whose recorded state is ``running``. A backup
#     taken from a machine where the gateway was stopped (or carrying a
#     stale/foreign value) overwrites the target's own state and leaves the
#     gateway stuck "starting"/"cooking", disconnecting it from the Nous
#     portal (NS-508 / the second half of NS-501).
#   - ``gateway.pid`` / ``cron.pid`` / ``gateway.lock`` / ``processes.json``
#     reference PIDs and locks in the *source* machine's process namespace; a
#     numerically-equal PID in the new environment is a different process.
#     These mirror exactly what the boot-time stale-runtime-file sweep
#     already removes on every start.
#
# Older backups predate the backup-side exclusions, so we filter on import too
# rather than trusting the archive's contents.

# zipfile.open() drops Unix mode bits on extract; restore tightens these to 0600.

# Reserved archive subtree for provider state that lives OUTSIDE SON_OF_ANTON_HOME
# (e.g. ~/.honcho, ~/.hindsight). The active memory provider declares these via
# MemoryProvider.backup_paths(); they're stored under this prefix encoded
# relative to the user's home directory, and restored to their original
# home-relative location on import. Anything not under home is skipped.


# ---------------------------------------------------------------------------
# SQLite safe copy
# ---------------------------------------------------------------------------


def is_zeroed_sqlite_file(
    path: Path, *, probe_bytes: int = 100, force: bool = False
) -> bool:
    """True when *path* looks like the #68474 zeroed-state.db signature.

    Signature: size > 0, first *probe_bytes* are all NUL (no ``SQLite format 3``
    header). Used at SessionDB open and for snapshot diagnostics so a silent
    all-zero file becomes a guided recovery instead of a generic failure.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    from son_of_anton_cli.sqlite_safe_read import read_header_bytes_preopen

    head = read_header_bytes_preopen(
        path, length=max(16, probe_bytes), force=force
    )
    if not head:
        return False
    if head.startswith(b"SQLite format 3"):
        return False
    return all(byte == 0 for byte in head)


# ---------------------------------------------------------------------------
# SQLite integrity verification
# ---------------------------------------------------------------------------


# Default ceiling above which ``PRAGMA integrity_check`` is skipped in favour
# of the (O(1)) header + structural probe. ``integrity_check`` walks every
# b-tree page in the file, so its cost scales with database size: on a 30 GB
# state.db it runs for many minutes of pegged CPU with no output, which reads
# to the user as a hung `son-of-anton update` (#70553 follow-up). Sessions databases
# in the tens of GB are normal for heavy users, so the size-unbounded check is
# never an acceptable default on the update path.


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Quick state snapshots (used by /snapshot slash command and son-of-anton backup --quick)
# ---------------------------------------------------------------------------

# Critical state files to include in quick snapshots (relative to SON_OF_ANTON_HOME).
# Everything else is either regeneratable (logs, cache) or managed separately
# (skills, repo, sessions/).
#
# Entries may be individual files OR directories.  Directories are captured
# recursively; missing entries are silently skipped.  Pairing data lives in
# platform-specific JSON blobs outside state.db, so it's listed here explicitly
# — `son-of-anton update` snapshots this set before pulling so approved-user lists
# are recoverable if anything goes wrong (issue #15733).


# Relative path of the cron job database inside SON_OF_ANTON_HOME. Kept in sync with
# the entry in ``_QUICK_STATE_FILES`` and with ``cron/jobs.py``'s ``JOBS_FILE``.


# ---------------------------------------------------------------------------
# Shared full-zip backup helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pre-update auto-backup
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pre-migration auto-backup (used by `son-of-anton claw migrate`)
# ---------------------------------------------------------------------------


