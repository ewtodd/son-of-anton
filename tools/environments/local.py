"""Local execution environment — spawn-per-call with session snapshot."""

import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from son_of_anton_constants import get_process_son_of_anton_home
from tools.environments.base import BaseEnvironment, _pipe_stdin

logger = logging.getLogger(__name__)


def _resolve_local_initial_cwd(cwd: str) -> str:
    """Resolve the local backend's initial cwd to an absolute host path.

    ``TERMINAL_CWD`` can be populated from config.yaml before the terminal
    backend is created.  If that value is relative and happens to match the
    directory Son of Anton was already launched from (for example ``son-of-anton``
    while the process cwd is ``~/.son-of-anton/son-of-anton``), passing it through
    unchanged makes the wrapper run ``cd son-of-anton`` *inside* the project
    and fail with a confusing nested-path error.  Anchor relative local cwd
    values once, up front, so both ``subprocess.Popen(cwd=...)`` and the
    in-shell ``cd`` use the same absolute directory.
    """
    expanded = os.path.expanduser(cwd) if cwd else os.getcwd()
    if os.path.isabs(expanded):
        return expanded

    candidate = os.path.abspath(expanded)
    current = os.getcwd()

    # Common recovery for config values like ``son-of-anton`` when Son of Anton was
    # launched from that directory already.  ``os.path.abspath`` would point at
    # a nonexistent nested ``./son-of-anton``; use the current directory instead.
    if not os.path.isdir(candidate):
        wanted_parts = Path(expanded).parts
        current_parts = Path(current).parts
        if wanted_parts and len(wanted_parts) <= len(current_parts):
            if current_parts[-len(wanted_parts):] == wanted_parts:
                return current

    return candidate


def _cwd_usable(path: str) -> bool:
    """True when *path* is a directory this process can actually chdir into.

    ``os.path.isdir`` alone is not enough: stat() on ``/root`` succeeds for a
    non-root user (only ``/`` needs search permission), but
    ``subprocess.Popen(cwd='/root')`` then dies with ``PermissionError:
    [Errno 13] Permission denied: '/root'``. Seen in the wild when a
    root-launched CLI session leaks ``/root`` into shared state that a
    non-root gateway/cron process later reads (#65583) — every cron job's
    terminal/file tool then fails on every command, forever. Checking
    X_OK up front lets the caller fall back instead.
    """
    return os.path.isdir(path) and os.access(path, os.X_OK)


def _resolve_safe_cwd(cwd: str) -> str:
    """Return ``cwd`` if it exists as a directory this process can enter,
    else the nearest existing accessible ancestor.  Falls back to
    ``tempfile.gettempdir()`` only if walking up the path can't find any
    usable directory (effectively never on a healthy filesystem, but cheap
    belt-and-braces).

    Used by ``_run_bash`` to recover when the configured cwd is gone — most
    commonly because a previous tool call deleted its own working directory
    (issue #17558) — or inaccessible to this user, e.g. ``/root`` leaking
    from a root-launched CLI session into a non-root gateway's cron jobs
    (issue #65583).  Without this guard, ``subprocess.Popen(..., cwd=...)``
    raises ``FileNotFoundError``/``PermissionError`` before bash starts,
    wedging every subsequent terminal call until the gateway restarts.
    """
    if cwd and _cwd_usable(cwd):
        return cwd
    if cwd and os.path.isdir(cwd):
        logger.warning(
            "Configured terminal cwd %r exists but is not accessible to "
            "this user (uid=%s) — falling back to the nearest usable "
            "directory. If this is a gateway/cron process, check for "
            "root-owned paths leaking into terminal.cwd / TERMINAL_CWD "
            "(#65583).",
            cwd, getattr(os, "getuid", lambda: "?")(),
        )
    parent = os.path.dirname(cwd) if cwd else ""
    while parent:
        if _cwd_usable(parent):
            return parent
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            # Reached the filesystem root and it doesn't exist either —
            # genuinely nothing to fall back to except the temp dir.
            break
        parent = next_parent
    return tempfile.gettempdir()


# Son of Anton-internal env vars that should NOT leak into terminal subprocesses.
_SON_OF_ANTON_PROVIDER_ENV_FORCE_PREFIX = "_SON_OF_ANTON_FORCE_"

# Son of Anton-managed AWS *inference* credentials for ``auth_type="aws_sdk"``
# providers (Bedrock).  Scoped DELIBERATELY NARROW: this lists only the
# Bedrock-specific bearer token, which is a Son of Anton inference secret exactly
# analogous to ``OPENAI_API_KEY`` — nobody drives the ``aws``/``terraform``/
# ``boto3`` toolchain off it, so stripping it from terminal/execute_code
# subprocesses costs no user capability.
#
# The GENERAL AWS credential chain (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
# AWS_SESSION_TOKEN, AWS_PROFILE, and the config/role pointers) is INTENTIONALLY
# left inheritable.  The local terminal is the user's trusted operator shell; the agent having the same general AWS access the
# user's own shell has is the intended posture, not a leak.  Hard-blocklisting
# those vars would (a) regress every user who runs aws/terraform/cdk/boto3 in
# the agent terminal — not just Bedrock users, since the registry is iterated
# unconditionally — and (b) be unrecoverable, because env_passthrough.py
# refuses to re-allow anything in this blocklist (GHSA-rhgp-j443-p4rf).  See
# issue #32314 discussion.
_AWS_SDK_CREDENTIAL_ENV_VARS = frozenset({
    "AWS_BEARER_TOKEN_BEDROCK",
})


