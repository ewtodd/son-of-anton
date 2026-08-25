"""Subprocess compatibility helpers.

Son of Anton is Nix-only (Linux + macOS). The Windows-specific helpers in
this module were stripped; the remaining functions are kept because call
sites across the codebase still import them.

**All helpers are no-ops or POSIX-equivalents on Nix platforms** — calling
them in Linux/macOS code paths is safe by design.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Mapping, Sequence

__all__ = [
    "resolve_node_command",
    "split_command_line",
    "bounded_git_probe",
    "bounded_probe_run",
    "kill_process_tree",
    "noninteractive_git_env",
]


def split_command_line(line: str) -> list[str]:
    """Split a user-supplied command line into tokens.

    ``shlex.split(line)`` — POSIX tokenization.

    Raises ValueError for unbalanced quotes, same as ``shlex.split``.
    """
    import shlex

    return shlex.split(line)


# -----------------------------------------------------------------------------
# Node ecosystem launcher resolution
# -----------------------------------------------------------------------------


def resolve_node_command(name: str, argv: Sequence[str]) -> list[str]:
    """Resolve a Node-ecosystem command name to an absolute-path argv.

    ``shutil.which(name)`` returns a fully-qualified path when found, which
    is functionally identical to bare-name resolution (the OS does its own
    PATH search) with the side benefit of making the argv reproducible in
    logs. When the command is not on PATH, returns the bare name — the
    subsequent Popen will raise FileNotFoundError with a readable error.

    Args:
        name: The command name to resolve (``npm``, ``npx``, ``node`` …).
        argv: The remaining arguments.  Must NOT include ``name`` itself —
            this function builds the full argv list.

    Returns:
        A list suitable for passing to subprocess.Popen/run/call.
    """
    resolved = shutil.which(name)
    if resolved:
        return [resolved, *argv]
    return [name, *argv]


# -----------------------------------------------------------------------------
# Non-interactive git environment (credential-prompt hang guard)
# -----------------------------------------------------------------------------


def noninteractive_git_env(
    base: "Mapping[str, str] | None" = None,
) -> dict[str, str]:
    """Environment for *internal* git invocations that must never prompt.

    Son of Anton shells out to git from many non-interactive contexts — MCP catalog
    installs, plugin install/update, profile distribution staging, worktree
    base fetches, desktop review-pane fetch/push. When the remote is private,
    misconfigured, or requires auth, git's default behavior is to prompt on
    the inherited terminal (or via an askpass helper), which silently hangs
    the operation until its timeout — or forever at call sites without one.
    Ported from openai/codex#34540 / #34612 ("detach non-interactive
    subprocesses from stdin"): a background tool invocation must fail fast
    with a readable error, not wait for input nobody can type.

    Returns a copy of ``base`` (default ``os.environ``) with:

    * ``GIT_TERMINAL_PROMPT=0`` — git fails with "terminal prompts disabled"
      instead of prompting for credentials.
    * ``GCM_INTERACTIVE=Never`` — Git Credential Manager never pops its own
      dialog.

    ``GIT_ASKPASS`` / ``SSH_ASKPASS`` are deliberately left alone: when the
    user has a *working* askpass helper or ssh-agent configured, auth should
    still succeed non-interactively. The env only disables paths that block
    on a human.

    Pair with ``stdin=subprocess.DEVNULL`` so git (and any credential helper
    it spawns) also can't read the parent's inherited stdin.

    This is for internal plumbing calls only — the agent-facing terminal tool
    has its own policy layer and user-visible PTY, where prompting can be
    legitimate.
    """
    env = dict(base if base is not None else os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    return env


# -----------------------------------------------------------------------------
# Bounded, fail-open git probing
# -----------------------------------------------------------------------------


def kill_process_tree(proc: "subprocess.Popen") -> None:
    """Best-effort terminate *proc* and its descendants on POSIX.

    ``proc.kill()`` alone only terminates the direct child; killing the
    launcher can leave descendants (credential helpers, ``git-remote-https``,
    hook children) running and holding the pipe write ends. Callers spawn the
    child in its own process group (``process_group=0``, Python ≥3.11), so
    when — and only when — the child leads its own group (``pgid == pid``),
    the entire group is signalled with ``os.killpg``. The ownership check
    means a fallback spawn that shares our group can never cause us to kill
    unrelated processes. Ported from openai/codex#36793 ("Terminate timed-out
    Git process trees"); generalized for the shell-hook runner via
    openai/codex#37527 ("Terminate timed-out hook process trees").

    All failures are swallowed — this is cleanup on an already-failing path, and
    the caller's contract is to fail open. ``kill()`` can raise (access denied,
    already reaped); an unhandled raise here would escape the caller's ``except``
    handler and break that contract.
    """
    # Group-kill first: verify the child actually leads its own process
    # group before signalling it, so we never blast a shared group.
    try:
        import signal as _signal

        pgid = os.getpgid(proc.pid)
        if pgid == proc.pid:
            os.killpg(pgid, _signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def bounded_probe_run(
    argv: Sequence[str],
    *,
    timeout: float,
    errors: str = "replace",
) -> "subprocess.CompletedProcess[str] | None":
    """Deadlock-safe ``subprocess.run(argv, capture_output=True, timeout=...)``
    for fail-open probe call sites. Returns a ``CompletedProcess`` when the
    child finished within *timeout* (any exit code), or ``None`` on spawn
    failure or timeout.

    Why not ``subprocess.run``: ``run()``'s post-timeout cleanup calls an
    *unbounded* ``communicate()`` after killing the direct child. Killing it
    can leave a descendant holding duplicates of the captured stdout/stderr
    handles, so the pipes never reach EOF and the reader-thread join blocks
    forever.

    The bounded flow: an explicit ``communicate(timeout)``, then on any
    failure a tree-kill (see :func:`kill_process_tree`) plus a bounded 1s
    post-kill drain; if the pipes are still held after that, they're abandoned
    (the orphaned reader threads are daemonic and cost nothing).

    The child is placed in its own process group (``process_group=0``,
    Python ≥3.11) so timeout cleanup can take down descendants with the
    launcher instead of orphaning them.
    """
    _popen_kwargs: dict = {"process_group": 0}
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors=errors,
            **_popen_kwargs,
        )
    except Exception:
        return None
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except Exception:
        # Timeout OR any other communicate() failure (torn-down pipe, decode
        # error): terminate the child + descendants and drain bounded. Leaving
        # it running would leak the same suspended-descendant class this guards.
        kill_process_tree(proc)
        try:
            proc.communicate(timeout=1)
        except Exception:
            pass
        return None
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)


def bounded_git_probe(argv: Sequence[str], *, timeout: float) -> str:
    """Run a short, throwaway ``git`` probe and return stripped stdout, or ``""``
    on ANY failure (nonzero exit, timeout, spawn error, decode error).

    This is the shared, deadlock-safe replacement for
    ``subprocess.run(["git", ...], timeout=...)`` at fail-open probe call sites
    (``agent.coding_context._git``).

    The bounded flow: an explicit ``communicate(timeout)``, then on any failure a
    tree-kill (see :func:`kill_process_tree`) plus a bounded 1s post-kill
    drain; if the pipes are still held after that, they're abandoned (the orphaned
    reader threads are daemonic and cost nothing).

    The probe is placed in its own process group (``process_group=0``,
    Python ≥3.11) so timeout cleanup can take down descendants — credential
    helpers, ``git-remote-https``, hook children — with the launcher instead of
    orphaning them (see :func:`kill_process_tree`; port of
    openai/codex#36793). ``process_group`` only changes which group the child
    belongs to; it does not detach the terminal or alter the fast path.
    """
    result = bounded_probe_run(argv, timeout=timeout)
    if result is None or result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


# Backward-compat alias — existing call sites/tests import the historical name.
_kill_git_process_tree = kill_process_tree
