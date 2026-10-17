"""Tirith pre-exec security scanning wrapper.

Runs the tirith binary as a subprocess to scan commands for content-level
threats (homograph URLs, pipe-to-interpreter, terminal injection, etc.).

Exit code is the verdict source of truth:
  0 = allow, 1 = block, 2 = warn

JSON stdout enriches findings/summary but never overrides the verdict.
Operational failures (spawn error, timeout, unknown exit code) fail open.
Programming errors propagate.

The Nix wrapper puts tirith on PATH; ``security.tirith_enabled`` turns the
scan off.
"""

import json
import logging
import shutil
import subprocess
import threading

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5


def _tirith_enabled() -> bool:
    try:
        from son_of_anton_cli.config import load_config_readonly
        cfg = load_config_readonly().get("security", {}) or {}
    except Exception:
        cfg = {}
    return bool(cfg.get("tirith_enabled", True))


# Cached path after the first successful PATH lookup.
_resolved_path: str | None = None

# Circuit breaker: after _CRASH_LIMIT consecutive spawn/execution failures,
# disable tirith for the rest of the process to prevent agent hangs.
# Reset on successful execution (see _record_tirith_crash / check_command_security).
#
# Thread safety: _crash_count and _circuit_open are module-level globals
# mutated without a lock. check_command_security can be called from
# concurrent agent threads (gateway multi-session). The race is benign —
# at worst two threads both increment past _CRASH_LIMIT and both set
# _circuit_open = True, opening the breaker one call early. No data
# corruption or security bypass is possible.
_CRASH_LIMIT = 3
_crash_count: int = 0
_circuit_open: bool = False


def _record_tirith_crash() -> None:
    """Increment the crash counter and open the circuit breaker if needed."""
    global _crash_count, _circuit_open
    _crash_count += 1
    if _crash_count >= _CRASH_LIMIT:
        _circuit_open = True
        logger.warning(
            "tirith circuit breaker opened after %d consecutive failures; "
            "disabling for the rest of the process",
            _crash_count,
        )


# Warning de-duplication: the spawn/path warnings live in the hot path, so a
# missing binary would otherwise log once per terminal command.
_warned_messages: set[str] = set()
_warned_lock = threading.Lock()


def _warn_once(key: str, message: str, *args) -> None:
    """``logger.warning`` but at-most-once per ``key`` for the process
    lifetime. Used to avoid drowning the log when a fail-open tirith
    misconfiguration fires on every command."""
    with _warned_lock:
        if key in _warned_messages:
            return
        _warned_messages.add(key)
    logger.warning(message, *args)


def _resolve_tirith_path() -> str | None:
    """Return the tirith binary on PATH (cached once found), or None."""
    global _resolved_path
    if _resolved_path is None:
        _resolved_path = shutil.which("tirith")
    return _resolved_path


def ensure_installed():
    """Return the tirith path when scanning is enabled and the binary is on PATH."""
    if not _tirith_enabled():
        return None
    return _resolve_tirith_path()


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

_MAX_FINDINGS = 50
_MAX_SUMMARY_LEN = 500


def check_command_security(command: str) -> dict:
    """Run tirith security scan on a command.

    Exit code determines action (0=allow, 1=block, 2=warn). JSON enriches
    findings/summary. Spawn failures and timeouts fail open. Programming
    errors propagate.

    Returns:
        {"action": "allow"|"warn"|"block", "findings": [...], "summary": str}
    """
    global _crash_count, _circuit_open

    if not _tirith_enabled():
        return {"action": "allow", "findings": [], "summary": ""}

    # Circuit breaker: if tirith has crashed _CRASH_LIMIT times in a row,
    # stop trying for the rest of the process.  Without this, a corrupted
    # binary causes every tool call to hit the same spawn failure
    # -> fail-open -> agent retry loop, hanging the user for 20+ minutes.
    if _circuit_open:
        return {"action": "allow", "findings": [], "summary": "tirith disabled (circuit breaker)"}

    tirith_path = _resolve_tirith_path()
    timeout = _TIMEOUT_SECONDS

    if tirith_path is None:
        _warn_once(
            "tirith_path_none",
            "tirith not found on PATH; scanning disabled",
        )
        return {"action": "allow", "findings": [], "summary": "tirith path unavailable"}

    try:
        result = subprocess.run(
            [tirith_path, "check", "--json", "--non-interactive",
             "--shell", "posix", "--", command],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        # Covers FileNotFoundError, PermissionError, exec format error.
        # Dedupe by ``(errno, exc class)`` so a transient failure mode
        # surfaces once but doesn't drown the log on every command.
        spawn_key = f"tirith_spawn_failed:{type(exc).__name__}:{getattr(exc, 'errno', '')}"
        _warn_once(spawn_key, "tirith spawn failed: %s", exc)
        _record_tirith_crash()
        return {"action": "allow", "findings": [], "summary": f"tirith unavailable: {exc}"}
    except subprocess.TimeoutExpired:
        _warn_once(
            f"tirith_timeout:{timeout}",
            "tirith timed out after %ds",
            timeout,
        )
        _record_tirith_crash()
        return {"action": "allow", "findings": [], "summary": f"tirith timed out ({timeout}s)"}

    # Map exit code to action
    exit_code = result.returncode
    if exit_code == 0:
        action = "allow"
        # Successful execution — reset circuit breaker
        _crash_count = 0
    elif exit_code == 1:
        action = "block"
    elif exit_code == 2:
        action = "warn"
    else:
        # Unknown exit code (includes signal-killed processes like -11/SIGSEGV)
        # — fail open
        logger.warning("tirith returned unexpected exit code %d", exit_code)
        _record_tirith_crash()
        return {"action": "allow", "findings": [], "summary": f"tirith exit code {exit_code} (fail-open)"}

    # Parse JSON for enrichment (never overrides the exit code verdict)
    findings = []
    summary = ""
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        raw_findings = data.get("findings", [])
        findings = raw_findings[:_MAX_FINDINGS]
        summary = (data.get("summary", "") or "")[:_MAX_SUMMARY_LEN]
    except (json.JSONDecodeError, AttributeError):
        # JSON parse failure degrades findings/summary, not the verdict
        logger.debug("tirith JSON parse failed, using exit code only")
        if action == "block":
            summary = "security issue detected (details unavailable)"
        elif action == "warn":
            summary = "security warning detected (details unavailable)"

    # Suppress warn verdicts that consist solely of a lookalike_tld finding for
    # the .app TLD.  .app is a legitimate gTLD used by many production services
    # and the "can be confused with file extensions" heuristic generates false
    # positives for normal API calls.  Any other finding (including other
    # lookalike_tld entries for non-.app TLDs) preserves the warn action.
    if action == "warn" and findings:
        non_suppressible = [f for f in findings if not _is_app_tld_finding(f)]
        if not non_suppressible:
            action = "allow"
            findings = []
            summary = ""

    return {"action": action, "findings": findings, "summary": summary}


def _is_app_tld_finding(finding: dict) -> bool:
    """Return True if this finding is a lookalike_tld warning for the .app TLD only.

    Checks the rule_id and inspects common value/detail field names that
    Tirith may use to carry the TLD string.
    """
    if not isinstance(finding, dict):
        return False
    if finding.get("rule_id") != "lookalike_tld":
        return False
    for field in ("value", "tld", "detail", "description", "message"):
        val = finding.get(field)
        if val is not None and ".app" in str(val).lower():
            return True
    return False