def _build_provider_env_blocklist() -> frozenset:
    """Derive the blocklist from provider, tool, and gateway config."""
    blocked: set[str] = set()

    try:
        from son_of_anton_cli.auth import PROVIDER_REGISTRY
        for pconfig in PROVIDER_REGISTRY.values():
            blocked.update(pconfig.api_key_env_vars)
            if pconfig.auth_type == "aws_sdk":
                blocked.update(_AWS_SDK_CREDENTIAL_ENV_VARS)
            if pconfig.base_url_env_var:
                blocked.add(pconfig.base_url_env_var)
    except ImportError:
        pass

    try:
        from son_of_anton_cli.config import OPTIONAL_ENV_VARS
        for name, metadata in OPTIONAL_ENV_VARS.items():
            category = metadata.get("category")
            if category in {"tool", "messaging"}:
                blocked.add(name)
            elif category == "setting" and metadata.get("password"):
                blocked.add(name)
    except ImportError:
        pass

    blocked.update({
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "LLM_MODEL",
        "GOOGLE_API_KEY",
        # Path to a GCP service-account JSON, not a bare key, so
        # OPTIONAL_ENV_VARS marks it password=False and the loop above skips it.
        "GOOGLE_APPLICATION_CREDENTIALS",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "PERPLEXITY_API_KEY",
        "COHERE_API_KEY",
        "FIREWORKS_API_KEY",
        "XAI_API_KEY",
        "HELICONE_API_KEY",
        "PARALLEL_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "TELEGRAM_HOME_CHANNEL",
        "TELEGRAM_HOME_CHANNEL_NAME",
        "DISCORD_HOME_CHANNEL",
        "DISCORD_HOME_CHANNEL_NAME",
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_AUTO_THREAD",
        "SLACK_HOME_CHANNEL",
        "SLACK_HOME_CHANNEL_NAME",
        "SLACK_ALLOWED_USERS",
        "WHATSAPP_ENABLED",
        "WHATSAPP_MODE",
        "WHATSAPP_ALLOWED_USERS",
        "SIGNAL_HTTP_URL",
        "SIGNAL_ACCOUNT",
        "SIGNAL_ALLOWED_USERS",
        "SIGNAL_GROUP_ALLOWED_USERS",
        "SIGNAL_HOME_CHANNEL",
        "SIGNAL_HOME_CHANNEL_NAME",
        "SIGNAL_IGNORE_STORIES",
        "HASS_TOKEN",
        "HASS_URL",
        "EMAIL_ADDRESS",
        "EMAIL_PASSWORD",
        "EMAIL_IMAP_HOST",
        "EMAIL_SMTP_HOST",
        "EMAIL_HOME_ADDRESS",
        "EMAIL_HOME_ADDRESS_NAME",
        "SON_OF_ANTON_DASHBOARD_SESSION_TOKEN",
        "GATEWAY_ALLOWED_USERS",
        "GH_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_INSTALLATION_ID",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DAYTONA_API_KEY",
        "GATEWAY_RELAY_ID",
        "GATEWAY_RELAY_SECRET",
        "GATEWAY_RELAY_DELIVERY_KEY",
        "VERCEL_OIDC_TOKEN",
        "VERCEL_TOKEN",
        "VERCEL_PROJECT_ID",
        "VERCEL_TEAM_ID",
    })
    # CLAUDE_CODE_OAUTH_TOKEN is deliberately NOT stripped.  It is set and
    # owned by the user's Claude Code install (subscription OAuth), not a
    # Son of Anton-managed inference credential — Claude subscription auth is not a
    # working Son of Anton provider path.  Stripping it broke agent-spawned
    # ``claude`` CLIs: the child fell through to the shared macOS Keychain /
    # ``~/.claude/.credentials.json`` store and, on auth failure, cleared it,
    # logging the user out of their interactive Claude sessions (#55878).
    # It arrives via the registry loop above (anthropic api_key_env_vars),
    # so remove it explicitly.
    blocked.discard("CLAUDE_CODE_OAUTH_TOKEN")
    return frozenset(blocked)


_SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST = _build_provider_env_blocklist()

# Active-virtualenv markers that must NOT leak into terminal subprocesses.
# The gateway runs inside its own venv, so its process environment carries
# VIRTUAL_ENV (and possibly CONDA_PREFIX). If those leak into commands the
# agent runs against OTHER Python projects, tools like ``uv``/``poetry`` treat
# the inherited value as the active environment and build/sync that other
# project's dependencies into the Son of Anton venv path instead of the project's own
# ``.venv`` — silently clobbering the Son of Anton environment (e.g. a project pinned
# to a different Python version overwrites it and breaks the gateway). The
# Son of Anton venv stays reachable via PATH (its bin dir is first), so stripping
# these markers is safe and only prevents the cross-project clobber (#23473).
#
# PYTHONHOME is included because a gateway-inherited value redirects the
# standard-library search of ANY child interpreter — including unrelated
# system/venv Pythons — to the Son of Anton venv's stdlib, which crashes with
# version-mismatch errors before a child script even imports a package
# (#75018). Son of Anton itself treats PYTHONHOME as contamination in its own
# child processes (managed_uv.py, sqlite_runtime.py), so stripping it from
# subprocess envs is consistent. Users who need PYTHONHOME for a specific
# child can set it explicitly in the command.
#
# PYTHONPATH is NOT included here — it's handled by
# _strip_son_of_anton_owned_pythonpath() which removes only Son of Anton-owned entries,
# preserving user-set paths.
_ACTIVE_VENV_MARKER_VARS = ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME")


def _is_son_of_anton_internal_secret(key: str) -> bool:
    """Return True for Son of Anton-internal secrets injected under *dynamic* names.

    ``_SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST`` is name-based and derived from the
    provider/tool registries, but the gateway and CLI also inject secrets into
    ``os.environ`` at runtime under names no static registry knows about:

    - ``AUXILIARY_<TASK>_API_KEY`` / ``AUXILIARY_<TASK>_BASE_URL`` — per-task
      side-LLM credentials bridged from ``config.yaml[auxiliary]`` by
      ``gateway/run.py`` and ``cli.py`` (vision, web_extract, approval,
      compression, and any plugin-registered auxiliary task). These are
      separate, often higher-spend API keys plus base URLs that may point at
      private endpoints; a model-authored shell command must never see them.
    - ``GATEWAY_RELAY_*_SECRET`` / ``GATEWAY_RELAY_*_KEY`` /
      ``GATEWAY_RELAY_*_TOKEN`` — relay-auth material provisioned by the
      gateway (``GATEWAY_RELAY_SECRET``, ``GATEWAY_RELAY_DELIVERY_KEY``).
      These are Tier-1 gateway secrets, like the messaging bot tokens in
      ``_ALWAYS_STRIP_KEYS``. Non-secret ``GATEWAY_RELAY_*`` routing hints
      (``GATEWAY_RELAY_URL``, ``GATEWAY_RELAY_PLATFORMS``, …) are NOT matched
      and remain visible.

    ``code_execution_tool.py`` already catches these via substring matching on
    ``KEY`` / ``SECRET`` / ``TOKEN``; the terminal backend's narrower name-based
    blocklist did not, which is the leak this predicate closes.

    This is the single source of truth for "Son of Anton-internal dynamic secret"
    across every spawn path — the terminal ``_make_run_env`` /
    ``_sanitize_subprocess_env`` filters, the Docker passthrough filter, and the
    non-terminal :func:`son_of_anton_subprocess_env` helper all call it, so the
    dynamic patterns are stripped **unconditionally** regardless of
    ``env_passthrough`` skill registration or ``inherit_credentials``. Nothing
    a model-driving CLI legitimately needs matches these patterns.
    """
    upper = key.upper()
    if upper.startswith("AUXILIARY_") and (
        upper.endswith("_API_KEY") or upper.endswith("_BASE_URL")
    ):
        return True
    if upper.startswith("GATEWAY_RELAY_") and (
        upper.endswith("_SECRET") or upper.endswith("_KEY") or upper.endswith("_TOKEN")
    ):
        return True
    return False


def _inject_context_son_of_anton_home(env: dict) -> None:
    """Bridge the context-local Son of Anton home override into subprocess env."""
    try:
        from son_of_anton_constants import get_son_of_anton_home_override

        value = get_son_of_anton_home_override()
        if value:
            env["SON_OF_ANTON_HOME"] = value
    except Exception:
        pass


def _inject_session_context_env(env: dict) -> None:
    """Bridge gateway session ContextVars into a subprocess environment dict.

    ContextVars don't propagate to child processes, so the live session vars
    (SON_OF_ANTON_SESSION_*) are bridged onto the child env here.

    🔴 Cross-session leak guard. The session vars also have a process-global
    os.environ mirror (written last-writer-wins as a CLI/cron fallback, never
    cleared). Under a concurrent multi-session host (the messaging gateway, ACP
    adapter, API server, TUI) that global belongs to *whichever turn wrote it
    last* — NOT necessarily this task. A subprocess spawned from a task whose
    ContextVar is _UNSET (e.g. a sibling message task that never bound, or one
    that inherited another session's context) would otherwise inherit the
    FOREIGN global and act on another session's identity.

    So once the session-context machinery is engaged in this process (any host
    has called set_session_vars), the session vars are ContextVar-authoritative:
    - ContextVar set (incl. explicitly-empty "") → that value wins, overriding
      any stale snapshot/global value.
    - ContextVar _UNSET → STRIP the var from the child env rather than inherit
      the possibly-foreign process-global.
    In a pure single-process CLI/one-shot that never engaged the session-context
    system there is no concurrency to leak across, so the inherited fallback is
    kept. See gateway/session_context.session_context_engaged and
    tests/tools/test_local_env_session_leak.py.
    """
    try:
        from gateway.session_context import (
            _UNSET,
            _VAR_MAP,
            session_context_engaged,
        )
    except Exception:
        return

    _engaged = session_context_engaged()
    for var_name, var in _VAR_MAP.items():
        value = var.get()
        if value is not _UNSET:
            # Explicitly bound (including "") — authoritative for this task.
            env[var_name] = "" if value is None else str(value)
        elif _engaged:
            # Unset for THIS task while a concurrent host is engaged: drop any
            # inherited global so a sibling session's value can't leak in.
            env.pop(var_name, None)


def _sanitize_subprocess_env(base_env: dict | None, extra_env: dict | None = None) -> dict:
    """Filter Son of Anton-managed secrets from a subprocess environment."""
    try:
        from tools.env_passthrough import (
            is_env_passthrough as _is_passthrough,
            resolve_passthrough_value as _resolve_passthrough_value,
        )
    except Exception:
        _is_passthrough = lambda _: False  # noqa: E731
        _resolve_passthrough_value = lambda _name, fallback: fallback  # noqa: E731

    sanitized: dict[str, str] = {}

    for key, value in (base_env or {}).items():
        if key.startswith(_SON_OF_ANTON_PROVIDER_ENV_FORCE_PREFIX):
            continue
        if _is_son_of_anton_internal_secret(key):
            continue
        passthrough = _is_passthrough(key)
        if key in _SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST and not passthrough:
            continue
        resolved = _resolve_passthrough_value(key, value) if passthrough else value
        if resolved is not None:
            sanitized[key] = resolved

    for key, value in (extra_env or {}).items():
        if key.startswith(_SON_OF_ANTON_PROVIDER_ENV_FORCE_PREFIX):
            real_key = key[len(_SON_OF_ANTON_PROVIDER_ENV_FORCE_PREFIX):]
            if _is_son_of_anton_internal_secret(real_key):
                continue
            sanitized[real_key] = value
        elif _is_son_of_anton_internal_secret(key):
            continue
        else:
            passthrough = _is_passthrough(key)
            if key in _SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST and not passthrough:
                continue
            resolved = _resolve_passthrough_value(key, value) if passthrough else value
            if resolved is not None:
                sanitized[key] = resolved

    _inject_context_son_of_anton_home(sanitized)

    from son_of_anton_constants import apply_subprocess_home_env
    apply_subprocess_home_env(sanitized)

    # Same cross-session leak guard as _make_run_env, for the background/PTY
    # spawn path (process_registry.spawn_local builds env via this function).
    _inject_session_context_env(sanitized)

    # Filter PYTHONPATH before removing VIRTUAL_ENV, then strip the runtime
    # marker vars.  PYTHONPATH filtering must run first so the runtime marker
    # check can still prove ownership against the repo layout.
    _strip_son_of_anton_owned_pythonpath_and_runtime_markers(sanitized)


    return sanitized


# Tier-1 secrets: stripped from EVERY spawned subprocess unconditionally —
# even when the caller opts into credential inheritance for a model-driving
# CLI (claude / codex / gemini).  These are not LLM provider credentials; no
# legitimate child Son of Anton spawns needs them, and they are the highest-value
# secrets to keep out of a compromised dependency's reach (gateway bot tokens,
# GitHub auth, remote-compute tokens, dashboard session secret).  The set is a
# narrow subset of _SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST; provider keys are handled by
# the conditional Tier-2 strip in son_of_anton_subprocess_env().
_ALWAYS_STRIP_KEYS: frozenset[str] = frozenset({
    # GitHub auth
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_INSTALLATION_ID",
    # Gateway / messaging bot tokens and access control
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
    # Gateway relay auth — the ID/secret/delivery-key triplet the gateway
    # provisions and persists to the 0600 .env. Stripped unconditionally on
    # EVERY spawn surface (terminal + model-driving CLIs) so it can't drift
    # between paths: _SECRET / _DELIVERY_KEY are also matched by
    # _is_son_of_anton_internal_secret, but _ID has no secret suffix, so it must be
    # enumerated here to stay stripped on the inherit_credentials=True path
    # (codex / copilot), which skips the Tier-2 blocklist.
    "GATEWAY_RELAY_ID",
    "GATEWAY_RELAY_SECRET",
    "GATEWAY_RELAY_DELIVERY_KEY",
    "HASS_TOKEN",
    "EMAIL_PASSWORD",
    "SON_OF_ANTON_DASHBOARD_SESSION_TOKEN",
    # Remote-compute / infrastructure secrets
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "DAYTONA_API_KEY",
})


def son_of_anton_subprocess_env(*, inherit_credentials: bool = False) -> dict[str, str]:
    """Build a sanitized environment dict for a spawned subprocess.

    Centralized helper for the **non-terminal** spawn surface (browser,
    ACP/CLI executors, computer-use driver, dep-ensure, TUI Node host,
    detached gateway).  Use this instead of copying ``os.environ`` directly
    so strip-by-default is the uniform policy across every spawn site, with a
    single source of truth (``_SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST``).  The terminal
    / execute_code path keeps using :func:`_sanitize_subprocess_env`, which is
    skill-aware (``env_passthrough``); this helper is for spawns that have no
    skill-passthrough concept.

    Two-tier stripping:

    * **Tier 1 (always):** ``_ALWAYS_STRIP_KEYS`` — gateway bot tokens, GitHub
      auth, and remote-compute secrets are removed regardless of
      ``inherit_credentials``.  No child Son of Anton spawns legitimately needs them.
    * **Tier 2 (conditional):** the rest of ``_SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST``
      (LLM provider API keys, tool secrets) is removed unless the caller passes
      ``inherit_credentials=True``.

    Pass ``inherit_credentials=True`` **only** when the child legitimately
    needs LLM provider credentials — a user-blessed ``claude`` / ``codex`` /
    ``gemini`` CLI executor, or the TUI Node host that makes model calls.  The
    flag is grep-able for audit: ``grep -rn 'inherit_credentials=True'`` lists
    every spawn site that still receives provider credentials.

    Callers that need a *specific* non-provider secret (e.g. the browser worker
    needs ``BROWSERBASE_API_KEY`` / ``FIRECRAWL_API_KEY``) should call with
    ``inherit_credentials=False`` and copy just those keys back from
    ``os.environ`` into the returned dict.
    """
    env = os.environ.copy()

    # Tier 1 — always strip.
    for key in _ALWAYS_STRIP_KEYS:
        env.pop(key, None)
    # Internal routing hints and Son of Anton-internal dynamic secrets
    # (``AUXILIARY_<TASK>_API_KEY`` / ``_BASE_URL`` side-LLM credentials,
    # ``GATEWAY_RELAY_*`` relay-auth material) must never reach a child,
    # regardless of ``inherit_credentials`` — a model-driving CLI has no
    # legitimate use for them. See :func:`_is_son_of_anton_internal_secret`.
    for key in list(env):
        if key.startswith(_SON_OF_ANTON_PROVIDER_ENV_FORCE_PREFIX):
            env.pop(key, None)
        elif _is_son_of_anton_internal_secret(key):
            env.pop(key, None)

    if not inherit_credentials:
        # Tier 2 — strip provider/tool credentials unless explicitly inherited.
        for key in _SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST:
            env.pop(key, None)

    # Force UTF-8 mode for spawned child Pythons so encoding mismatches can't
    # silently corrupt tool output (#31420).
    env.setdefault("PYTHONUTF8", "1")

    _inject_context_son_of_anton_home(env)
    from son_of_anton_constants import apply_subprocess_home_env
    apply_subprocess_home_env(env)

    _strip_son_of_anton_owned_pythonpath_and_runtime_markers(env)

    # Cross-session leak guard, same as the terminal spawn paths: this helper
    # copies os.environ, whose SON_OF_ANTON_SESSION_* mirror is a last-writer-wins
    # global under a concurrent multi-session host. A caller that re-binds the
    # session identity explicitly (slash_worker/ACP via --session-key argv) is
    # unaffected — bound ContextVars win here — but a caller that spawns without
    # re-binding (e.g. tui_gateway cli.exec) would otherwise inherit a FOREIGN
    # session's identity. Strip _UNSET session vars when engaged so that can't
    # happen; single uniform policy across every spawn surface.
    _inject_session_context_env(env)

    # Non-terminal subprocess helpers (browser, lazy-deps, TUI/ACP hosts, etc.)
    # also need the delegate_task child lineage marker.  Otherwise a child
    # context that later imports Kanban DB code in the spawned process would
    # still see the parent's SON_OF_ANTON_HOME but lose the DB mutation guard.

    return env


def build_subprocess_env(
    base: "Mapping[str, str] | None" = None,
    *,
    inherit_profile_home: bool = True,
    scrub_secrets: bool = True,
    extra: "Mapping[str, str] | None" = None,
) -> dict[str, str]:
    """Single factory for building a child-process environment.

    Every spawn site in the codebase should build its env through this
    function (or :func:`son_of_anton_subprocess_env` for the model-driving-CLI
    surface) instead of copying ``os.environ`` directly, so profile-home
    propagation (``SON_OF_ANTON_HOME`` / subprocess ``HOME`` contract) and the
    Son of Anton secret-scrub policy have a single owner.  History: ~11 separate
    commits each fixed one more spawn site that missed profile-HOME or
    secret-scrub propagation; this factory is the fix for the class.

    Parameters:

    * ``base`` — starting environment.  ``None`` (default) snapshots
      ``os.environ``.  Pass an explicit mapping to build on a caller-prepared
      env instead.
    * ``scrub_secrets=True`` (default) — delegate to
      :func:`_sanitize_subprocess_env`, the long-standing owner of the scrub
      list (provider blocklist + ``_is_son_of_anton_internal_secret`` dynamic
      patterns + kanban/venv-marker/session-context guards) **and** of
      ``SON_OF_ANTON_HOME`` / subprocess-HOME propagation.  On this path profile
      home propagation is inherent — ``inherit_profile_home`` is ignored
      (always applied), exactly matching today's sanitize semantics.
    * ``scrub_secrets=False`` — preserve the base env content byte-for-byte
      (no key is removed).  Use for children that intentionally receive
      secrets (git credential flows, ``bws``/``op`` secret CLIs) or where
      scrubbing could change behavior.  The site is still a win: it becomes
      grep-able and future-fixable.
    * ``inherit_profile_home`` — on the non-scrub path, when True, bridge the
      context-local Son of Anton home override into ``SON_OF_ANTON_HOME`` and apply the
      subprocess HOME contract (``son_of_anton_constants.apply_subprocess_home_env``).
      Pass False to keep the inherited env untouched (exact legacy
      ``os.environ.copy()`` behavior).
    * ``extra`` — applied **last** on the non-scrub path so explicit caller
      overrides (e.g. a session-scoped ``SON_OF_ANTON_HOME``) always win.  On the
      scrub path it is forwarded as ``_sanitize_subprocess_env``'s
      ``extra_env`` (same force-prefix / blocklist handling as today).
    """
    if scrub_secrets:
        # _sanitize_subprocess_env already performs SON_OF_ANTON_HOME override
        # bridging + apply_subprocess_home_env unconditionally; delegating
        # wholesale keeps one owner and zero drift.
        return _sanitize_subprocess_env(
            dict(base) if base is not None else os.environ.copy(),
            dict(extra) if extra else None,
        )

    env: dict[str, str] = dict(base) if base is not None else os.environ.copy()
    if inherit_profile_home:
        _inject_context_son_of_anton_home(env)
        from son_of_anton_constants import apply_subprocess_home_env
        apply_subprocess_home_env(env)
    if extra:
        env.update(extra)
    return env


def _find_bash() -> str:
    """Find bash for command execution."""
    return (
        shutil.which("bash")
        or ("/usr/bin/bash" if os.path.isfile("/usr/bin/bash") else None)
        or ("/bin/bash" if os.path.isfile("/bin/bash") else None)
        or os.environ.get("SHELL")
        or "/bin/sh"
    )


# POSIX-sh-family shells that understand the ``[shell, "-lic", "set +m; …"]``
# invocation spawn_local uses. $SHELL values outside this set (fish, csh/tcsh,
# nushell, elvish, xonsh, …) would error on that syntax, so _find_shell falls
# back to bash for them rather than honouring $SHELL. (#42203)
_SPAWN_COMPATIBLE_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "ksh", "mksh"})


def _find_shell() -> str:
    """Find the user's login shell for background process spawning.

    Unlike ``_find_bash`` (which always returns a bash binary for callers
    that explicitly need bash), this function prefers the user's configured
    ``$SHELL`` so that ``spawn_local`` uses the shell the user actually logs
    in with.

    On macOS Catalina+ the default login shell is zsh, but
    ``shutil.which("bash")`` still finds the system ``/bin/bash`` (GNU bash
    3.2).  When bash 3.2 is invoked with ``-l`` (login) and stdin is
    ``/dev/null``, it sources ``~/.bash_profile`` which on many macOS setups
    contains ``exec /bin/zsh -l``.  That ``exec`` replaces bash with zsh but
    drops the ``-c`` argument, so the background command never runs — the
    subprocess exits 0 with no output and no side effects.

    Preferring ``$SHELL`` (when it is a POSIX-``sh``-family shell) avoids this
    because zsh/bash/sh/dash/ksh handle ``-lic`` correctly even with
    redirected stdin.

    Only POSIX-sh-family shells are honoured: ``spawn_local`` invokes the
    shell as ``[shell, "-lic", "set +m; <cmd>"]``, and that ``-lic`` bundle +
    ``set +m`` job-control syntax is NOT understood by fish, csh/tcsh,
    nushell, elvish, xonsh, etc.  Returning such a ``$SHELL`` would trade the
    bash-3.2 swallow for a parse error on every background command, so for any
    non-allowlisted shell we fall back to ``_find_bash`` (the prior behaviour).
    """
    user_shell = os.environ.get("SHELL")
    if (
        user_shell
        and os.path.isfile(user_shell)
        and os.access(user_shell, os.X_OK)
        and Path(user_shell).name in _SPAWN_COMPATIBLE_SHELLS
    ):
        return user_shell
    return _find_bash()


# Standard PATH entries for environments with minimal PATH.
_SANE_PATH = (
    "/opt/homebrew/bin:/opt/homebrew/sbin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

# Cached directory containing the ``son-of-anton`` console-script.
# ``_SENTINEL`` distinguishes "not resolved yet" from a resolved ``None``.
_SENTINEL = object()
_SON_OF_ANTON_BIN_DIR: "str | None | object" = _SENTINEL


def _resolve_son_of_anton_bin_dir() -> str | None:
    """Return the directory holding the ``son-of-anton`` console-script, or None.

    The terminal tool runs in a freshly-spawned subshell whose PATH is the
    agent process's PATH plus a static set of system dirs (``_SANE_PATH``).
    When the gateway is launched by something that does NOT source the user's
    shell rc — systemd, a service manager, a desktop launcher, cron — the
    son-of-anton install dir (``~/.local/bin``, the venv ``bin``/``Scripts``, pipx,
    nix) is absent from that PATH, so plugins shelling out to bare ``son-of-anton``
    via the terminal tool hit ``command not found`` (exit 127) even though
    ``son-of-anton`` works fine in the user's own interactive terminal.

    We resolve the install dir once (it never changes within a process) and
    prepend-if-missing it to the subshell PATH so bare ``son-of-anton`` resolves
    regardless of how the gateway was started.

    Resolution order (cheap, no heavy imports):
      1. ``shutil.which("son-of-anton")`` — normal PATH-installed shim.
      2. The directory of ``sys.argv[0]`` when it's an absolute path to a
         real ``son-of-anton`` executable (covers nix-store / venv wrappers).
      3. The directory of ``sys.executable`` — the running interpreter's
         venv ``bin``/``Scripts`` is where its console-scripts live.
    """
    global _SON_OF_ANTON_BIN_DIR
    if _SON_OF_ANTON_BIN_DIR is not _SENTINEL:
        return _SON_OF_ANTON_BIN_DIR  # type: ignore[return-value]

    candidate: str | None = None

    which = shutil.which("son-of-anton")
    if which:
        candidate = os.path.dirname(which)

    if candidate is None:
        argv0 = sys.argv[0] if sys.argv else ""
        base = os.path.basename(argv0).lower()
        if (
            os.path.isabs(argv0)
            and (base == "son-of-anton" or base.startswith("son-of-anton."))
            and os.path.isfile(argv0)
        ):
            candidate = os.path.dirname(argv0)

    if candidate is None:
        exe_dir = os.path.dirname(sys.executable) if sys.executable else ""
        if exe_dir:
            shim = "son-of-anton"
            if os.path.isfile(os.path.join(exe_dir, shim)):
                candidate = exe_dir

    if candidate and not os.path.isdir(candidate):
        candidate = None

    _SON_OF_ANTON_BIN_DIR = candidate
    return candidate


def _prepend_son_of_anton_bin_dir(existing_path: str) -> str:
    """Prepend the son-of-anton install dir to ``existing_path`` if it's missing.

    Cross-platform (uses ``os.pathsep``). First-occurrence wins, so a PATH
    that already contains the dir is returned unchanged. Returns the input
    unchanged when the install dir can't be resolved.
    """
    bin_dir = _resolve_son_of_anton_bin_dir()
    if not bin_dir:
        return existing_path
    sep = os.pathsep
    entries = [e for e in existing_path.split(sep) if e] if existing_path else []
    if bin_dir in entries:
        return existing_path
    return sep.join([bin_dir, *entries])


def _managed_runtime_path_entries() -> list[str]:
    """Return existing Son of Anton-managed runtime dirs for the terminal subshell PATH.

    The terminal tool spawns a subshell whose PATH is the agent process's PATH
    plus ``_SANE_PATH``. Neither carries the runtimes Son of Anton installs for
    itself, so on a machine where Son of Anton provisioned its own toolchain a
    command the agent runs resolves a system copy instead — or nothing at all:

    - ``$SON_OF_ANTON_HOME/node`` (+ ``/bin``) — installed to satisfy the desktop and
      browser toolchain. ``tools/browser_tool.py`` already does this for its own
      subprocesses; the agent's shell deserves the same.
    - ``$SON_OF_ANTON_HOME/bin`` — the managed ``uv``. ``install.sh`` writes it there
      and nothing has ever put that directory on PATH, so an install whose only
      uv is the managed one looks uv-less to both the agent and the model.

    Resolved per call rather than cached in a module constant because
    ``get_son_of_anton_home()`` is profile-scoped and a managed tree can appear
    mid-process (``heal_son_of_anton_managed_node``, a first browser install).
    """
    try:
        from son_of_anton_constants import get_son_of_anton_home, iter_son_of_anton_node_dirs

        candidates = [*iter_son_of_anton_node_dirs(), get_son_of_anton_home() / "bin"]
        return [str(d) for d in candidates if d.is_dir()]
    except Exception:
        return []


def _append_missing_sane_path_entries(existing_path: str) -> str:
    """Return a normalised POSIX PATH with missing sane entries appended.

    On POSIX the caller-supplied PATH is rewritten (not merely appended to):
    empty entries and duplicate entries are dropped, preserving
    first-occurrence order, then each missing ``_SANE_PATH`` entry is appended
    once at the end so existing entries keep their precedence.

    Two intentional normalisations beyond the bare "add Homebrew dirs" fix:

    - **Empty entries are stripped.** A leading/trailing/double ``:`` encodes
      an empty PATH element, which POSIX shells interpret as the current
      working directory — a mild foot-gun in a default terminal environment.
      We drop these rather than carry them through.
    - **Duplicates are collapsed** (first occurrence wins), so a caller PATH
      that already contains repeats is not propagated verbatim.

    Son of Anton-managed runtime dirs are appended alongside the sane entries, not
    prepended: a tool the user deliberately put on their own PATH still wins,
    and the managed one only fills the gap where there would otherwise be
    nothing.

    For a well-formed PATH (no empties, no duplicates) the leading segment is
    byte-identical to the input and ordering is preserved; only the missing
    sane entries are appended.
    """
    sane_entries = [entry for entry in _SANE_PATH.split(":") if entry]
    sane_entries.extend(
        entry for entry in _managed_runtime_path_entries() if entry not in sane_entries
    )
    if not existing_path:
        return ":".join(sane_entries)

    # De-duplicate the caller PATH (first occurrence wins) and drop empty
    # entries before merging in the sane fallbacks.
    seen: set[str] = set()
    ordered_entries: list[str] = []
    for entry in existing_path.split(":"):
        if not entry or entry in seen:
            continue
        seen.add(entry)
        ordered_entries.append(entry)

    # _SANE_PATH is a static, duplicate-free constant, so a membership check
    # against the caller entries is sufficient — no need to track `seen` here.
    for entry in sane_entries:
        if entry not in seen:
            ordered_entries.append(entry)

    return ":".join(ordered_entries)


def _make_run_env(env: dict, cwd: str | None = None) -> dict:
    """Build a run environment with a sane PATH and provider-var stripping."""
    try:
        from tools.env_passthrough import (
            is_env_passthrough as _is_passthrough,
            resolve_passthrough_value as _resolve_passthrough_value,
        )
    except Exception:
        _is_passthrough = lambda _: False  # noqa: E731
        _resolve_passthrough_value = lambda _name, fallback: fallback  # noqa: E731

    merged = dict(os.environ | env)
    run_env = {}
    for k, v in merged.items():
        if k.startswith(_SON_OF_ANTON_PROVIDER_ENV_FORCE_PREFIX):
            real_key = k[len(_SON_OF_ANTON_PROVIDER_ENV_FORCE_PREFIX):]
            if _is_son_of_anton_internal_secret(real_key):
                continue
            run_env[real_key] = v
        elif _is_son_of_anton_internal_secret(k):
            continue
        else:
            passthrough = _is_passthrough(k)
            if k in _SON_OF_ANTON_PROVIDER_ENV_BLOCKLIST and not passthrough:
                continue
            value = _resolve_passthrough_value(k, v) if passthrough else v
            if value is not None:
                run_env[k] = value
    path_key = "PATH"
    new_path = _append_missing_sane_path_entries(run_env.get(path_key, ""))
    # Ensure the son-of-anton install dir is reachable so plugins can shell out
    # to bare ``son-of-anton`` via the terminal tool even when the gateway was
    # launched without it on PATH (systemd, service managers, cron, etc.).
    run_env[path_key] = _prepend_son_of_anton_bin_dir(new_path)

    _inject_context_son_of_anton_home(run_env)

    from son_of_anton_constants import apply_subprocess_home_env
    apply_subprocess_home_env(run_env)

    # Bridge ContextVar-based session vars into the subprocess env (with the
    # cross-session leak guard — strips _UNSET vars when a concurrent host is
    # engaged so a sibling session's os.environ mirror can't leak in).
    _inject_session_context_env(run_env)

    _strip_son_of_anton_owned_pythonpath_and_runtime_markers(run_env)


    if cwd:
        # Marker for terminal.home_mode=cwd: get_subprocess_home() uses this
        # to point HOME at the command's working directory (gateway profiles
        # whose terminal.cwd is the user's home).
        run_env["SON_OF_ANTON_SUBPROCESS_CWD"] = str(cwd)

    return run_env


def _same_path(left: Path, right: Path) -> bool:
    """Compare path spellings with host filesystem case semantics."""
    left_parts = [os.path.normcase(part) for part in left.parts]
    right_parts = [os.path.normcase(part) for part in right.parts]
    return left_parts == right_parts


def _build_son_of_anton_repo_root_aliases(
    resolved_root: Path,
    lexical_root: Path,
    configured_home: Path,
) -> tuple[Path, ...]:
    """Return exact repo-root spellings emitted by Son of Anton launchers.

    Launchers can map a physical path under the resolved SON_OF_ANTON_HOME back
    onto the configured SON_OF_ANTON_HOME spelling. Mirror that producer
    contract here so a symlink/junction-backed install is matched without
    treating arbitrary descendants of SON_OF_ANTON_HOME as Son of Anton-owned.
    Additionally, when the repo itself is a junction/symlink under the
    configured root, the single deterministic candidate <root>/<repo dirname>
    is accepted only when strict resolve proves it is the exact physical repo
    root.
    """
    aliases: list[Path] = []

    def add(candidate: Path) -> None:
        if not any(_same_path(candidate, existing) for existing in aliases):
            aliases.append(candidate)

    add(resolved_root)
    add(lexical_root)

    # Profile re-home: with --profile / sticky active_profile the configured
    # home becomes <root>/profiles/<name>.  The repo root then lives beside
    # the profiles directory (not under the profile home), so the home-
    # relative mapping below cannot reach it.  Derive the root spelling
    # lexically the same way get_default_son_of_anton_root() does (parent of a
    # "profiles" component) and run the same exact-ownership mapping against
    # it -- this recovers the launcher's lexical root under profile re-home
    # while still never matching arbitrary descendants of SON_OF_ANTON_HOME.
    home_candidates = [configured_home]
    if configured_home.parent.name == "profiles":
        home_candidates.append(configured_home.parent.parent)

    for home in home_candidates:
        try:
            resolved_home = home.resolve()
            home_key = os.path.normcase(str(resolved_home))
            root_key = os.path.normcase(str(resolved_root))
            if os.path.commonpath([home_key, root_key]) == home_key:
                relative_root = os.path.relpath(str(resolved_root), str(resolved_home))
                add(home / relative_root)
        except (OSError, ValueError):
            pass

    # Repo-level junction recovery: the repository itself may be a
    # junction/symlink under the configured root (e.g. a symlinked checkout)
    # while the import spelling (editable install) resolves to the physical
    # location.  The home-relative mapping above cannot express a cross-root
    # link (commonpath raises on disjoint roots), so prove the EXACT
    # filesystem identity of the single deterministic candidate -- <lexical
    # root>/<repo dirname> -- with a strict resolve before accepting it as
    # Son of Anton-owned.  Fail-closed: a
    # missing path (strict resolve raises), a real directory that is not the
    # known physical root, or any unrelated spelling never becomes an alias.
    for home in home_candidates:
        repo_candidate = home / resolved_root.name
        try:
            if repo_candidate.resolve(strict=True) == resolved_root.resolve(strict=True):
                add(repo_candidate)
        except OSError:
            pass

    return tuple(aliases)


# --- Son of Anton venv / repo-root detection (module-level, computed once) ---

#: The Son of Anton repository root - three levels up from this file
#: (``tools/environments/local.py`` -> ``tools/environments`` -> ``tools``
#: -> repo root).  This is the directory the Electron app prepends to
#: PYTHONPATH so the backend can do ``import tools``, ``import son_of_anton_cli``,
#: etc.  Subprocesses that are NOT the Son of Anton backend don't need it and it
#: can shadow local packages.
_son_of_anton_repo_root: Path = Path(__file__).resolve().parents[2]

#: Alternate spellings of the repo root that Son of Anton launchers may emit.
#: ``Path(__file__).resolve()`` canonicalizes symlinks/junctions, but launchers
#: can render Son of Anton-owned paths under the configured SON_OF_ANTON_HOME
#: spelling. ``Path(__file__)`` (unresolved) keeps that spelling, so a PYTHONPATH
#: entry written by the launcher still matches even though it differs
#: lexically from the resolved root.
_son_of_anton_repo_root_aliases: tuple[Path, ...] = _build_son_of_anton_repo_root_aliases(
    _son_of_anton_repo_root,
    Path(__file__).absolute().parents[2],
    get_process_son_of_anton_home(),
)

#: Whether the current interpreter is running inside a venv.  On Python 3.3+
#: ``sys.base_prefix != sys.prefix`` indicates a venv (or virtualenv).
#: ``sys.real_prefix`` is the old virtualenv (<20) marker.
_in_venv: bool = (
    getattr(sys, "base_prefix", sys.prefix) != sys.prefix
    or hasattr(sys, "real_prefix")
)

#: Cached set of site-packages directories that belong to the running
#: interpreter's own venv.  Computed lazily (once) because ``site`` import
#: and path construction are not free and this function is called on every
#: subprocess spawn.
_son_of_anton_site_packages: list[Path] | None = None


def _validated_runtime_venv(env: dict) -> Path | None:
    """Return a producer-owned runtime venv identified by VIRTUAL_ENV.

    A user may carry an unrelated VIRTUAL_ENV, so the variable alone is not
    provenance.  Require the exact ``<Son of Anton repo>/venv`` layout and a real
    venv marker before accepting its separate runtime venv.
    """
    value = env.get("VIRTUAL_ENV")
    if not value:
        return None

    candidate = Path(value)
    if not any(_same_path(candidate, repo_root / "venv") for repo_root in _son_of_anton_repo_root_aliases):
        return None

    try:
        if not (candidate / "pyvenv.cfg").is_file():
            return None
    except OSError:
        return None

    return candidate


def _get_son_of_anton_site_packages(env: dict) -> list[Path]:
    """Return exact site-packages dirs owned by the Son of Anton runtime.

    Uses ``site.getsitepackages()`` when available for robustness (it respects
    ``.pth`` rewrites and platform conventions), with a manual fallback that
    constructs the canonical path from ``sys.prefix`` for POSIX layouts.
    """
    global _son_of_anton_site_packages
    if _son_of_anton_site_packages is not None:
        result = list(_son_of_anton_site_packages)
    else:
        result = []
        if _in_venv:
            try:
                import site
                for sp in site.getsitepackages():
                    result.append(Path(sp))
            except Exception:
                pass

            # Fallback: construct manually.
            #   sys.prefix / lib / python{X.Y} / site-packages
            if not result:
                pyver = f"python{sys.version_info[0]}.{sys.version_info[1]}"
                result.append(Path(sys.prefix) / "lib" / pyver / "site-packages")

        _son_of_anton_site_packages = list(result)

    runtime_venv = _validated_runtime_venv(env)
    if runtime_venv is not None:
        runtime_site_packages = runtime_venv / "Lib" / "site-packages"
        if not any(_same_path(runtime_site_packages, existing) for existing in result):
            result.append(runtime_site_packages)

    return result


def _strip_son_of_anton_owned_pythonpath_and_runtime_markers(env: dict) -> None:
    """Strip Son of Anton-owned PYTHONPATH entries, then the runtime marker vars.

    Ordering is load-bearing: PYTHONPATH filtering must run BEFORE the
    markers are removed so a validated runtime venv (VIRTUAL_ENV ->
    <repo>/venv) can still prove ownership.
    """
    _strip_son_of_anton_owned_pythonpath(env)
    for _marker in _ACTIVE_VENV_MARKER_VARS:
        env.pop(_marker, None)


def _strip_son_of_anton_owned_pythonpath(env: dict) -> None:
    """Remove Son of Anton-owned PYTHONPATH entries from subprocess environments.

    Launchers prepend the Son of Anton repo root and the Son of Anton venv's
    site-packages so the backend can ``import tools``; leaking those into a
    child Python of a DIFFERENT version makes it load the backend's C
    extensions and crash (``numpy._core._multiarray_umath``, ``PIL._imaging``,
    ``cryptography``).  Blanket-removing PYTHONPATH would discard legitimate
    user entries, so only entries proven Son of Anton-owned are removed:

    1. The exact repo root (never direct children -- no launcher injects
       one, and user paths under the repo must survive).
    2. The exact runtime site-packages dirs (running interpreter's venv or
       a validated runtime venv; descendants are user paths).

    Everything else -- user libs, Nix plugin paths, a pythonX.Y/site-packages
    entry meant for a DIFFERENT child version -- is preserved byte-for-byte:
    ownership is decided by path provenance, never by a cross-version
    heuristic (#74817 follow-up).
    """
    pp = env.get("PYTHONPATH")
    if not pp:
        return

    son_of_anton_site_packages = _get_son_of_anton_site_packages(env)

    kept: list[str] = []
    stripped: list[str] = []

    for entry in pp.split(os.pathsep):
        # Empty and non-normalized components are user-owned semantics.  In
        # particular, an empty component means the current working directory.
        # Preserve raw spelling unless the exact component is Son of Anton-owned.
        if entry == "":
            kept.append(entry)
            continue

        entry_path = Path(entry)
        should_strip = False

        # --- Check 1: Son of Anton venv site-packages ---
        # Producers inject the exact directory, never a descendant.  Exact
        # matching avoids deleting a user path nested below site-packages.
        for sp in son_of_anton_site_packages:
            if _same_path(entry_path, sp):
                should_strip = True
                break
        if should_strip:
            stripped.append(entry)
            continue

        # --- Check 2: Son of Anton repo root ---
        # The Electron app prepends the repo root so ``import tools`` works
        # in the backend.  Subprocesses don't need it and it can shadow
        # local packages of the same name.  Only the EXACT root is stripped:
        # no launcher injects a direct child (``<repo>/tools`` etc.) as an
        # independent PYTHONPATH entry, and user paths that merely happen to
        # live under the repo directory must be preserved.  Both the
        # resolved and unresolved (SON_OF_ANTON_HOME/junction) spellings count as
        # Son of Anton-owned.
        if not should_strip:
            should_strip = any(
                _same_path(entry_path, repo_root)
                for repo_root in _son_of_anton_repo_root_aliases
            )

        if should_strip:
            stripped.append(entry)
        else:
            kept.append(entry)

    if kept:
        env["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        env.pop("PYTHONPATH", None)

    if stripped:
        logger.debug(
            "Stripped Son of Anton-owned entries from PYTHONPATH: %s",
            stripped,
        )


def _read_terminal_shell_init_config() -> tuple[list[str], bool]:
    """Return (shell_init_files, auto_source_bashrc) from config.yaml.

    Best-effort — returns sensible defaults on any failure so terminal
    execution never breaks because the config file is unreadable.
    """
    try:
        from son_of_anton_cli.config import load_config

        cfg = load_config() or {}
        terminal_cfg = cfg.get("terminal") or {}
        files = terminal_cfg.get("shell_init_files") or []
        if not isinstance(files, list):
            files = []
        auto_bashrc = bool(terminal_cfg.get("auto_source_bashrc", True))
        return [str(f) for f in files if f], auto_bashrc
    except Exception:
        return [], True


def _resolve_shell_init_files() -> list[str]:
    """Resolve the list of files to source before the login-shell snapshot.

    Expands ``~`` and ``${VAR}`` references and drops anything that doesn't
    exist on disk, so a missing ``~/.bashrc`` never breaks the snapshot.
    The ``auto_source_bashrc`` path runs only when the user hasn't supplied
    an explicit list — once they have, Son of Anton trusts them.
    """
    explicit, auto_bashrc = _read_terminal_shell_init_config()

    candidates: list[str] = []
    if explicit:
        candidates.extend(explicit)
    elif auto_bashrc:
        # Build a login-shell-ish source list so tools like n / nvm / asdf /
        # pyenv that self-install into the user's shell rc land on PATH in
        # the captured snapshot.
        #
        # ~/.profile and ~/.bash_profile run first because they have no
        # interactivity guard — installers like ``n`` and ``nvm`` append
        # their PATH export there on most distros, and a non-interactive
        # ``. ~/.profile`` picks that up.
        #
        # ~/.bashrc runs last. On Debian/Ubuntu the default bashrc starts
        # with ``case $- in *i*) ;; *) return;; esac`` and exits early
        # when sourced non-interactively, which is why sourcing bashrc
        # alone misses nvm/n PATH additions placed below that guard. We
        # still include it so users who put PATH logic in bashrc (and
        # stripped the guard, or never had one) keep working.
        candidates.extend(["~/.profile", "~/.bash_profile", "~/.bashrc"])

    resolved: list[str] = []
    for raw in candidates:
        try:
            path = os.path.expandvars(os.path.expanduser(raw))
        except Exception:
            continue
        if path and os.path.isfile(path):
            resolved.append(path)
    return resolved


def _prepend_shell_init(cmd_string: str, files: list[str]) -> str:
    """Prepend ``source <file>`` lines (guarded + silent) to a bash script.

    Each file is wrapped so a failing rc file doesn't abort the whole
    bootstrap: ``set +e`` keeps going on errors, ``2>/dev/null`` hides
    noisy prompts, and ``|| true`` neutralises the exit status.
    """
    if not files:
        return cmd_string

    prelude_parts = ["set +e"]
    for path in files:
        # shlex.quote isn't available here without an import; the files list
        # comes from os.path.expanduser output so it's a concrete absolute
        # path.  Escape single quotes defensively anyway.
        safe = path.replace("'", "'\\''")
        prelude_parts.append(f"[ -r '{safe}' ] && . '{safe}' 2>/dev/null || true")
    prelude = "\n".join(prelude_parts) + "\n"
    return prelude + cmd_string


class LocalEnvironment(BaseEnvironment):
    """Run commands directly on the host machine.

    Spawn-per-call: every execute() spawns a fresh bash process.
    Session snapshot preserves env vars across calls.
    CWD persists via file-based read after each command.
    """

    _profile_scoped_passthrough = True

    def __init__(self, cwd: str = "", timeout: int = 60, env: dict = None):
        cwd = _resolve_local_initial_cwd(cwd)
        super().__init__(cwd=cwd, timeout=timeout, env=env)
        self.init_session()

    def get_temp_dir(self) -> str:
        """Return a shell-safe writable temp dir for local execution.

        Prefer POSIX-style env vars when available, keep using /tmp on regular
        Unix systems, and only fall back to tempfile.gettempdir() when it also
        resolves to a POSIX path.

        Check the environment configured for this backend first so callers can
        override the temp root explicitly (for example via terminal.env or a
        custom TMPDIR), then fall back to the host process environment.
        """
        for env_var in ("TMPDIR", "TMP", "TEMP"):
            candidate = self.env.get(env_var) or os.environ.get(env_var)
            if candidate and candidate.startswith("/"):
                return candidate.rstrip("/") or "/"

        if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK | os.X_OK):
            return "/tmp"

        candidate = tempfile.gettempdir()
        if candidate.startswith("/"):
            return candidate.rstrip("/") or "/"

        return "/tmp"

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        bash = _find_bash()
        # For login-shell invocations (used by init_session to build the
        # environment snapshot), prepend sources for the user's bashrc /
        # custom init files so tools registered outside bash_profile
        # (nvm, asdf, pyenv, …) end up on PATH in the captured snapshot.
        # Non-login invocations are already sourcing the snapshot and
        # don't need this.
        if login:
            init_files = _resolve_shell_init_files()
            if init_files:
                cmd_string = _prepend_shell_init(cmd_string, init_files)
        args = [bash, "-l", "-c", cmd_string] if login else [bash, "-c", cmd_string]
        run_env = _make_run_env(self.env, str(self.cwd))

        # Recover when the cwd has been deleted out from under us — usually by
        # a previous tool call that ran ``rm -rf`` on its own working dir
        # (issue #17558).  Popen would otherwise raise FileNotFoundError on
        # the cwd before bash starts, wedging every subsequent call until the
        # gateway restarts.
        safe_cwd = _resolve_safe_cwd(self.cwd)
        if safe_cwd != self.cwd:
            logger.warning(
                "LocalEnvironment cwd %r is missing on disk; "
                "falling back to %r so terminal commands keep working.",
                self.cwd,
                safe_cwd,
            )
            self.cwd = safe_cwd

        _popen_cwd = self.cwd

        proc = subprocess.Popen(
            args,
            text=True,
            env=run_env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            start_new_session=True,
            cwd=_popen_cwd,
        )
        try:
            proc._son_of_anton_pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pass

        if stdin_data is not None:
            _pipe_stdin(proc, stdin_data)

        return proc

    def _kill_process(self, proc):
        """Kill the entire process group (all children)."""

        def _group_alive(pgid: int) -> bool:
            try:
                os.killpg(pgid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                # The group exists, even if this process cannot signal it.
                return True

        def _wait_for_group_exit(pgid: int, timeout: float) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                # Reap the wrapper promptly. A dead but unreaped group leader
                # still makes killpg(pgid, 0) report the group as alive.
                try:
                    proc.poll()
                except Exception:
                    pass
                if not _group_alive(pgid):
                    return True
                time.sleep(0.05)
            try:
                proc.poll()
            except Exception:
                pass
            return not _group_alive(pgid)

        try:
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = getattr(proc, "_son_of_anton_pgid", None)
                if pgid is None:
                    raise

            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                return

            # Wait on the process group, not just the shell wrapper. Under
            # load the wrapper can exit before grandchildren do; returning
            # at that point leaves orphaned process-group members behind.
            if _wait_for_group_exit(pgid, 1.0):
                return

            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                return
            _wait_for_group_exit(pgid, 2.0)
            try:
                proc.wait(timeout=0.2)
            except (subprocess.TimeoutExpired, OSError):
                pass
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass

    def _update_cwd(self, result: dict):
        """Update cwd from the stdout marker emitted by the wrapped command.

        The base command wrapper already appends ``pwd -P`` to stdout inside a
        session-specific marker, so the local backend can share the same parser
        as remote backends instead of re-reading the temp file it just wrote.
        """
        self._extract_cwd_from_output(result)

    def _extract_cwd_from_output(self, result: dict):
        """Same semantics as the base class, validating the parsed directory
        exists before assigning to ``self.cwd`` — a stale path would otherwise
        make ``_run_bash``'s safe-cwd recovery warn on every subsequent
        command.

        Always defers to the base class for stripping the marker text from
        ``result["output"]`` so output formatting is identical.
        """
        # Snapshot pre-existing cwd, defer to base for parsing + marker
        # stripping, then validate whatever it assigned.
        prev_cwd = self.cwd
        super()._extract_cwd_from_output(result)
        if self.cwd != prev_cwd:
            if not self.cwd or not os.path.isdir(self.cwd):
                # Stale / non-existent path — keep previous cwd; _run_bash
                # will resolve a safe fallback on the next call if needed.
                # The rollback restores a value this command did not observe,
                # so it is not attributable to this command's session either.
                self.cwd = prev_cwd
                result.pop("cwd_observed", None)

    def cleanup(self):
        """Clean up temp files."""
        for f in (self._snapshot_path, self._cwd_file):
            try:
                os.unlink(f)
            except OSError:
                pass
        # Remove any orphaned atomic-write temp snapshots (snap.tmp.<bashpid>)
        # a failed/interrupted mv could have left behind (#38249).
        try:
            import glob
            for tmp in glob.glob(f"{self._snapshot_path}.tmp.*"):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except Exception:
            pass
