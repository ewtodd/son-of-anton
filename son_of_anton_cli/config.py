"""
Configuration management for Son of Anton Agent.

Config files are stored in ~/.son-of-anton/ for easy access:
- ~/.son-of-anton/config.toml  - All settings (model, toolsets, terminal, etc.)
- ~/.son-of-anton/.env         - API keys and secrets

This module provides:
- son-of-anton config          - Show current configuration
- son-of-anton config get      - Print a resolved configuration value
"""

import copy
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Set

from son_of_anton_cli.route_identity import normalize_route_base_url

logger = logging.getLogger(__name__)

# Track which (config_path, mtime_ns, size) tuples we've already warned about
# so concurrent CLI/gateway loads of a broken config.toml don't spam stderr
# every time. Cleared automatically when the file changes (different mtime).
_CONFIG_PARSE_WARNED: set = set()


def _backup_corrupt_config(config_path: Path) -> Optional[Path]:
    """Preserve a corrupted ``config.toml`` by copying it to a timestamped ``.bak``.

    When the TOML can't be parsed, ``load_config()`` silently falls back to
    ``DEFAULT_CONFIG`` and the user's broken file stays on disk untouched.
    That file is still the user's only copy of their intended overrides — if
    they re-run the setup wizard or ``son-of-anton config set`` (which rewrites
    ``config.toml``), the broken-but-recoverable content is gone for good.

    This snapshots the corrupted file to ``config.toml.corrupt.<ts>.bak`` so
    the user can diff/repair it. Unlike Gemini CLI's policy-file recovery
    (which resets the live file to a clean state), we deliberately leave
    ``config.toml`` in place: son-of-anton never silently mutates the user's config,
    and leaving it means a hand-fixed file is re-read on the next load. The
    backup is best-effort — any failure (permissions, symlink, disk full) is
    swallowed so config loading is never blocked by backup problems.

    Returns the backup path on success, else ``None``. Symlinks are not
    followed/copied (mirrors the Gemini #21541 lstat guard) to avoid
    clobbering whatever a malicious/misconfigured symlink points at.
    """
    try:
        if config_path.is_symlink():
            return None
        st = config_path.stat()
        if st.st_size == 0:
            # Empty file isn't worth preserving and a TOML parse returns {}
            # for it anyway (so it wouldn't reach here), but guard regardless.
            return None
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = config_path.with_name(f"{config_path.name}.corrupt.{ts}.bak")
        # Don't clobber an existing backup from the same second; if there's
        # already a corrupt backup for this exact mtime, assume we've snapshotted
        # this corruption already and skip (the dedup cache normally prevents a
        # second call, but a process restart can clear it).
        sibling_baks = list(
            config_path.parent.glob(f"{config_path.name}.corrupt.*.bak")
        )
        for existing in sibling_baks:
            try:
                if existing.stat().st_size == st.st_size:
                    # Same size as the current broken file — likely the same
                    # corruption already preserved. Avoid backup churn.
                    return None
            except OSError:
                continue
        if backup_path.exists():
            return None
        shutil.copy2(config_path, backup_path)
        return backup_path
    except Exception:
        return None


def _warn_config_parse_failure(
    config_path: Path, exc: Exception, *, fallback: str = "defaults"
) -> None:
    """Surface a config.toml parse failure to user, log, and stderr.

    A TOML parse error in ``~/.son-of-anton/config.toml`` causes ``load_config()``
    to silently fall back to ``DEFAULT_CONFIG``, which means every user
    override (auxiliary providers, fallback chain, model overrides, etc.)
    is dropped. Before this helper that was a one-line ``print(...)`` that
    scrolled off-screen on the first invocation and was never seen again.

    Now: warn once per (path, mtime_ns, size) on stderr **and** in
    ``agent.log`` / ``errors.log`` at WARNING level so ``son-of-anton logs``
    surfaces it. Re-warns automatically if the file changes (different
    mtime/size), so users editing the config see the next failure. On the
    first warning for a given broken file we also snapshot it to a
    timestamped ``.bak`` (best-effort) so the user's recoverable content
    survives any later rewrite of ``config.toml`` by the setup wizard or
    ``son-of-anton config set``.

    ``fallback`` selects the message wording: ``"defaults"`` (fresh process,
    nothing else to serve) or ``"last-known-good"`` (in-process retention of
    the previously loaded config — see the codex#31188 port in
    ``_load_config_impl``).
    """
    try:
        st = config_path.stat()
        key = (str(config_path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(config_path), 0, 0)
    if key in _CONFIG_PARSE_WARNED:
        return
    _CONFIG_PARSE_WARNED.add(key)

    backup_path = _backup_corrupt_config(config_path)

    if fallback == "last-known-good":
        msg = (
            f"Failed to parse {config_path}: {exc}. "
            f"Keeping the previously loaded config for this process — "
            f"edits to config.toml are being IGNORED until the TOML is fixed."
        )
    else:
        msg = (
            f"Failed to parse {config_path}: {exc}. "
            f"Falling back to default config — every user override "
            f"(auxiliary providers, fallback chain, model settings) is being IGNORED. "
            f"Fix the TOML and restart."
        )
    if backup_path is not None:
        msg += f" A copy of the corrupted file was saved to {backup_path}."
    logger.warning(msg)
    try:
        sys.stderr.write(f"⚠️  son-of-anton config: {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Env var names that influence how the next subprocess executes —
# never writable through ``save_env_value``. Anything that controls
# the loader, interpreter, shell, or replacement editor counts:
#
# * ``LD_PRELOAD`` / ``LD_LIBRARY_PATH`` / ``LD_AUDIT`` — Linux dynamic
#   loader. ``DYLD_*`` — macOS equivalent. Planting a path here means
#   the next ``subprocess.run([...])`` Son of Anton makes loads attacker code
#   before main().
# * ``PYTHONPATH`` / ``PYTHONHOME`` / ``PYTHONSTARTUP`` /
#   ``PYTHONUSERBASE`` — Python interpreter init. Son of Anton itself starts
#   from one of these on every restart.
# * ``NODE_OPTIONS`` / ``NODE_PATH`` — Node interpreter; affects npm,
#   ``son-of-anton update``, the TUI build.
# * ``PATH`` — too broad to allow. The env writer never needs to rewrite
#   the operator's PATH; if a tool can't be found, the fix is to add an
#   absolute path in the integration config, not to mutate PATH globally.
# * ``GIT_SSH_COMMAND`` / ``GIT_EXEC_PATH`` — git rewrites that fire
#   on every plugin install / ``son-of-anton update``.
# * ``BROWSER`` / ``EDITOR`` / ``VISUAL`` / ``PAGER`` — commands the
#   shell or CLI invokes implicitly. Wrong values here = RCE on next
#   ``$EDITOR``.
# * ``SHELL`` — what subprocess uses with ``shell=True`` (we try to
#   avoid that, but defense in depth).
# * ``SON_OF_ANTON_HOME`` / ``SON_OF_ANTON_PROFILE`` / ``SON_OF_ANTON_CONFIG`` /
#   ``SON_OF_ANTON_ENV`` — Son of Anton runtime location flags. Writing these into
#   ``.env`` would relocate state in ways the user did not request from
#   the env writer. ``config.toml`` is the supported surface for these.
#
# IMPORTANT: ``SON_OF_ANTON_*`` overall is NOT blocked. Many legitimate
# integration credentials follow that prefix (SON_OF_ANTON_LANGFUSE_PUBLIC_KEY,
# SON_OF_ANTON_SPOTIFY_CLIENT_ID, ...). The
# denylist is name-by-name on purpose so the gate stays narrow and
# doesn't accidentally break provider setup wizards.
#
# This is enforced on *write* only — values already in ``.env`` (set
# by the operator out-of-band, or pre-existing) keep working. The
# point is that the env writer's writable surface cannot escalate by
# planting them.
_ENV_VAR_NAME_DENYLIST: frozenset[str] = frozenset({
    # Loader / linker
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_FALLBACK_FRAMEWORK_PATH",
    # Python
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
    "PYTHONEXECUTABLE", "PYTHONNOUSERSITE",
    # Node
    "NODE_OPTIONS", "NODE_PATH",
    # General
    "PATH", "SHELL", "BROWSER", "EDITOR", "VISUAL", "PAGER",
    # Git
    "GIT_SSH_COMMAND", "GIT_EXEC_PATH", "GIT_SHELL",
    # Son of Anton runtime location — never via the env writer.
    # NOT a SON_OF_ANTON_* blanket: integration credentials (SON_OF_ANTON_GEMINI_*,
    # SON_OF_ANTON_LANGFUSE_*, SON_OF_ANTON_SPOTIFY_*, ...) ARE allowed.
    "SON_OF_ANTON_HOME", "SON_OF_ANTON_PROFILE", "SON_OF_ANTON_CONFIG", "SON_OF_ANTON_ENV",
})


def _reject_denylisted_env_var(key: str) -> None:
    """Raise if ``key`` is in :data:`_ENV_VAR_NAME_DENYLIST`.

    Centralised so both the regular and "secure" env writers share the
    same gate, and so the message is consistent for callers.
    """
    if key in _ENV_VAR_NAME_DENYLIST:
        raise ValueError(
            f"Environment variable {key!r} is on the writer denylist. "
            "Names that influence subprocess execution (LD_PRELOAD, "
            "PYTHONPATH, PATH, EDITOR, ...) or Son of Anton runtime location "
            "(SON_OF_ANTON_HOME, SON_OF_ANTON_PROFILE, ...) cannot be persisted via "
            "the env writer. If you really need this, edit "
            "~/.son-of-anton/.env directly."
        )

_LAST_EXPANDED_CONFIG_BY_PATH: Dict[str, Any] = {}
# (path, mtime_ns, size) -> cached expanded config dict.
# load_config() returns a deepcopy of the cached value when the file
# hasn't changed since the last load, skipping the TOML parse +
# _deep_merge + _normalize_* + _expand_env_vars (~13 ms/call).
# save_config() writes via atomic_toml_write which
# produces a fresh inode, so stat() sees a new mtime_ns and the next
# load repopulates automatically — no explicit invalidation hook.
# Cached tuple is (user_mtime_ns, user_size, merged_value, env_ref_snapshot) —
# the env snapshot invalidates it when a referenced ${VAR} changes value
# (late .env load, in-process rotation — #58514).
_LOAD_CONFIG_CACHE: Dict[str, Tuple[int, int, Dict[str, Any], Dict[str, Optional[str]]]] = {}
# (path, mtime_ns, size) -> cached raw yaml dict. Same pattern as
# _LOAD_CONFIG_CACHE but for read_raw_config() — used when callers want
# the user's on-disk values without defaults merged in.
_RAW_CONFIG_CACHE: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}
# Serializes all config read/write paths. libyaml's C extension is not
# thread-safe for concurrent safe_load() on the same file, and multiple
# tool threads (approval.py, setup flows) hit
# load_config / read_raw_config / save_config from different threads
# during long agent runs. RLock (not Lock) because save_config internally
# calls read_raw_config. Also covers mutation of the module-level cache
# dicts above.
_CONFIG_LOCK = threading.RLock()
# Env var names written to .env that aren't in OPTIONAL_ENV_VARS
# (managed by setup/provider flows directly).
_EXTRA_ENV_KEYS = frozenset({
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    "SIGNAL_ACCOUNT", "SIGNAL_HTTP_URL",
    "SIGNAL_ALLOWED_USERS", "SIGNAL_GROUP_ALLOWED_USERS",
    "SIGNAL_HOME_CHANNEL", "SIGNAL_HOME_CHANNEL_NAME",
    "TERMINAL_ENV", "TERMINAL_SSH_KEY", "TERMINAL_SSH_PORT",
    # SON_OF_ANTON_TOOL_PROGRESS_MODE is deprecated (replaced by display.tool_progress
    # in config.toml) but STILL READ at runtime by the gateway as a back-compat
    # fallback, so it must stay known to reload/compat paths. The boolean
    # SON_OF_ANTON_TOOL_PROGRESS variant is fully unsupported since the v12 config
    # support floor retired its only consumer (the v3→4 migration): it is no
    # longer listed here and doctor flags it as ignored.
    "SON_OF_ANTON_TOOL_PROGRESS_MODE",
    # Langfuse observability plugin — optional tuning keys + standard SDK vars.
    # Activation is via plugins.enabled (opt-in through `son-of-anton plugins enable
    # observability/langfuse` or `son-of-anton tools → Langfuse`); credentials gate
    # the plugin at runtime.
    "SON_OF_ANTON_LANGFUSE_ENV",
    "SON_OF_ANTON_LANGFUSE_RELEASE",
    "SON_OF_ANTON_LANGFUSE_SAMPLE_RATE",
    "SON_OF_ANTON_LANGFUSE_MAX_CHARS",
    "SON_OF_ANTON_LANGFUSE_CAPTURE",
    "SON_OF_ANTON_LANGFUSE_DEBUG",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    # ACP (Agent Client Protocol) keys — profile-isolable so different
    # profiles can use different ACP backends without cross-leak.
    "SON_OF_ANTON_ACP_AUTH_METHOD",
    "SON_OF_ANTON_ACP_AUTO_APPROVE",
    "SON_OF_ANTON_COPILOT_ACP_COMMAND",
    "SON_OF_ANTON_COPILOT_ACP_ARGS",
    "COPILOT_CLI_PATH",
    "COPILOT_ACP_BASE_URL",
})

from son_of_anton_cli.colors import Colors, color
from son_of_anton_cli.default_soul import DEFAULT_SOUL_MD, is_legacy_template_soul


# =============================================================================
# Managed mode (NixOS declarative config)
# =============================================================================

_MANAGED_TRUE_VALUES = ("true", "1", "yes")
# Only the NixOS module ever wrote a bare "true" or an empty marker, so both
# legacy signals name that system.
_LEGACY_MANAGED_SYSTEM = "nixos"


def get_managed_system() -> Optional[str]:
    """Return the package manager owning this install, if any."""
    raw = os.getenv("SON_OF_ANTON_MANAGED", "").strip()
    marker = None
    if raw:
        marker = raw.lower()
    else:
        managed_marker = get_son_of_anton_home() / ".managed"
        # An interactive shell reads the marker, because it does not see the
        # SON_OF_ANTON_MANAGED variable of the service. A marker with content
        # names the system that manages the install.
        try:
            marker_exists = managed_marker.exists()
        except PermissionError:
            # Cross-user home (su without -): treat as not managed rather
            # than crashing the config load with a raw traceback.
            marker_exists = False
            marker = ""
        if marker_exists:
            try:
                marker = managed_marker.read_text(encoding="utf-8", errors="replace").strip().lower()
            except OSError:
                marker = ""

    if marker is None:
        return None

    if marker == "" or marker in _MANAGED_TRUE_VALUES:
        return _LEGACY_MANAGED_SYSTEM

    return marker


def is_managed() -> bool:
    """Check if Son of Anton is running in package-manager-managed mode.

    Two signals: the SON_OF_ANTON_MANAGED env var (set by the systemd service),
    or a .managed marker file in SON_OF_ANTON_HOME (set by the NixOS activation
    script, so interactive shells also see it).
    """
    return get_managed_system() is not None


def format_managed_message(action: str = "modify this Son of Anton installation") -> str:
    """Build a user-facing error for managed installs."""
    managed_system = get_managed_system() or "a package manager"
    return (
        f"Cannot {action}: this Son of Anton installation is managed by {managed_system}.\n"
        "Use your package manager to upgrade or reinstall Son of Anton."
    )

def managed_error(action: str = "modify configuration"):
    """Print user-friendly error for managed mode."""
    print(format_managed_message(action), file=sys.stderr)


# =============================================================================
# Config paths
# =============================================================================

# Re-export from son_of_anton_constants — canonical definition lives there.
from son_of_anton_constants import get_son_of_anton_home  # noqa: F811,E402
from utils import atomic_replace, dump_toml, fast_toml_load

def get_config_path() -> Path:
    """Get the main config file path."""
    return get_son_of_anton_home() / "config.toml"

def get_env_path() -> Path:
    """Get the .env file path (for API keys)."""
    return get_son_of_anton_home() / ".env"

def get_project_root() -> Path:
    """Get the project installation directory."""
    return Path(__file__).parent.parent.resolve()

def _secure_dir(path):
    """Set directory to owner-only access (0700 by default).

    Skipped in managed mode — the NixOS module sets group-readable
    permissions (0750) so interactive users in the son-of-anton group can
    share state with the gateway service.

    The mode can be overridden via the SON_OF_ANTON_HOME_MODE environment variable
    (e.g. SON_OF_ANTON_HOME_MODE=0701) for deployments where a web server (nginx,
    caddy, etc.) needs to traverse SON_OF_ANTON_HOME to reach a served subdirectory.
    The execute-only bit on a directory permits cd-through without exposing
    directory listings.
    """
    if is_managed():
        return
    try:
        mode_str = os.environ.get("SON_OF_ANTON_HOME_MODE", "").strip()
        mode = int(mode_str, 8) if mode_str else 0o700
    except ValueError:
        mode = 0o700
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


def _secure_file(path):
    """Set file to owner-only read/write (0600).

    Skipped in managed mode — the NixOS activation script sets
    group-readable permissions (0640) on config files.
    """
    if is_managed():
        return
    try:
        if os.path.exists(str(path)):
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def _ensure_default_soul_md(home: Path) -> None:
    """Seed a default SOUL.md into SON_OF_ANTON_HOME, upgrading legacy empty templates.

    First run: write DEFAULT_SOUL_MD. Existing installs whose SOUL.md is still
    the old comment-only scaffold (seeded by older installers, which shadowed
    the runtime default) get upgraded in place to DEFAULT_SOUL_MD. A SOUL.md
    the user actually customized is never touched.
    """
    soul_path = home / "SOUL.md"
    if soul_path.exists():
        try:
            existing = soul_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        if not is_legacy_template_soul(existing):
            return
        # Legacy empty template -> upgrade to the real default in place.
    soul_path.write_text(DEFAULT_SOUL_MD, encoding="utf-8")
    _secure_file(soul_path)


# Home paths whose directory skeleton has been created this process — see
# ensure_son_of_anton_home(). Only successful passes are recorded, so a raised
# managed-mode/missing-profile error keeps re-checking on later loads.
_SON_OF_ANTON_HOME_ENSURED: set = set()


def ensure_son_of_anton_home():
    """Ensure ~/.son-of-anton directory structure exists with secure permissions.

    In managed mode (NixOS), dirs are created by the activation script with
    setgid + group-writable (2770). We skip mkdir and set umask(0o007) so
    any files created (e.g. SOUL.md) are group-writable (0660).

    Memoized per home path: this runs on EVERY ``load_config()`` (inside the
    config lock), and the ~14 mkdir/chmod syscalls per call made repeated
    config loads the dominant cost of hot read paths like ``model.options``.
    After the first successful pass for a given ``SON_OF_ANTON_HOME`` we only re-run
    the full walk if the home directory itself has vanished (a deleted home is
    recreated on the next load, as before). Profile switches change
    ``get_son_of_anton_home()`` and therefore re-run for the new path.
    """
    home = get_son_of_anton_home()
    key = str(home)

    try:
        home_ok = home.is_dir() if key in _SON_OF_ANTON_HOME_ENSURED else False
    except PermissionError:
        raise RuntimeError(
            f"SON_OF_ANTON_HOME {home} is not accessible to this user — "
            "it may have leaked across accounts (use `su - <user>` or unset "
            "SON_OF_ANTON_HOME)."
        )
    if home_ok:
        return
    # Named profiles must be created explicitly (e.g. ``son-of-anton profile create``).
    # If a stale process keeps running after the profile was renamed/deleted,
    # silently mkdir-ing the old SON_OF_ANTON_HOME would resurrect an empty skeleton
    # and make the deleted profile reappear in Desktop/profile lists.
    if home.parent.name == "profiles" and not home.exists():
        raise FileNotFoundError(
            f"Named profile home does not exist: {home}. "
            "Create the profile explicitly before using it."
        )
    if is_managed():
        old_umask = os.umask(0o007)
        try:
            _ensure_son_of_anton_home_managed(home)
        finally:
            os.umask(old_umask)
    else:
        home.mkdir(parents=True, exist_ok=True)
        _secure_dir(home)
        for subdir in (
            "cron", "sessions", "logs", "memories",
            "pairing", "hooks", "image_cache", "audio_cache",
        ):
            d = home / subdir
            d.mkdir(parents=True, exist_ok=True)
            _secure_dir(d)
        _ensure_default_soul_md(home)

    _SON_OF_ANTON_HOME_ENSURED.add(key)


def _ensure_son_of_anton_home_managed(home: Path):
    """Managed-mode variant: verify dirs exist (activation creates them), seed SOUL.md."""
    if not home.is_dir():
        raise RuntimeError(
            f"SON_OF_ANTON_HOME {home} does not exist."
        )
    for subdir in ("cron", "sessions", "logs", "memories"):
        d = home / subdir
        if not d.is_dir():
            raise RuntimeError(f"{d} does not exist.")
    # Inside umask(0o007) scope — SOUL.md will be created as 0660
    _ensure_default_soul_md(home)


# =============================================================================
# Config loading/saving
# =============================================================================

from son_of_anton_cli.config_defaults import DEFAULT_CONFIG, OPTIONAL_ENV_VARS  # noqa: F401

# =============================================================================
# Config Migration System
# =============================================================================

# Tool Gateway env vars are always visible — they're useful for
# self-hosted / custom gateway setups regardless of subscription state.


def _set_nested(config, dotted_key: str, value):
    """Set a value at an arbitrarily nested dotted key path.

    Supports both dict and list navigation:
      _set_nested(c, "a.b.c", 1)     → c["a"]["b"]["c"] = 1
      _set_nested(c, "a.0.b", 1)     → c["a"][0]["b"] = 1
      _set_nested(c, "providers.1", "x") → c["providers"][1] = "x"

    Intermediate dicts are created on demand.  List indices are parsed
    from numeric path segments; the referenced index must already exist
    (we do not grow lists — the user is navigating into structure they
    wrote themselves).  If a segment targets a non-container leaf
    (scalar), the leaf is replaced with a fresh dict so the write can
    proceed — this preserves the pre-existing behavior for bare scalar
    overrides (e.g. setting ``a.b.c`` where ``a.b`` was previously a
    string).

    Guards against #17876: before this fix the code unconditionally
    replaced any non-dict value (including lists) with ``{}``, silently
    destroying list-typed config like ``custom_providers`` whenever a
    caller used an indexed path.
    """
    parts = dotted_key.split(".")
    current = config
    for part in parts[:-1]:
        if isinstance(current, list):
            try:
                idx = int(part)
            except (TypeError, ValueError):
                raise TypeError(
                    f"Cannot navigate into list at key {dotted_key!r}: "
                    f"segment {part!r} is not a numeric index"
                )
            current = current[idx]
        elif isinstance(current, dict):
            existing = current.get(part)
            # Preserve dicts and lists; replace missing/scalar with a fresh dict.
            if part not in current or not isinstance(existing, (dict, list)):
                current[part] = {}
            current = current[part]
        else:
            raise TypeError(
                f"Cannot navigate into {type(current).__name__} at key {dotted_key!r}"
            )
    last = parts[-1]
    if isinstance(current, list):
        current[int(last)] = value
    else:
        current[last] = value


def clear_model_endpoint_credentials(
    model_cfg: Dict[str, Any],
    *,
    clear_api_key: bool = True,
    clear_api_mode: bool = True,
    clear_base_url: bool = False,
) -> Dict[str, Any]:
    """Remove stale inline endpoint credentials from a model config.

    ``model.api_key`` is valid only for explicit custom endpoint assignments.
    Built-in providers resolve credentials from env vars, auth.json, or the
    credential pool. When switching away from a custom endpoint, leaving these
    fields behind keeps secrets in config.toml and can contaminate later custom
    resolution paths.
    """
    if not isinstance(model_cfg, dict):
        return model_cfg
    if clear_api_key:
        model_cfg.pop("api_key", None)
        model_cfg.pop("api", None)
    if clear_api_mode:
        model_cfg.pop("api_mode", None)
    if clear_base_url:
        model_cfg.pop("base_url", None)
    return model_cfg


_MISSING = object()


def _get_nested(config, dotted_key: str):
    """Return a dotted-path value from nested dict/list config data."""
    current = config
    for part in dotted_key.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (TypeError, ValueError, IndexError):
                return _MISSING
        elif isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        else:
            return _MISSING
    return current


def _is_env_config_key(key: str) -> bool:
    """Return whether `son-of-anton config set` routes this key to .env."""
    if "." in key:
        return False
    key_upper = key.upper()
    api_keys = [
        'OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'VOICE_TOOLS_OPENAI_KEY',
        'EXA_API_KEY', 'PARALLEL_API_KEY', 'FIRECRAWL_API_KEY', 'FIRECRAWL_API_URL',
        'FIRECRAWL_GATEWAY_URL', 'TOOL_GATEWAY_DOMAIN', 'TOOL_GATEWAY_SCHEME',
        'TOOL_GATEWAY_USER_TOKEN', 'TAVILY_API_KEY',
        'BROWSERBASE_API_KEY', 'BROWSERBASE_PROJECT_ID', 'BROWSER_USE_API_KEY',
        'FAL_KEY', 'DISCORD_BOT_TOKEN',
        'TERMINAL_SSH_HOST', 'TERMINAL_SSH_USER', 'TERMINAL_SSH_KEY',
        'SUDO_PASSWORD', 'SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN',
        'GITHUB_TOKEN', 'HONCHO_API_KEY',
    ]
    return (
        key_upper in api_keys
        or key_upper.endswith(('_API_KEY', '_TOKEN', '_SECRET'))
        or key_upper.startswith('TERMINAL_SSH')
    )


def _format_config_get_value(value, *, as_json: bool) -> str:
    """Format a config value for command-line output."""
    if as_json:
        import json
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, dict):
        return dump_toml(value).rstrip()
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ``_normalize_custom_provider_entry`` runs on every ``load_picker_context()``
# call (i.e. per interactive picker/inventory request), so any warning it emits
# fires repeatedly for the same static config. Deduplicate per (provider,
# signature) — a repeated warning storm would otherwise spam stderr and the
# log on every picker request. The cache lives for the process lifetime.
_PROVIDER_NORMALIZE_WARNED: set = set()


def _warn_once_per_provider(
    provider_key: str, signature: str, msg: str, *args: Any
) -> None:
    """Emit ``logger.warning(msg, *args)`` at most once per (provider, signature)."""
    dedup_key = (provider_key or "?", signature)
    if dedup_key in _PROVIDER_NORMALIZE_WARNED:
        return
    _PROVIDER_NORMALIZE_WARNED.add(dedup_key)
    logger.warning(msg, *args)


_API_MODE_ALIASES = {
    # Values accepted by earlier releases (and natural spellings) mapped to
    # the canonical transport names consumed by agent_init. Before this map
    # existed, an unrecognized api_mode was silently ignored and the
    # transport fell through to hostname-based guessing, so a config that
    # said ``api_mode: openai`` (valid on older releases) could flip to
    # a dead mode after an update and break the provider (#66543 discussion).
    "openai": "chat_completions",
    "openai_chat": "chat_completions",
    "openai-chat": "chat_completions",
    "chat-completions": "chat_completions",
    "chatcompletions": "chat_completions",
}


def _canonical_api_mode(api_mode: str) -> str:
    """Map legacy/alias ``api_mode`` spellings to canonical transport names.

    Unknown values pass through unchanged (callers keep their existing
    fall-through behavior); known aliases are rewritten so downstream
    consumers (``agent_init``'s accepted-set check, runtime resolution)
    see a canonical name instead of silently discarding the user's intent.
    """
    cleaned = api_mode.strip()
    return _API_MODE_ALIASES.get(cleaned.lower(), cleaned)


def _normalize_custom_provider_entry(
    entry: Any,
    *,
    provider_key: str = "",
) -> Optional[Dict[str, Any]]:
    """Return a runtime-compatible custom provider entry or ``None``."""
    if not isinstance(entry, dict):
        return None

    # Shallow-copy before the alias normalization below writes into the
    # entry: callers (get_compatible_custom_providers,
    # providers_dict_to_custom_providers) pass live sub-dicts from
    # load_config_readonly()'s shared cache, and mutating those both
    # violates the cache's no-mutation contract and leaks duplicated
    # alias keys back into config.toml through any later
    # save_config(load_config()) round-trip.
    entry = dict(entry)

    # Accept camelCase aliases commonly used in hand-written configs.
    _CAMEL_ALIASES: Dict[str, str] = {
        "apiKey": "api_key",
        "baseUrl": "base_url",
        "apiMode": "api_mode",
        "keyEnv": "key_env",
        "apiKeyEnv": "key_env",  # alias — OpenClaw-compatible + docs variant
        "defaultModel": "default_model",
        "contextLength": "context_length",
        "rateLimitDelay": "rate_limit_delay",
    }
    # api_key_env is a documented snake_case alias for key_env (see
    # website/docs/guides/azure-foundry.md).  Normalize it up front so the
    # rest of the normalizer treats it as the canonical field.
    if "api_key_env" in entry and "key_env" not in entry:
        entry["key_env"] = entry["api_key_env"]
    _KNOWN_KEYS = {
        # ``provider`` duplicates the ``providers.<name>`` mapping key and is
        # unused here, but Son of Anton' own config writer has historically emitted it
        # into provider entries. Accept it silently so those (self-written)
        # configs don't warn on every load.
        "provider",
        "name", "api", "url", "base_url", "api_key", "key_env", "api_key_env",
        "key_cmd",
        "api_mode", "transport", "model", "default_model", "models",
        "models_discovered",
        "context_length", "rate_limit_delay",
        "request_timeout_seconds", "stale_timeout_seconds",
        "discover_models", "extra_body", "extra_headers",
        "ssl_ca_cert", "ssl_verify",
    }
    for camel, snake in _CAMEL_ALIASES.items():
        if camel in entry and snake not in entry:
            _warn_once_per_provider(
                provider_key, f"camel:{camel}",
                "providers.%s: camelCase key '%s' auto-mapped to '%s' "
                "(use snake_case to avoid this warning)",
                provider_key or "?", camel, snake,
            )
            entry[snake] = entry[camel]
    unknown = set(entry.keys()) - _KNOWN_KEYS - set(_CAMEL_ALIASES.keys())
    if unknown:
        _warn_once_per_provider(
            provider_key, "unknown:" + ",".join(sorted(unknown)),
            "providers.%s: unknown config keys ignored: %s",
            provider_key or "?", ", ".join(sorted(unknown)),
        )

    from urllib.parse import urlparse

    base_url = ""
    for url_key in ("base_url", "url", "api"):
        raw_url = entry.get(url_key)
        if isinstance(raw_url, str) and raw_url.strip():
            candidate = raw_url.strip()
            # Accept URLs containing unresolved placeholder tokens — both
            # ``${ENV_VAR}`` env-refs and bare ``{region}``-style templates —
            # without URL validation. They are expanded at runtime, so a
            # caller reaching this normalizer with raw (un-expanded) config
            # would otherwise see the provider silently dropped (#14457).
            if re.search(r"\{[^}]+\}", candidate):
                base_url = candidate
                break
            parsed = urlparse(candidate)
            if parsed.scheme and parsed.netloc:
                base_url = candidate
                break
            else:
                logger.warning(
                    "providers.%s: '%s' value '%s' is not a valid URL "
                    "(no scheme or host) — skipped",
                    provider_key or "?", url_key, candidate,
                )
    if not base_url:
        return None

    name = ""
    raw_name = entry.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        name = raw_name.strip()
    elif provider_key.strip():
        name = provider_key.strip()
    if not name:
        return None

    normalized: Dict[str, Any] = {
        "name": name,
        "base_url": base_url,
    }

    provider_key = provider_key.strip()
    if provider_key:
        normalized["provider_key"] = provider_key

    api_key = entry.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        normalized["api_key"] = api_key.strip()

    key_env = entry.get("key_env") or entry.get("api_key_env")
    if isinstance(key_env, str) and key_env.strip():
        normalized["key_env"] = key_env.strip()
        if entry.get("api_key_env") and not entry.get("key_env"):
            normalized["api_key_env"] = key_env.strip()

    api_mode = entry.get("api_mode") or entry.get("transport")
    if isinstance(api_mode, str) and api_mode.strip():
        normalized["api_mode"] = _canonical_api_mode(api_mode)

    model_name = entry.get("model") or entry.get("default_model")
    if isinstance(model_name, str) and model_name.strip():
        normalized["model"] = model_name.strip()

    # Entry-level marker: the ``models`` mapping was auto-discovered by
    # Son of Anton (``_save_discovered_models_to_config``), not hand-curated.
    # Older Son of Anton versions wrote an in-mapping ``__discovered_model_catalog__``
    # sentinel instead; accept it on read and strip it from the models
    # mapping so sentinel keys never surface as model IDs.
    models_discovered = entry.get("models_discovered") is True

    models = entry.get("models")
    if isinstance(models, dict) and models:
        # Shallow-copy: `entry` may alias a cached config sub-dict, and the
        # normalized entry escapes into long-lived runtime state
        # (agent._custom_providers) — don't share the cached models mapping.
        models_copy = dict(models)
        if models_copy.pop("__discovered_model_catalog__", None) is True:
            models_discovered = True
        models_copy.pop("__explicit_model_allowlist__", None)
        if models_copy:
            normalized["models"] = models_copy
    elif isinstance(models, list) and models:
        # Hand-edited configs (and older Son of Anton versions) may write
        # ``models`` as a plain list of ids or as ``[{id: ...}]`` rows.
        # Preserve both by converting to the dict shape downstream code
        # expects; otherwise normalize silently drops the list and /model
        # shows the provider with (0) models.
        normalized_models: Dict[str, Any] = {}
        for item in models:
            if isinstance(item, str) and item.strip():
                normalized_models[item.strip()] = {}
                continue
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                model_id = item.get("name")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            model_meta = {
                k: v for k, v in item.items() if k not in {"id", "name"}
            }
            normalized_models[model_id.strip()] = model_meta
        if normalized_models:
            normalized["models"] = normalized_models

    if models_discovered:
        normalized["models_discovered"] = True

    context_length = entry.get("context_length")
    if isinstance(context_length, int) and context_length > 0:
        normalized["context_length"] = context_length

    rate_limit_delay = entry.get("rate_limit_delay")
    if isinstance(rate_limit_delay, (int, float)) and rate_limit_delay >= 0:
        normalized["rate_limit_delay"] = rate_limit_delay

    discover_models = entry.get("discover_models")
    if isinstance(discover_models, bool):
        normalized["discover_models"] = discover_models

    extra_body = entry.get("extra_body")
    if isinstance(extra_body, dict):
        normalized["extra_body"] = dict(extra_body)

    # Per-provider extra HTTP headers (proxies, gateways, custom auth).
    # Values may carry credentials (e.g. CF-Access-Client-Secret) — never
    # log them anywhere downstream.
    normalized_headers = normalize_extra_headers(entry.get("extra_headers"))
    if normalized_headers:
        normalized["extra_headers"] = normalized_headers

    ssl_ca_cert = entry.get("ssl_ca_cert")
    if isinstance(ssl_ca_cert, str) and ssl_ca_cert.strip():
        normalized["ssl_ca_cert"] = ssl_ca_cert.strip()

    ssl_verify = entry.get("ssl_verify")
    if isinstance(ssl_verify, bool):
        normalized["ssl_verify"] = ssl_verify
    elif isinstance(ssl_verify, str) and ssl_verify.strip():
        normalized["ssl_verify"] = ssl_verify.strip()

    return normalized


def providers_dict_to_custom_providers(providers_dict: Any) -> List[Dict[str, Any]]:
    """Normalize ``providers`` config entries into the legacy custom-provider shape."""
    if not isinstance(providers_dict, dict):
        return []

    custom_providers: List[Dict[str, Any]] = []
    for key, entry in providers_dict.items():
        if isinstance(entry, dict) and not is_provider_enabled(entry):
            continue
        normalized = _normalize_custom_provider_entry(entry, provider_key=str(key))
        if normalized is not None:
            custom_providers.append(normalized)

    return custom_providers


def get_compatible_custom_providers(
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return a deduplicated custom-provider view across legacy and v12+ config.

    ``custom_providers`` remains the on-disk legacy format, while ``providers``
    is the newer keyed schema.  Runtime and picker flows still need a single
    list-shaped view, but we should not materialise that compatibility layer
    back into config.toml because it duplicates entries in UIs.
    """
    if config is None:
        config = load_config()

    compatible: List[Dict[str, Any]] = []
    seen_provider_keys: set = set()
    seen_name_url_pairs: set = set()

    def _append_if_new(entry: Optional[Dict[str, Any]]) -> None:
        if entry is None:
            return
        provider_key = str(entry.get("provider_key", "") or "").strip().lower()
        name = str(entry.get("name", "") or "").strip().lower()
        base_url = str(entry.get("base_url", "") or "").strip().rstrip("/").lower()
        model = str(entry.get("model", "") or "").strip().lower()
        pair = (name, base_url, model)

        if provider_key and provider_key in seen_provider_keys:
            return
        if name and base_url and pair in seen_name_url_pairs:
            return

        compatible.append(entry)
        if provider_key:
            seen_provider_keys.add(provider_key)
        if name and base_url:
            seen_name_url_pairs.add(pair)

    custom_providers = config.get("custom_providers")
    if custom_providers is not None:
        if isinstance(custom_providers, list):
            for entry in custom_providers:
                _append_if_new(_normalize_custom_provider_entry(entry))
        elif isinstance(custom_providers, dict):
            # Keyed dict form (``custom_providers.custom.base_url = ...``) —
            # accepted everywhere for compatibility with the fork's examples
            # and declarative configs; normalize like the ``providers:``
            # keyed schema.
            for entry in providers_dict_to_custom_providers(custom_providers):
                _append_if_new(entry)
        else:
            return []

    for entry in providers_dict_to_custom_providers(config.get("providers")):
        _append_if_new(entry)

    return compatible


def _coerce_ssl_verify(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"false", "0", "no", "off"}:
            return False
        if lowered in {"true", "1", "yes", "on"}:
            return True
    return None


def get_custom_provider_tls_settings(
    base_url: str,
    custom_providers: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return TLS settings from a matching ``custom_providers`` / ``providers`` entry."""
    if custom_providers is None:
        try:
            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            custom_providers = []
    if not base_url or not isinstance(custom_providers, list):
        return {}

    target_url = normalize_route_base_url(base_url)
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        entry_url = normalize_route_base_url(entry.get("base_url"))
        if not entry_url or entry_url != target_url:
            continue
        out: Dict[str, Any] = {}
        ca = entry.get("ssl_ca_cert")
        if isinstance(ca, str) and ca.strip():
            out["ssl_ca_cert"] = ca.strip()
        verify = _coerce_ssl_verify(entry.get("ssl_verify"))
        if verify is not None:
            out["ssl_verify"] = verify
        return out
    return {}


def apply_custom_provider_tls_to_client_kwargs(
    client_kwargs: Dict[str, Any],
    base_url: str,
    custom_providers: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> None:
    """Attach per-provider TLS knobs to OpenAI client kwargs when matched."""
    tls = get_custom_provider_tls_settings(base_url, custom_providers, config)
    if tls.get("ssl_ca_cert"):
        client_kwargs["ssl_ca_cert"] = tls["ssl_ca_cert"]
    if "ssl_verify" in tls:
        client_kwargs["ssl_verify"] = tls["ssl_verify"]


def normalize_extra_headers(extra_headers: Any) -> Dict[str, str]:
    """Normalize a raw ``extra_headers`` value into a ``dict[str, str]``.

    Stringifies keys and values and drops entries whose value is ``None``.
    Returns ``{}`` for non-dict or empty inputs. This is the single shared
    normalizer for per-provider ``extra_headers`` across config normalization,
    runtime resolution, client construction, and live ``/models`` discovery.

    SECURITY: header values routinely carry credentials (Cloudflare Access
    service tokens, proxy auth, custom bearer schemes). Callers must never
    log the returned values.
    """
    if not isinstance(extra_headers, dict) or not extra_headers:
        return {}
    return {str(k): str(v) for k, v in extra_headers.items() if v is not None}


def get_custom_provider_extra_headers(
    base_url: str,
    custom_providers: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Return ``extra_headers`` from a matching ``providers`` / ``custom_providers`` entry.

    Matches the entry whose normalized route identity equals *base_url*,
    mirroring :func:`get_custom_provider_tls_settings`, and returns its
    ``extra_headers`` dict, or ``{}`` when no entry matches or declares none.

    SECURITY: header values routinely carry credentials (Cloudflare Access
    service tokens, proxy auth, custom bearer schemes). Callers must never
    log the returned values.
    """
    if custom_providers is None:
        try:
            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            custom_providers = []
    if not base_url or not isinstance(custom_providers, list):
        return {}

    target_url = normalize_route_base_url(base_url)
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        entry_url = normalize_route_base_url(entry.get("base_url"))
        if not entry_url or entry_url != target_url:
            continue
        headers = normalize_extra_headers(entry.get("extra_headers"))
        if headers:
            return headers
    return {}


def apply_custom_provider_extra_headers_to_client_kwargs(
    client_kwargs: Dict[str, Any],
    base_url: str,
    custom_providers: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> None:
    """Merge per-provider ``extra_headers`` onto OpenAI client ``default_headers``.

    Provider-specific headers win over provider/SDK defaults already present in
    ``client_kwargs`` — they are the most specific configuration level. No-op
    when the base_url matches no ``providers`` / ``custom_providers`` entry or
    the entry declares no headers.

    SECURITY: values may carry credentials — never log them.
    """
    extra_headers = get_custom_provider_extra_headers(base_url, custom_providers, config)
    if not extra_headers:
        return
    merged = dict(client_kwargs.get("default_headers") or {})
    merged.update(extra_headers)
    client_kwargs["default_headers"] = merged


def get_custom_provider_context_length(
    model: str,
    base_url: str,
    custom_providers: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Look up a per-model ``context_length`` override from ``custom_providers``.

    Matches any entry whose normalized route identity equals ``base_url`` and
    returns ``custom_providers[i].models.<model>.context_length`` if present and
    valid.  Returns ``None`` when no override applies.

    This is the single source of truth for custom-provider context overrides,
    used by:
      * ``AIAgent.__init__`` (startup resolution)
      * ``AIAgent.switch_model`` (mid-session ``/model`` switch)
      * ``son_of_anton_cli.model_switch.resolve_display_context_length`` (``/model`` confirmation display)
      * ``gateway.run._format_session_info`` (``/info`` display)
      * ``agent.model_metadata.get_model_context_length`` (when custom_providers is threaded through)

    Before this helper existed, the lookup was duplicated in ``run_agent.py``'s
    startup path only; every other path (notably ``/model`` switch) fell back
    to the 128K default.  See #15779.
    """
    if not model or not base_url:
        return None
    if custom_providers is None:
        try:
            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            if config is None:
                return None
            raw = config.get("custom_providers")
            custom_providers = raw if isinstance(raw, list) else []
    if not isinstance(custom_providers, list):
        return None

    target_url = normalize_route_base_url(base_url)
    if not target_url:
        return None

    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        entry_url = normalize_route_base_url(entry.get("base_url"))
        if not entry_url or entry_url != target_url:
            continue
        models = entry.get("models")
        if not isinstance(models, dict):
            continue
        model_cfg = models.get(model)
        if not isinstance(model_cfg, dict):
            continue
        raw_ctx = model_cfg.get("context_length")
        if raw_ctx is None:
            continue
        try:
            ctx = int(raw_ctx)
        except (TypeError, ValueError):
            continue
        if ctx > 0:
            return ctx
    return None


def get_custom_provider_reasoning_decl(
    model: str,
    base_url: Optional[str],
    custom_providers: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the per-model reasoning declaration for a custom-provider route.

    Mirrors :func:`get_custom_provider_context_length` (same route-identity
    matching, same entry shape) and reads ``models.<model>.reasoning_effort``
    (a single level) and/or ``models.<model>.reasoning_efforts`` (the wire
    vocabulary the endpoint actually accepts).

    Why this exists: ``custom`` is an open-ended provider that fronts
    anything OpenAI-compatible, and those endpoints disagree on the
    ``reasoning_effort`` vocabulary. A Qwen3-on-vLLM route served through
    LiteLLM accepts exactly ``none/low/medium/xhigh`` and 400s on
    ``high``/``max``/``minimal``/``ultra`` (verified live). The custom
    provider profile therefore clamps the outgoing effort to the declared
    set instead of the widest OpenAI-compat vocabulary, and
    ``resolve_reasoning_config`` honours the per-model effort here — the
    same place users already declare ``context_length`` — so a value the
    user wrote next to ``context_length`` actually takes effect.

    Returns ``{"effort": str, "efforts": tuple[str, ...] | None}`` — either
    field absent when not declared (callers treat that as "no opinion").
    """
    out: Dict[str, Any] = {}
    if not model:
        return out
    if custom_providers is None:
        try:
            if config is None:
                config = load_config_readonly()
            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            return out
    if not isinstance(custom_providers, list):
        return out

    target_url = normalize_route_base_url(base_url) if base_url else ""
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        entry_url = normalize_route_base_url(entry.get("base_url"))
        if not entry_url:
            continue
        # When the caller named a route, match it; otherwise accept the
        # entry (the only one a single-custom-provider config has).
        if target_url and entry_url != target_url:
            continue
        models = entry.get("models")
        if not isinstance(models, dict):
            continue
        model_cfg = models.get(model)
        if not isinstance(model_cfg, dict):
            continue
        effort = model_cfg.get("reasoning_effort")
        if effort is not None and not isinstance(effort, bool) and str(effort).strip():
            out["effort"] = str(effort).strip()
        efforts = model_cfg.get("reasoning_efforts")
        if isinstance(efforts, (list, tuple)):
            levels = [
                str(level).strip().lower()
                for level in efforts
                if isinstance(level, (str, int)) and str(level).strip()
            ]
            levels = [level for level in levels if level]
            if levels:
                out["efforts"] = tuple(levels)
        break
    return out


def get_custom_provider_chat_template_kwargs(
    model: str,
    base_url: Optional[str],
    custom_providers: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return ``custom_providers.<name>.models.<model>.chat_template_kwargs``.

    Same route matching as :func:`get_custom_provider_reasoning_decl`. The
    table is passed through to the endpoint as
    ``extra_body.chat_template_kwargs`` (vLLM / llama.cpp / SGLang render
    the chat template with it — e.g. Qwen3's ``enable_thinking``). Empty
    dict when undeclared or malformed.
    """
    if not model:
        return {}
    if custom_providers is None:
        try:
            if config is None:
                config = load_config_readonly()
            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            return {}
    if not isinstance(custom_providers, list):
        return {}

    target_url = normalize_route_base_url(base_url) if base_url else ""
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        entry_url = normalize_route_base_url(entry.get("base_url"))
        if not entry_url:
            continue
        if target_url and entry_url != target_url:
            continue
        models = entry.get("models")
        if not isinstance(models, dict):
            continue
        model_cfg = models.get(model)
        if not isinstance(model_cfg, dict):
            continue
        kwargs = model_cfg.get("chat_template_kwargs")
        return dict(kwargs) if isinstance(kwargs, dict) else {}
    return {}


def get_custom_provider_model_capability(
    model: str,
    base_url: str,
    capability: str,
    custom_providers: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[bool]:
    """Return an explicit boolean capability for one custom-provider model.

    Matching is scoped to the normalized route and exact runtime model id so
    aliases can declare capabilities without changing the id sent upstream.
    Missing or non-boolean declarations return ``None``.
    """
    if not model or not base_url or not capability:
        return None
    if custom_providers is None:
        try:
            if config is None:
                # Read-only path: this helper never mutates the entries it
                # scans, and get_compatible_custom_providers shallow-copies
                # each entry before normalizing, so the no-deepcopy cache is
                # safe here (~135us saved per call on the blank-stub paths).
                config = load_config_readonly()
            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            return None
    if not isinstance(custom_providers, list):
        return None

    target_url = normalize_route_base_url(base_url)
    if not target_url:
        return None

    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        entry_url = normalize_route_base_url(entry.get("base_url"))
        if not entry_url or entry_url != target_url:
            continue
        models = entry.get("models")
        if not isinstance(models, dict):
            continue
        model_cfg = models.get(model)
        if not isinstance(model_cfg, dict):
            continue
        value = model_cfg.get(capability)
        if isinstance(value, bool):
            return value
    return None


# =============================================================================
# Config structure validation
# =============================================================================

# Fields that are valid at root level of config.toml.
# DEFAULT_CONFIG is the single source of truth for documented roots; keep this
# set derived so new defaults (security, browser, …) are accepted
# automatically. A few optional/legacy roots are valid on disk but intentionally
# absent from DEFAULT_CONFIG (omitted when unused / alternate schema forms).
_EXTRA_KNOWN_ROOT_KEYS = {
    "custom_providers",  # legacy list form; modern equivalent is providers: {}
    "fallback_model",    # optional single dict or chain list; omitted when disabled
    "mcp_servers",       # MCP server definitions written by setup/tools flows
    # Roots read from the raw user TOML (or written by our own flows) that are
    # intentionally absent from DEFAULT_CONFIG:
    "plugins",           # plugin enable/disable lists (son_of_anton_cli/plugins_cmd.py)
    "smart_model_routing",   # written by the setup wizard (son_of_anton_cli/setup.py)
    "platform_toolsets",     # written by the setup wizard (son_of_anton_cli/setup.py)
    "known_plugin_toolsets", # written/read by son_of_anton_cli/tools_config.py toolset-save flow
    "known_builtin_toolsets",  # ditto — which builtin toolsets a platform's checklist has offered
    "session_reset",         # top-level form read by gateway/config.py + setup
    "group_sessions_per_user",   # top-level form bridged by gateway/config.py
    "thread_sessions_per_user",  # top-level form bridged by gateway/config.py
    "reset_triggers",            # top-level form bridged by gateway/config.py
    "always_log_local",          # top-level form bridged by gateway/config.py
    "profile_routes",        # top-level form accepted alongside gateway.profile_routes
    "platforms",             # top-level per-platform map merged by gateway/config.py
    "require_mention",       # top-level convenience form honored by the gateway (#3979)
    "unauthorized_dm_behavior",  # top-level form read by gateway/config.py
    "signal",            # Signal settings bridged to env vars by gateway/config.py
}
_KNOWN_ROOT_KEYS = frozenset(DEFAULT_CONFIG.keys()) | _EXTRA_KNOWN_ROOT_KEYS

# Valid fields inside a custom_providers list entry

# Fields that look like they should be inside custom_providers, not at root
_CUSTOM_PROVIDER_LIKE_FIELDS = {"base_url", "api_key", "rate_limit_delay", "api_mode"}


@dataclass
class ConfigIssue:
    """A detected config structure problem."""

    severity: str  # "error", "warning"
    message: str
    hint: str


def validate_config_structure(config: Optional[Dict[str, Any]] = None) -> List["ConfigIssue"]:
    """Validate config.toml structure and return a list of detected issues.

    Catches common TOML formatting mistakes that produce confusing runtime
    errors (like "Unknown provider") instead of clear diagnostics.

    Can be called with a pre-loaded config dict, or will load from disk.
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            return [ConfigIssue("error", "Could not load config.toml", "Fix it, or regenerate it from the Nix module's settings")]

    issues: List[ConfigIssue] = []

    # ── custom_providers: list or keyed-dict form are both valid ─────────
    cp = config.get("custom_providers")
    if cp is not None:
        if isinstance(cp, dict):
            # Keyed dict form (custom_providers.custom.base_url = ...) —
            # normalized by get_compatible_custom_providers at runtime.
            pass
        elif isinstance(cp, list):
            # Validate each entry in the list
            for i, entry in enumerate(cp):
                if not isinstance(entry, dict):
                    issues.append(ConfigIssue(
                        "warning",
                        f"custom_providers[{i}] is not a dict (got {type(entry).__name__})",
                        "Each entry should have at minimum: name, base_url",
                    ))
                    continue
                if not entry.get("name"):
                    issues.append(ConfigIssue(
                        "warning",
                        f"custom_providers[{i}] is missing 'name' field",
                        "Add a name, e.g.: name: my-provider",
                    ))
                if not entry.get("base_url"):
                    issues.append(ConfigIssue(
                        "warning",
                        f"custom_providers[{i}] is missing 'base_url' field",
                        "Add the API endpoint URL, e.g.: base_url: https://api.example.com/v1",
                    ))

    # ── fallback_model: single dict OR list of dicts (chain) ─────────────
    fb = config.get("fallback_model")
    if fb is not None:
        if isinstance(fb, list):
            # Chain fallback — validate each entry
            for i, entry in enumerate(fb):
                if not isinstance(entry, dict):
                    issues.append(ConfigIssue(
                        "error",
                        f"fallback_model[{i}] should be a dict, got {type(entry).__name__}",
                        "Each entry needs provider + model",
                    ))
                else:
                    if not entry.get("provider"):
                        issues.append(ConfigIssue(
                            "warning",
                            f"fallback_model[{i}] is missing 'provider' field",
                            "Add: provider: openrouter (or another provider)",
                        ))
                    if not entry.get("model"):
                        issues.append(ConfigIssue(
                            "warning",
                            f"fallback_model[{i}] is missing 'model' field",
                            "Add: model: <model-name>",
                        ))
        elif not isinstance(fb, dict):
            issues.append(ConfigIssue(
                "error",
                f"fallback_model should be a dict with 'provider' and 'model', got {type(fb).__name__}",
                "Change to:\n"
                "  fallback_model:\n"
                "    provider: openrouter\n"
                "    model: anthropic/claude-sonnet-4",
            ))
        elif fb:
            if not fb.get("provider"):
                issues.append(ConfigIssue(
                    "warning",
                    "fallback_model is missing 'provider' field — fallback will be disabled",
                    "Add: provider: openrouter (or another provider)",
                ))
            if not fb.get("model"):
                issues.append(ConfigIssue(
                    "warning",
                    "fallback_model is missing 'model' field — fallback will be disabled",
                    "Add: model: anthropic/claude-sonnet-4 (or another model)",
                ))

    # ── Check for fallback_model accidentally nested inside custom_providers ──
    if isinstance(cp, dict) and "fallback_model" not in config and "fallback_model" in (cp or {}):
        issues.append(ConfigIssue(
            "error",
            "fallback_model appears inside custom_providers instead of at root level",
            "Move fallback_model to the top level of config.toml (no indentation)",
        ))

    # ── model section: should exist when custom_providers is configured ──
    model_cfg = config.get("model")
    if cp and not model_cfg:
        issues.append(ConfigIssue(
            "warning",
            "custom_providers defined but no 'model' section — Son of Anton won't know which provider to use",
            "Add a model section:\n"
            "  model:\n"
            "    provider: custom\n"
            "    default: your-model-name\n"
            "    base_url: https://...",
        ))

    # ── Root-level keys that look misplaced ──────────────────────────────
    # Only provider-like fields (base_url, api_key, …) are flagged. Arbitrary
    # unknown top-level keys are deliberately NOT warned about: top-level
    # scalars are bridged into os.environ (gateway/run.py, son-of-anton send) so
    # users can feed external apps env-style keys from config.toml
    # — a closed-world allowlist can never enumerate those.
    for key in config:
        if key.startswith("_"):
            continue
        if key not in _KNOWN_ROOT_KEYS and key in _CUSTOM_PROVIDER_LIKE_FIELDS:
            issues.append(ConfigIssue(
                "warning",
                f"Root-level key '{key}' looks misplaced — should it be under 'model:' or inside a 'custom_providers' entry?",
                f"Move '{key}' under the appropriate section",
            ))

    return issues


def print_config_warnings(config: Optional[Dict[str, Any]] = None) -> None:
    """Print config structure warnings to stderr at startup.

    Called early in CLI and gateway init so users see problems before
    they hit cryptic "Unknown provider" errors.  Prints nothing if
    config is healthy.
    """
    try:
        issues = validate_config_structure(config)
    except Exception:
        return
    if not issues:
        return

    lines = ["\033[33m⚠ Config issues detected in config.toml:\033[0m"]
    for ci in issues:
        marker = "\033[31m✗\033[0m" if ci.severity == "error" else "\033[33m⚠\033[0m"
        lines.append(f"  {marker} {ci.message}")
    lines.append("  \033[2mRun 'son-of-anton doctor' for fix suggestions.\033[0m")
    sys.stderr.write("\n".join(lines) + "\n\n")


def warn_deprecated_cwd_env_vars() -> None:
    """Warn if MESSAGING_CWD or TERMINAL_CWD is set in .env instead of config.toml.

    These env vars are deprecated — the canonical setting is terminal.cwd
    in config.toml.  Read the file rather than ``os.environ`` because runtime
    config bridges and session restoration legitimately set ``TERMINAL_CWD``.
    Prints a migration hint to stderr.
    """
    try:
        env_map = load_env()
    except Exception:
        return

    messaging_cwd = str(env_map.get("MESSAGING_CWD") or "").strip()
    terminal_cwd_env = str(env_map.get("TERMINAL_CWD") or "").strip()

    lines: list[str] = []
    if messaging_cwd:
        lines.append(
            f"  \033[33m⚠\033[0m MESSAGING_CWD={messaging_cwd} found in .env — "
            f"this is deprecated."
        )
    if terminal_cwd_env:
        lines.append(
            f"  \033[33m⚠\033[0m TERMINAL_CWD={terminal_cwd_env} found in .env — "
            f"this is deprecated."
        )
    if lines:
        from son_of_anton_constants import display_son_of_anton_home

        hint_path = display_son_of_anton_home()
        lines.insert(0, "\033[33m⚠ Deprecated .env settings detected:\033[0m")
        lines.append(
            "  \033[2mMove to config.toml instead:  "
            "terminal:\\n    cwd: /your/project/path\033[0m"
        )
        lines.append(
            f"  \033[2mThen remove the old entries from {hint_path}/.env\033[0m"
        )
        sys.stderr.write("\n".join(lines) + "\n\n")


def _merge_partial_save(raw: dict, override: dict) -> dict:
    """Merge *override* over *raw* for partial ``save_config`` writes.

    Top-level sections omitted from *override* are preserved from *raw*.
    Shared top-level dict sections are deep-merged so a caller can update one
    nested key without dropping sibling keys from disk. Intentional key
    removals within a section are not supported here — migration writes must
    route through ``_persist_migration`` with a full ``read_raw_config()`` dict
    instead.
    """
    result = copy.deepcopy(override)
    for key, value in raw.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
        elif isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(value, result[key])
    return result


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, preserving nested defaults.

    Keys in *override* take precedence. If both values are dicts the merge
    recurses, so a user who overrides only one nested subkey will keep the
    sibling defaults intact.

    An empty section key in config.toml (``terminal:`` with no value) parses
    as TOML ``None``; treating that as an override would replace the entire
    default dict with ``None`` and crash every downstream consumer that
    expects a mapping (#58277). A ``None`` override of a dict default is
    ignored — same as the key being absent.
    """
    result = base.copy()
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        elif key in result and isinstance(result[key], dict) and value is None:
            continue
        else:
            result[key] = value
    return result


def _env_expand_match(m: re.Match) -> str:
    """Expand one ``${...}`` config reference.

    Two accepted shapes, matching what MCP server config already resolves
    (``tools/mcp_tool.py::_env_ref_name``):

    * ``${VAR}`` — legacy bare name, resolved via ``os.environ``.
    * ``${env:VAR}`` — Cursor-style SecretRef, same resolution after the
      ``env:`` prefix is stripped.  Before this, the prefixed form worked in
      MCP config but stayed a literal string in config.toml — a confusing
      half-support.

    Other SecretRef sources (``file:``, ``bitwarden:``, ``vault:``, ...)
    are NOT resolved here — external secret backends inject their values
    into the environment at startup (the ``secrets:`` block), so a config
    ref only ever needs the env shape.  Unknown prefixes warn once and stay
    verbatim so callers can detect them.
    """
    raw = m.group(0)
    inner = m.group(1).strip()
    if inner.startswith("env:"):
        name = inner[len("env:"):].strip()
        if not name:
            return raw
        val = os.environ.get(name)
        if val is not None:
            return val
        logger.warning(
            "Config ref %r: %s is not set (check ~/.son-of-anton/.env); "
            "keeping the literal placeholder", raw, name,
        )
        return raw
    if ":" in inner and re.match(r"^[a-z][a-z0-9_-]*:", inner):
        # Looks like a SecretRef with a non-env source.  Values from vault
        # backends arrive via the secrets: block as env vars — point there
        # instead of silently treating "bitwarden:FOO" as a var named
        # "bitwarden:FOO".
        logger.warning(
            "Config ref %r uses source %r which is not resolvable in "
            "config.toml — external secret sources inject env vars at "
            "startup, so reference the variable as ${env:NAME} instead",
            raw, inner.split(":", 1)[0],
        )
        return raw
    # Legacy ``${VAR}`` — bare name.
    return os.environ.get(inner, raw)


def _env_ref_var_name(ref: str) -> Optional[str]:
    """Normalize a ``${...}`` body to the env-var name it reads, or None
    when the ref uses a non-env source and never touches the environment."""
    ref = ref.strip()
    if ref.startswith("env:"):
        name = ref[len("env:"):].strip()
        return name or None
    if ":" in ref and re.match(r"^[a-z][a-z0-9_-]*:", ref):
        return None
    return ref


def _expand_env_vars(obj):
    """Recursively expand ``${VAR}`` / ``${env:VAR}`` references in config
    values.

    Only string values are processed; dict keys, numbers, booleans, and
    None are left untouched.  Unresolved references (variable not in
    ``os.environ``) are kept verbatim so callers can detect them.
    """
    if isinstance(obj, str):
        return re.sub(r"\${([^}]+)}", _env_expand_match, obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj


def _env_ref_snapshot(obj, snapshot=None):
    """Map every ``${VAR}`` / ``${env:VAR}`` name referenced in config values
    to its current ``os.environ`` value (``None`` when unset).

    Stored alongside cached ``load_config()`` results so a cache hit can
    detect that the cached expansion was made against a *different*
    environment — e.g. a ``load_config()`` that ran before
    ``load_son_of_anton_dotenv()`` populated the process env, or an env var
    rotated in-process after the first load. File mtime/size alone cannot
    see either case (#58514).

    ``${env:VAR}`` refs are tracked under the real variable name; refs
    with a non-env source prefix never read the environment, so they are
    excluded from the snapshot.
    """
    if snapshot is None:
        snapshot = {}
    if isinstance(obj, str):
        for raw in re.findall(r"\${([^}]+)}", obj):
            name = _env_ref_var_name(raw)
            if name is not None:
                snapshot[name] = os.environ.get(name)
    elif isinstance(obj, dict):
        for value in obj.values():
            _env_ref_snapshot(value, snapshot)
    elif isinstance(obj, list):
        for item in obj:
            _env_ref_snapshot(item, snapshot)
    return snapshot


def _items_by_unique_name(items):
    """Return a name-indexed dict only when all items have unique string names."""
    if not isinstance(items, list):
        return None
    indexed = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return None
        name = item["name"]
        if name in indexed:
            return None
        indexed[name] = item
    return indexed


def _preserve_env_ref_templates(current, raw, loaded_expanded=None):
    """Restore raw ``${VAR}`` templates when a value is otherwise unchanged.

    ``load_config()`` expands env refs for runtime use. When a caller later
    persists that config after modifying some unrelated setting, keep the
    original on-disk template instead of writing the expanded plaintext
    secret back to ``config.toml``.

    Prefer preserving the raw template when ``current`` still matches either
    the value previously returned by ``load_config()`` for this config path or
    the current environment expansion of ``raw``. This handles env-var
    rotation between load and save while still treating mixed literal/template
    string edits as caller-owned once their rendered value diverges.
    """
    if isinstance(current, str) and isinstance(raw, str) and re.search(r"\${[^}]+}", raw):
        if current == raw:
            return raw
        if isinstance(loaded_expanded, str) and current == loaded_expanded:
            return raw
        if _expand_env_vars(raw) == current:
            return raw
        return current

    if isinstance(current, dict) and isinstance(raw, dict):
        return {
            key: _preserve_env_ref_templates(
                value,
                raw.get(key),
                loaded_expanded.get(key) if isinstance(loaded_expanded, dict) else None,
            )
            for key, value in current.items()
        }

    if isinstance(current, list) and isinstance(raw, list):
        # Prefer matching named config objects (e.g. custom_providers) by name
        # so harmless reordering doesn't drop the original template. If names
        # are duplicated, fall back to positional matching instead of silently
        # shadowing one entry.
        current_by_name = _items_by_unique_name(current)
        raw_by_name = _items_by_unique_name(raw)
        loaded_by_name = _items_by_unique_name(loaded_expanded)
        if current_by_name is not None and raw_by_name is not None:
            return [
                _preserve_env_ref_templates(
                    item,
                    raw_by_name.get(item.get("name")),
                    loaded_by_name.get(item.get("name")) if loaded_by_name is not None else None,
                )
                for item in current
            ]
        return [
            _preserve_env_ref_templates(
                item,
                raw[index] if index < len(raw) else None,
                loaded_expanded[index]
                if isinstance(loaded_expanded, list) and index < len(loaded_expanded)
                else None,
            )
            for index, item in enumerate(current)
        ]

    return current


def _explicit_config_paths(config: Dict[str, Any]) -> Set[Tuple[str, ...]]:
    """Return leaf paths explicitly present in a raw config dict.

    Computed on the **raw** (un-normalized, un-expanded) config so that
    values injected by normalisation (e.g. ``agent.max_turns`` from
    ``DEFAULT_CONFIG``) are not mistakenly treated as user-set.

    Used by ``save_config`` to build the *preserve* set passed to
    ``_strip_default_values`` so only user-authored keys survive the
    defaults-strip pass.
    """
    paths: Set[Tuple[str, ...]] = set()

    def _walk(value: Any, path: Tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                _walk(child, path + (key,))
            return
        if path:
            paths.add(path)

    _walk(config, ())
    return paths


def _strip_default_values(
    config: Dict[str, Any],
    defaults: Dict[str, Any] = DEFAULT_CONFIG,
    preserve_keys: Optional[Set[Tuple[str, ...]]] = None,
) -> Dict[str, Any]:
    """Return *config* without keys whose values match *defaults*.

    Keys in *preserve_keys* (explicitly present in the user's raw config,
    before any normalisation) are always kept even when they equal the
    default, so user-set values such as ``memory.user_char_limit: 2200``
    survive a ``save_config`` round-trip.

    Nested dicts whose every child is stripped are removed entirely so
    default-only subtrees (e.g. ``gateway``) never bloat ``config.toml``
    when the user has nothing to say about them.
    """
    preserve_keys = set(preserve_keys or ())

    def _strip(value: Any, default: Any, path: Tuple[str, ...]) -> Any:
        if path in preserve_keys:
            return copy.deepcopy(value)

        if isinstance(value, dict) and value:
            default_dict = default if isinstance(default, dict) else {}
            stripped: Dict[str, Any] = {}
            for key, child in value.items():
                child_default = default_dict.get(key)
                stripped_child = _strip(child, child_default, path + (key,))
                if stripped_child is not None:
                    stripped[key] = stripped_child
            if stripped:
                return stripped
            # Entire subtree stripped — remove it
            return None

        if value == default:
            return None

        return copy.deepcopy(value)

    result: Dict[str, Any] = {}
    for key, value in config.items():
        stripped = _strip(value, defaults.get(key), (key,))
        if stripped is not None:
            result[key] = stripped
    return result


def split_model_config_default(raw_default: Any) -> tuple[str, str]:
    """Canonicalize a config ``model.default``/``model.model`` value.

    A dict-valued default (``model.default: {provider: ..., model: ...}``)
    pairs the model string with the provider it must be routed through. The
    dict is flattened here at the shared boundary so both halves stay
    together through ``SonOfAntonCLI`` construction: the model becomes a plain
    string and the provider is returned explicitly instead of being lost to
    the outer merged ``model.provider`` default (often ``"auto"``, which
    runtime resolution treats as authoritative and would otherwise route the
    model through the wrong active provider).

    Returns ``(model, provider)``; both are ``""`` when nothing is usable.
    """
    if isinstance(raw_default, dict):
        provider = str(raw_default.get("provider") or "").strip()
        model = raw_default.get("model") or raw_default.get("default")
        return (str(model or "").strip(), provider)
    return (str(raw_default or "").strip(), "")


def _normalize_root_model_keys(config: Dict[str, Any]) -> Dict[str, Any]:
    """Move stale root-level provider/base_url/context_length into model section.

    Some users (or older code) placed ``provider:``, ``base_url:``, or
    ``context_length:`` at the config root instead of inside ``model:``.
    These root-level keys are only used as a fallback when the corresponding
    ``model.*`` key is empty — they never override an existing value.
    After migration the root-level keys are removed so they can't cause
    confusion on subsequent loads.

    Also aliases ``api_base`` → ``base_url`` (issue #8919). ``api_base`` is the
    intuitive name OpenAI-SDK / LiteLLM users reach for, and ``son-of-anton config set``
    blindly accepts any dotted key — so ``model.api_base`` got written, confirmed,
    and then silently ignored by the runtime resolver (which reads only
    ``model.base_url``), causing requests to fall back to OpenRouter. We migrate
    the alias to the canonical key (fallback-only — never override an explicit
    ``base_url``) and drop the alias so it can't confuse later loads.

    Finally, canonicalizes the model-id key to ``model.default`` (issue #34500).
    The runtime resolver and ~14 other readers select the chat model via
    ``model.default``; ``model.model`` was already aliased inline at some sites
    but ``model.name`` was not, so a custom-provider config like
    ``model: {name: <id>, provider: <custom>}`` resolved to an empty model and
    the API request went out with ``model=`` (HTTP 400 from OpenAI-compatible
    backends) — while display paths (``son-of-anton status``/``dump``) read ``name``
    and *showed* the model, making the failure silent. Normalizing here (the
    single load/save chokepoint) means every reader, present and future, sees a
    populated ``default`` and the stale alias is migrated out of config.toml on
    the next save. Precedence: ``default`` > ``model`` > ``name`` (never
    overrides an explicit ``default``, so existing configs are unaffected).
    """
    # Only act if there's something to migrate: root-level keys, an api_base
    # alias, or a model dict whose id lives under a non-canonical key.
    model_in = config.get("model")
    model_has_alias = isinstance(model_in, dict) and model_in.get("api_base")
    # A dict-valued ``default``/``model`` (``{provider: ..., model: ...}``)
    # must be flattened into ``default`` (string) + ``provider`` here at the
    # single load/save chokepoint, so every reader (doctor, status, fallback
    # picker, prompt-size, context-switch guard, …) sees plain strings instead
    # of a nested dict that crashes ``.strip()``/``.lower()`` or routes the
    # model through the wrong provider.
    _has_nested_default = isinstance(model_in, dict) and (
        isinstance(model_in.get("default"), dict)
        or isinstance(model_in.get("model"), dict)
        or isinstance(model_in.get("name"), dict)
    )
    # A model dict needs canonicalization if its id lives under a non-canonical
    # key (``model``/``name``) — either because ``default`` is empty (we must
    # promote the alias) or because ``default`` is set but a stale alias still
    # lingers (we must drop it so config.toml ends up canonical).
    model_needs_canon = isinstance(model_in, dict) and (
        model_in.get("model") or model_in.get("name")
    )
    has_root = any(
        config.get(k) for k in ("provider", "base_url", "context_length", "api_base")
    )
    if not has_root and not model_has_alias and not model_needs_canon and not _has_nested_default:
        return config

    config = dict(config)
    model = config.get("model")
    if not isinstance(model, dict):
        model = {"default": model} if model else {}
    else:
        model = dict(model)
    config["model"] = model

    # Flatten a dict-valued ``model.default``/``model.model``:
    # ``{provider: <p>, model: <m>}`` -> ``default: "<m>"`` and, when no
    # explicit ``model.provider`` is set, ``provider: "<p>"``. The nested
    # provider must win over the merged default ``"auto"`` (which runtime
    # resolution treats as authoritative and would otherwise route the model
    # through the wrong active provider), but never over an explicitly
    # configured outer provider.
    for _key in ("default", "model", "name"):
        _val = model.get(_key)
        if isinstance(_val, dict):
            _nested_model = _val.get("model") or _val.get("default")
            _nested_provider = str(_val.get("provider") or "").strip()
            model[_key] = str(_nested_model or "").strip()
            if _nested_provider:
                _outer_provider = str(model.get("provider") or "").strip()
                if not _outer_provider or _outer_provider == "auto":
                    model["provider"] = _nested_provider

    for key in ("provider", "base_url", "context_length"):
        root_val = config.get(key)
        if root_val and not model.get(key):
            model[key] = root_val
        config.pop(key, None)

    # api_base is an alias for base_url, at the root OR inside model.
    for alias_val in (config.get("api_base"), model.get("api_base")):
        if alias_val and not model.get("base_url"):
            model["base_url"] = alias_val
    config.pop("api_base", None)
    model.pop("api_base", None)

    # Canonicalize the model id to ``default``. ``model`` and ``name`` are
    # last-resort aliases (in that order) — only consulted when ``default`` is
    # empty, then dropped so later loads/saves can't reintroduce the ambiguity.
    if not (model.get("default") or ""):
        alias = model.get("model") or model.get("name")
        if alias:
            model["default"] = alias
    if model.get("default"):
        # Drop the now-redundant aliases so config.toml ends up canonical.
        model.pop("model", None)
        model.pop("name", None)

    return config


def _normalize_max_turns_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize legacy root-level max_turns into agent.max_turns.

    Only injects the schema default when the user actually set max_turns
    somewhere (root level or under ``agent``).  A bare ``load_config()``
    call that passes the result straight to ``save_config()`` should not
    materialise ``agent.max_turns`` in config.toml when the user never set
    it — that makes the default sticky and blocks future schema changes.
    """
    config = dict(config)
    agent_config = dict(config.get("agent") or {})

    had_root = "max_turns" in config
    had_agent = "max_turns" in agent_config

    if had_root and not had_agent:
        agent_config["max_turns"] = config["max_turns"]

    # Only inject the default when the user explicitly set max_turns
    # (either root-level or under agent).  Otherwise leave it absent so
    # save_config can omit it and the schema default fills in at runtime.
    if not had_root and not had_agent:
        pass  # deliberately do not inject DEFAULT_CONFIG default
    elif "max_turns" not in agent_config:
        agent_config["max_turns"] = DEFAULT_CONFIG["agent"]["max_turns"]

    config["agent"] = agent_config
    config.pop("max_turns", None)
    return config


def is_provider_enabled(provider_cfg: Optional[Dict[str, Any]]) -> bool:
    """Return whether a ``providers.<name>`` config block is enabled.

    A provider is enabled by default. Only an explicit ``enabled: false`` in
    the block hides it from the model picker, ``/models`` listings, the
    runtime resolver and the doctor / status output.

    Backward-compat: configs without the ``enabled`` key keep working as
    before — the default is ``True``.

    Pass any non-dict (None, list, string) and you get ``True`` too, so
    malformed entries don't disappear silently; they'll still be flagged
    by the existing validation paths.
    """
    if not isinstance(provider_cfg, dict):
        return True
    flag = provider_cfg.get("enabled", True)
    if isinstance(flag, bool):
        return flag
    # TOML can produce strings for "true"/"false" depending on quoting.
    if isinstance(flag, str):
        return flag.strip().lower() not in {"false", "0", "no", "off"}
    return bool(flag)


# Sentinel used when the user requests an unlimited turn budget.
# ``sys.maxsize`` (9,223,372,036,854,775,807) is chosen so it:
#   - survives the ``str() → int()`` round-trip through the SON_OF_ANTON_MAX_ITERATIONS
#     env-var bridge in gateway/run.py,
#   - works correctly in every ``<``, ``>=``, and ``remaining = max - used``
#     comparison in agent/iteration_budget.py and agent/conversation_loop.py
#     without requiring those call sites to learn about a special "unlimited"
#     value, and
#   - is large enough that no real conversation will ever reach it (a turn
#     takes seconds; 9.2e18 turns would take ~10^11 years).
TURN_LIMIT_UNLIMITED = sys.maxsize

# String spellings that mean "no limit".  Lowercased, whitespace-stripped
# before comparison so ``"None"``, ``" unlimited "`` etc. all match.
_UNLIMITED_SPELLINGS = frozenset({
    "none", "null", "unlimited", "infinite", "infinity", "inf",
    "∞", "-1", "0",
})


def resolve_turn_limit(raw: Any, default: int = TURN_LIMIT_UNLIMITED) -> int:
    """Normalize a raw ``agent.max_turns`` value into an int iteration cap.

    Accepts:
      - ``int`` / ``float`` → ``int(raw)`` (floats truncated; values ≤ 0 mean
        "no limit" → :data:`TURN_LIMIT_UNLIMITED`).
      - numeric string (``"120"``) → ``int(raw)``.
      - ``"none"`` / ``"null"`` / ``"unlimited"`` / ``"infinite"`` /
        ``"infinity"`` / ``"inf"`` / ``"∞"`` / ``"-1"`` / ``"0"``
        (case-insensitive, whitespace-tolerant) → :data:`TURN_LIMIT_UNLIMITED`.
      - TOML ``None`` / ``null`` / absent value → ``default`` (which is itself
        :data:`TURN_LIMIT_UNLIMITED` — max_turns is unlimited by default).
      - Anything unparseable → ``default`` (with a debug log).

    The returned int is always ≥ 1, so loop conditions like
    ``while api_call_count < agent.max_iterations`` behave correctly even when
    the default (unlimited) path is taken.

    This is the single normalization point for the turn-limit value type.
    Config-reading sites (cli.py, gateway/run.py, cron/scheduler.py) call this
    instead of bare ``int(...)``, so ``agent.max_turns: none`` in config.toml
    becomes a first-class supported spelling of "unlimited". max_turns is
    unlimited unless the user sets an explicit positive integer cap.
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        # bool is a subclass of int; reject it explicitly so True/False don't
        # silently become 1/0.
        return default
    if isinstance(raw, (int, float)):
        n = int(raw)
        if n <= 0:
            return TURN_LIMIT_UNLIMITED
        return n
    if isinstance(raw, str):
        s = raw.strip().lower()
        if not s:
            return default
        if s in _UNLIMITED_SPELLINGS:
            return TURN_LIMIT_UNLIMITED
        try:
            n = int(s)
        except ValueError:
            try:
                n = int(float(s))
            except ValueError:
                logger.debug("resolve_turn_limit: unparseable value %r → default %d", raw, default)
                return default
        if n <= 0:
            return TURN_LIMIT_UNLIMITED
        return n
    # Unknown type (list, dict, …) — don't crash the agent over a bad config.
    logger.debug("resolve_turn_limit: unsupported type %s (%r) → default %d", type(raw).__name__, raw, default)
    return default


def cfg_get(cfg: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    """Traverse nested dict keys safely, returning ``default`` on any miss.

    Canonical helper for the ``cfg.get("X", {}).get("Y", default)`` pattern
    that appears 50+ times across the codebase. Handles three common gotchas
    in one place:

      1. Missing intermediate keys (returns ``default``, no KeyError).
      2. An intermediate value that's not a dict (e.g. a user wrote a string
         where a section was expected). Returns ``default`` instead of
         AttributeError on ``.get()``.
      3. ``cfg is None`` (callers sometimes pass ``load_config() or None``).

    Named ``cfg_get`` rather than ``cfg_path`` to avoid shadowing the
    ubiquitous ``cfg_path = _son_of_anton_home / "config.toml"`` local variable
    that appears in gateway/run.py, cron/scheduler.py, main.py, etc.

    Explicit ``None`` values are returned as-is (matches ``dict.get(key,
    default)`` semantics — ``default`` is only returned when the key is
    *absent*, not when it's present but set to ``None``).

    Examples:
        >>> cfg_get({"agent": {"reasoning_effort": "high"}}, "agent", "reasoning_effort")
        'high'
        >>> cfg_get({}, "agent", "reasoning_effort", default="medium")
        'medium'
        >>> cfg_get({"agent": "oops_a_string"}, "agent", "reasoning_effort", default="low")
        'low'
        >>> cfg_get(None, "anything", default=42)
        42
        >>> cfg_get({"a": {"b": None}}, "a", "b", default="def")  # explicit None preserved
        >>> cfg_get({"a": {"b": False}}, "a", "b", default=True)  # falsy values preserved
        False
    """
    if not isinstance(cfg, dict):
        return default
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict):
            return default
        if key not in node:
            return default
        node = node[key]
    return node


# Back-compat alias — canonical set lives in son_of_anton_cli.personality.
from son_of_anton_cli.personality import NEUTRAL_PERSONALITY_NAMES as _NEUTRAL_PERSONALITY_NAMES  # noqa: F401


def render_personality_prompt(value: Any) -> str:
    """Render a string or structured personality definition to a prompt."""
    from son_of_anton_cli.personality import render_personality_prompt as _render

    return _render(value)


def resolve_ephemeral_system_prompt_from_config(cfg: Optional[Dict[str, Any]]) -> str:
    """Resolve the session overlay from config.toml.

    ``display.personality`` is the selected named personality and wins when set.
    Otherwise fall back to the user-owned ``agent.system_prompt``. Callers should
    still prefer ``SON_OF_ANTON_EPHEMERAL_SYSTEM_PROMPT`` when that env var is set.

    Delegates to :mod:`son_of_anton_cli.personality` (single owner).
    """
    from son_of_anton_cli.personality import resolve_ephemeral_system_prompt

    return resolve_ephemeral_system_prompt(cfg)


def read_raw_config() -> Dict[str, Any]:
    """Read ~/.son-of-anton/config.toml as-is, without merging defaults or migrating.

    Returns the raw TOML dict, or ``{}`` if the file doesn't exist or can't
    be parsed.  Use this for lightweight config reads where you just need a
    single value and don't want the overhead of ``load_config()``'s deep-merge
    + migration pipeline.

    Cached on the config file's (mtime_ns, size) — same strategy as
    ``load_config()``. Returns a deepcopy on every call since some callers
    mutate the result before passing to ``save_config()``.
    """
    with _CONFIG_LOCK:
        try:
            config_path = get_config_path()
            st = config_path.stat()
            cache_key = (st.st_mtime_ns, st.st_size)
        except (FileNotFoundError, OSError):
            return {}

        path_key = str(config_path)
        cached = _RAW_CONFIG_CACHE.get(path_key)
        if cached is not None and cached[:2] == cache_key:
            return copy.deepcopy(cached[2])

        try:
            with open(config_path, encoding="utf-8") as f:
                data = fast_toml_load(f) or {}
        except Exception as e:
            _warn_config_parse_failure(config_path, e)
            return {}

        if not isinstance(data, dict):
            data = {}
        _RAW_CONFIG_CACHE[path_key] = (cache_key[0], cache_key[1], copy.deepcopy(data))
        return data


def read_user_config_raw(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Read a user ``config.toml`` EXACTLY as written on disk.

    No DEFAULT_CONFIG merge, no managed-scope overlay, no ``${ENV_VAR}``
    expansion, no migration, no root-model normalization, no caching.

    ONLY legal for write-back round-trips and raw-file diagnostics —
    behavioral reads must use load_config()/load_config_readonly().

    Legal call sites, exhaustively:

      * WRITE-BACK ROUND-TRIPS (read → mutate one key → save): merging
        defaults or the managed overlay here would persist hundreds of
        default keys (or administrator-pinned values) into the user's file
        on the next save. Raw is *correct*, not an optimization.
      * RAW-FILE DIAGNOSTICS (doctor, deprecation sweeps): these inspect
        what the user actually wrote — stale root keys, drift against .env —
        and merged defaults would produce false positives.
      * PRESENCE-SENSITIVE ENV BRIDGES (gateway/send bridges that only
        export a key when the user explicitly set it): a defaults merge
        would make every key "present" and bridge the entire DEFAULT_CONFIG
        into the environment. These sites must still apply
        ``_expand_env_vars`` inline, which they do.

    Semantics (deliberately mirrors the bare ``open()+TOML parse``
    pattern this replaces, so migrated sites keep their exact failure
    behavior):

      * missing file → ``{}``
      * unparseable TOML / other I/O errors → raises (callers that want
        fail-open already wrap in try/except; callers with last-known-good
        or warn semantics rely on the exception)
      * non-dict TOML root → ``{}``

    ``config_path`` defaults to :func:`get_config_path` (profile-aware).
    Pass an explicit path when the caller resolves its own home (gateway
    ``_son_of_anton_home``, tui profile override, multi-profile probes).
    """
    if config_path is None:
        config_path = get_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            data = fast_toml_load(f) or {}
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def read_raw_config_readonly() -> Dict[str, Any]:
    """Fast-path variant of ``read_raw_config()`` for callers that ONLY READ.

    Returns the cached raw-config dict directly, skipping the per-call
    ``copy.deepcopy`` that ``read_raw_config()`` performs (needed there
    because some callers mutate the result before ``save_config``).

    **Mutating the returned dict corrupts the in-process cache for every
    subsequent caller.** Only use on read-only paths — e.g. per-turn policy
    checks like the shared-metrics gate, which runs 2-3x per agent turn and
    was paying a full config deepcopy each time.

    Same (mtime_ns, size) freshness key as ``read_raw_config()`` — an edited
    config.toml is picked up on the next call.
    """
    with _CONFIG_LOCK:
        try:
            config_path = get_config_path()
            st = config_path.stat()
            cache_key = (st.st_mtime_ns, st.st_size)
        except (FileNotFoundError, OSError):
            return {}

        path_key = str(config_path)
        cached = _RAW_CONFIG_CACHE.get(path_key)
        if cached is not None and cached[:2] == cache_key:
            return cached[2]

        try:
            with open(config_path, encoding="utf-8") as f:
                data = fast_toml_load(f) or {}
        except Exception as e:
            _warn_config_parse_failure(config_path, e)
            return {}

        if not isinstance(data, dict):
            data = {}
        # Store and return THE SAME object (identity invariant): the first
        # caller must see the exact dict later cache hits return, so a test
        # asserting ``ro1 is ro2`` holds from the very first call.
        cached_copy = copy.deepcopy(data)
        _RAW_CONFIG_CACHE[path_key] = (cache_key[0], cache_key[1], cached_copy)
        return cached_copy


def require_readable_config_before_write(config_path: Optional[Path] = None) -> None:
    """Refuse to replace an existing config.toml that cannot be read."""
    if config_path is None:
        config_path = get_config_path()
    try:
        config_path.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(
            f"Refusing to overwrite {config_path}: existing config.toml cannot be accessed "
            f"({exc}). Fix the file permissions or move it aside first."
        ) from exc

    try:
        with open(config_path, "rb") as f:
            f.read(1)
    except OSError as exc:
        raise RuntimeError(
            f"Refusing to overwrite {config_path}: existing config.toml cannot be read "
            f"({exc}). Fix the file permissions or move it aside first."
        ) from exc


def atomic_config_write(config_path: Path, data: Any, **kwargs: Any) -> None:
    """Fail-closed atomic write for ``config.toml``.

    The single chokepoint every config-update path should use instead of
    calling :func:`utils.atomic_yaml_write` directly. It runs
    :func:`require_readable_config_before_write` first, so a full-file
    replacement can never silently clobber an existing ``config.toml`` that
    degraded to an empty dict on read (permission error, broken mount,
    transient I/O). New-file creation still works when the path is absent.

    Root cause this guards: ``read_raw_config()`` returns ``{}`` for BOTH an
    absent file and an unreadable-but-present file. Callers that read then
    overwrite can't tell the two apart, so an unreadable config would be
    replaced with only defaults or the single edited section. Routing every
    write through this helper enforces the invariant in one place rather than
    relying on each of ~15 independent write sites to remember the guard.

    ``kwargs`` are forwarded verbatim to ``atomic_toml_write``
    (``sort_keys``, ``default_flow_style``, ``extra_content``, ...).
    """
    from utils import atomic_toml_write

    require_readable_config_before_write(config_path)
    atomic_toml_write(config_path, data, **kwargs)


def load_config() -> Dict[str, Any]:
    """Load configuration from ~/.son-of-anton/config.toml.

    Cached on the config file's (mtime_ns, size). Returns a deepcopy of
    the cached value when unchanged, since most call sites mutate the
    result (e.g. ``cfg["model"]["default"] = ...`` before ``save_config``).
    The cache is keyed on ``str(config_path)`` so profile switches
    (which change ``SON_OF_ANTON_HOME`` and therefore ``get_config_path()``)
    don't collide.

    Read-only callers should use ``load_config_readonly()`` to skip the
    defensive deepcopy — that path matters in agent-loop hot spots like
    ``get_provider_request_timeout`` which is called once per API turn.
    """
    return _load_config_impl(want_deepcopy=True)


def load_config_readonly() -> Dict[str, Any]:
    """Fast-path variant of ``load_config()`` for callers that ONLY READ.

    Returns the cached config dict directly without the defensive deepcopy
    that ``load_config()`` applies. **Mutating the returned dict (or any
    nested structure) corrupts the in-process cache for every subsequent
    caller** — only use this when you are absolutely sure your code path
    will not write to the result. If you need to mutate or pass to
    ``save_config``, call ``load_config()`` instead.

    Why this exists: ``load_config()`` cache-hit cost is ~265us per call,
    half of which (~135us) is the defensive deepcopy. The agent loop calls
    into config reads (timeouts, thresholds, feature flags) ~20-50x per
    conversation; skipping deepcopy here removes a measurable allocation
    source and the GC pressure that comes with it.

    Note: this returns a plain ``dict`` (not ``MappingProxyType``) so
    existing ``isinstance(x, dict)`` guards downstream keep working. The
    safety guarantee is purely documented, not enforced — be careful.
    """
    return _load_config_impl(want_deepcopy=False)


TERMINAL_CONFIG_ENV_MAP = {
    "backend": "TERMINAL_ENV",
    "cwd": "TERMINAL_CWD",
    "timeout": "TERMINAL_TIMEOUT",
    "lifetime_seconds": "TERMINAL_LIFETIME_SECONDS",
    "sandbox_dir": "TERMINAL_SANDBOX_DIR",
}


def _terminal_env_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _is_ssh_remote_tilde_cwd(backend: str, cwd: str) -> bool:
    """Return whether the remote SSH shell must expand *cwd* itself.

    Expanding ``~`` on the Son of Anton host rewrites it to the host home
    before SSH sees it. Preserve ``~`` and ``~/...`` so they follow the
    user selected by the SSH connection.
    """
    if (backend or "").strip().lower() != "ssh":
        return False
    return cwd == "~" or cwd.startswith("~/")


def apply_terminal_config_to_env(
    *,
    env: Optional[Dict[str, str]] = None,
    config: Optional[Dict[str, Any]] = None,
    override: Optional[bool] = None,
) -> Dict[str, str]:
    """Bridge ``terminal.*`` config into the env vars terminal tools read.

    ``tools.terminal_tool`` is intentionally environment-driven because it also
    runs in child processes (TUI, gateway workers).  This helper
    gives those child-process launch paths the same config bridge as classic
    CLI without importing ``cli.py`` and paying for its startup side effects.

    Explicit keys in the user config's ``terminal`` section are authoritative
    and override their matching env values.  Merged defaults only backfill
    missing env vars; they never replace unrelated exported/.env values.
    """
    target = os.environ if env is None else env

    raw_config = read_raw_config()
    raw_terminal_cfg = raw_config.get("terminal")
    file_has_terminal_config = isinstance(raw_terminal_cfg, dict)
    if not file_has_terminal_config:
        raw_terminal_cfg = {}
    should_override = file_has_terminal_config if override is None else override

    cfg = config if config is not None else load_config_readonly()
    terminal_cfg = cfg.get("terminal", {}) if isinstance(cfg, dict) else {}
    if not isinstance(terminal_cfg, dict):
        return target

    # A caller-supplied config is its own source of explicit keys.  For the
    # normal merged-config path, only keys present in raw config.toml may
    # override existing env values; keys inherited from DEFAULT_CONFIG are
    # backfill-only.
    explicit_keys = terminal_cfg.keys() if config is not None else raw_terminal_cfg.keys()
    backend_is_explicit = config is not None or "backend" in raw_terminal_cfg
    if backend_is_explicit:
        terminal_backend = str(
            terminal_cfg.get("backend") or target.get("TERMINAL_ENV") or ""
        )
    else:
        terminal_backend = str(
            target.get("TERMINAL_ENV") or terminal_cfg.get("backend") or ""
        )

    # The local backend's working-directory contract is the process's own
    # launch dir (os.getcwd()), matching cli.py's bridge and the documented
    # CLI behavior. A configured terminal.cwd is a gateway/daemon concept
    # (messaging mode) and must NOT redirect interactive sessions or their
    # children into it — otherwise a home-manager-written config.toml pins
    # every local TUI/CLI to the account's home regardless of spawn dir.
    local_backend = (str(terminal_backend or "").strip().lower() in {"", "local"})

    for cfg_key, env_var in TERMINAL_CONFIG_ENV_MAP.items():
        if cfg_key not in terminal_cfg:
            continue
        if cfg_key == "cwd" and local_backend:
            continue
        value = terminal_cfg[cfg_key]
        if cfg_key == "cwd":
            raw_cwd = str(value or "").strip()
            if raw_cwd in {".", "auto", "cwd"}:
                continue
            if isinstance(value, str) and not _is_ssh_remote_tilde_cwd(
                terminal_backend, raw_cwd
            ):
                value = os.path.expanduser(value)
        if (should_override and cfg_key in explicit_keys) or env_var not in target:
            target[env_var] = _terminal_env_value(value)
    return target


def _load_config_impl(*, want_deepcopy: bool) -> Dict[str, Any]:
    with _CONFIG_LOCK:
        ensure_son_of_anton_home()
        config_path = get_config_path()
        path_key = str(config_path)

        try:
            st = config_path.stat()
            user_sig: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            user_sig = None
        cache_sig = user_sig

        cached = _LOAD_CONFIG_CACHE.get(path_key)
        if cached is not None and cache_sig is not None and cached[:2] == cache_sig:
            # File signatures match, but the cached expansion is only valid if
            # every ${VAR} it was expanded against still has the same value.
            # Without this, a load_config() that ran before load_son_of_anton_dotenv()
            # pins unexpanded literals (e.g. auxiliary.<task>.api_key) for the
            # life of the process (#58514).
            env_snapshot = cached[3]
            if all(os.environ.get(k) == v for k, v in env_snapshot.items()):
                return copy.deepcopy(cached[2]) if want_deepcopy else cached[2]

        config = copy.deepcopy(DEFAULT_CONFIG)

        if user_sig is not None:
            try:
                with open(config_path, encoding="utf-8") as f:
                    user_config = fast_toml_load(f) or {}

                if "max_turns" in user_config:
                    agent_user_config = dict(user_config.get("agent") or {})
                    if agent_user_config.get("max_turns") is None:
                        agent_user_config["max_turns"] = user_config["max_turns"]
                    user_config["agent"] = agent_user_config
                    user_config.pop("max_turns", None)

                config = _deep_merge(config, user_config)
            except Exception as e:
                # Last-known-good fallback (port of openai/codex#31188's
                # invariant: a parse failure in a policy/config file must not
                # silently replace the effective policy with an empty/default
                # one). Falling through to DEFAULT_CONFIG here drops EVERY user
                # override — including security-critical ``approvals.deny``
                # rules, which are supposed to block commands even under yolo.
                # A long-running gateway whose user mid-edits config.toml into
                # broken TOML would silently lose those rules on the next load.
                # Within a running process we still have the last successfully
                # loaded config — keep serving it until the file is fixed.
                # Fresh processes with no last-known-good keep the existing
                # DEFAULT_CONFIG fallback.
                lkg = _LAST_EXPANDED_CONFIG_BY_PATH.get(path_key)
                _warn_config_parse_failure(
                    config_path,
                    e,
                    fallback="last-known-good" if lkg is not None else "defaults",
                )
                if lkg is not None:
                    # save_config() stores the pre-expansion normalized dict
                    # (env-ref templates preserved); the load path stores the
                    # expanded one. Expand defensively — idempotent when the
                    # stored value is already expanded.
                    from typing import cast as _cast
                    lkg_copy: Dict[str, Any] = _cast(
                        Dict[str, Any], _expand_env_vars(copy.deepcopy(lkg))
                    )
                    if cache_sig is not None:
                        # Cache under the corrupt file's signature (empty env
                        # snapshot: always valid) so repeated loads don't
                        # re-parse the broken file; fixing the file changes the
                        # signature and triggers a normal reload.
                        _empty_env: Dict[str, Optional[str]] = {}
                        _LOAD_CONFIG_CACHE[path_key] = (
                            cache_sig[0], cache_sig[1], lkg_copy, _empty_env,
                        )
                    return copy.deepcopy(lkg_copy) if want_deepcopy else lkg_copy

        normalized = _normalize_root_model_keys(_normalize_max_turns_config(config))
        expanded = _expand_env_vars(normalized)
        _LAST_EXPANDED_CONFIG_BY_PATH[path_key] = copy.deepcopy(expanded)
        if cache_sig is not None:
            # Cache stores a separate deepcopy so subsequent ``load_config()``
            # (deepcopy=True) callers can mutate freely without affecting the
            # cached value, and ``load_config_readonly()`` (deepcopy=False)
            # callers all see the same stable cached object. The cached tuple is
            # (user_mtime, user_size, value, env_ref_snapshot). The snapshot
            # records the environment values this expansion was made against so
            # later loads can detect env drift (late .env load, in-process
            # rotation) — see cache hit above.
            cached_copy = copy.deepcopy(expanded)
            env_snapshot = _env_ref_snapshot(normalized)
            _LOAD_CONFIG_CACHE[path_key] = (*cache_sig, cached_copy, env_snapshot)
            # On the readonly path return the same cached object subsequent
            # calls will see — keeps "two readonly calls return the same
            # object" invariant that callers may rely on for identity checks.
            if not want_deepcopy:
                return cached_copy
        else:
            _LOAD_CONFIG_CACHE.pop(path_key, None)
        # First-load result is a fresh dict (not aliased to the cache); safe
        # to return directly. For the deepcopy=True path this is the
        # canonical "freshly-built mutable result" the function has always
        # returned. For the deepcopy=False path with no cache (e.g. config
        # file missing), it's also fine — callers get an isolated object.
        return expanded


_SECURITY_COMMENT = """
# ── Security ──────────────────────────────────────────────────────────
# Secret redaction is ON by default — strings that look like API keys,
# tokens, and passwords are masked in tool output, logs, and chat
# responses before the model or user ever sees them. Set redact_secrets
# to false to disable (e.g. when developing the redactor itself).
# tirith pre-exec scanning is enabled by default when the tirith binary
# is available. Configure via security.tirith_* keys or env vars
# (TIRITH_ENABLED, TIRITH_BIN, TIRITH_TIMEOUT, TIRITH_FAIL_OPEN).
#
# security:
#   redact_secrets: true
#   tirith_enabled: true
#   tirith_path: "tirith"
#   tirith_timeout: 5
#   tirith_fail_open: true
"""

_FALLBACK_COMMENT = """
# ── Fallback Model ────────────────────────────────────────────────────
# Automatic provider failover when primary is unavailable.
# Uncomment and configure to enable. Triggers on rate limits (429),
# overload (529), service errors (503), or connection failures.
#
# Supported providers:
#   nous         (OAuth — son-of-anton auth) — Nous Portal
#   zai          (ZAI_API_KEY)         — Z.AI / GLM
#   kimi-coding  (KIMI_API_KEY)        — Kimi / Moonshot
#   kimi-coding-cn (KIMI_CN_API_KEY)   — Kimi / Moonshot (China)
#   minimax      (MINIMAX_API_KEY)     — MiniMax
#   minimax-cn   (MINIMAX_CN_API_KEY)  — MiniMax (China)
#
# For custom OpenAI-compatible endpoints, add base_url and key_env.
#
# fallback_model:
#   provider: openrouter
#   model: anthropic/claude-sonnet-4
"""


def save_config(
    config: Dict[str, Any],
    *,
    strip_defaults: bool = True,
    preserve_keys: Optional[Set[Tuple[str, ...]]] = None,
    merge_existing: bool = False,
):
    """Save configuration to ~/.son-of-anton/config.toml.\n

    Default values from ``DEFAULT_CONFIG`` are not written to disk unless
    the user explicitly set them (i.e. the path exists in the raw config
    before any normalisation).  This prevents config.toml from being
    contaminated with schema defaults on every save, which makes future
    default changes invisible to users.

    When ``merge_existing`` is True, the on-disk raw config is deep-merged
    under *config* before writing so partial callers (migration steps via
    ``_persist_migration``) cannot drop unrelated sections the caller omitted.
    Full-document replacement callers (raw TOML editor, callers that
    already deep-merge) must leave this False so intentional deletions survive.
    """
    with _CONFIG_LOCK:
        if is_managed():
            managed_error("save configuration")
            return
        from utils import atomic_toml_write

        ensure_son_of_anton_home()
        config_path = get_config_path()
        require_readable_config_before_write(config_path)
        # Compute explicit user paths BEFORE any normalisation --------
        # _normalize_max_turns_config may inject agent.max_turns from
        # DEFAULT_CONFIG; using the raw dict preserves which paths the
        # user actually set so _strip_default_values can keep them.
        _raw_for_paths = read_raw_config()
        explicit_raw_paths: Optional[Set[Tuple[str, ...]]] = (
            _explicit_config_paths(_raw_for_paths) if _raw_for_paths else None
        )
        if merge_existing and _raw_for_paths:
            config = _merge_partial_save(_raw_for_paths, config)
        # ----------------------------------------------------------------

        current_normalized = _normalize_root_model_keys(_normalize_max_turns_config(config))
        normalized = current_normalized
        raw_existing = (
            _normalize_root_model_keys(_normalize_max_turns_config(_raw_for_paths))
            if _raw_for_paths
            else {}
        )
        if raw_existing:
            normalized = _preserve_env_ref_templates(
                normalized,
                raw_existing,
                _LAST_EXPANDED_CONFIG_BY_PATH.get(str(config_path)),
            )

        # Strip schema-default values so the user's custom settings are not
        # silently reset on every save.  Keys the user explicitly set (paths
        # from the raw pre-normalisation config) are always preserved.
        effective_preserve_keys: Set[Tuple[str, ...]] = set()
        if explicit_raw_paths:
            effective_preserve_keys.update(explicit_raw_paths)
        if preserve_keys:
            effective_preserve_keys.update(preserve_keys)

        if strip_defaults and effective_preserve_keys:
            # _preserve_env_ref_templates may return Any; cast for type-checker.
            from typing import cast as _cast
            normalized = _cast(Dict[str, Any], normalized)
            normalized = _strip_default_values(
                normalized,  # type: ignore[arg-type]
                DEFAULT_CONFIG,
                preserve_keys=effective_preserve_keys,
            )

        # Build optional commented-out sections for features that are off by
        # default or only relevant when explicitly configured.
        parts = []
        sec = normalized.get("security", {})
        if not sec or sec.get("redact_secrets") is None:
            parts.append(_SECURITY_COMMENT)
        fb = normalized.get("fallback_model", {})
        fb_is_valid = False
        if isinstance(fb, list):
            fb_is_valid = any(isinstance(e, dict) and e.get("provider") and e.get("model") for e in fb)
        elif isinstance(fb, dict):
            fb_is_valid = bool(fb.get("provider") and fb.get("model"))
        if not fb_is_valid:
            parts.append(_FALLBACK_COMMENT)

        atomic_toml_write(
            config_path,
            normalized,
            extra_content="".join(parts) if parts else None,
        )
        _secure_file(config_path)
        _RAW_CONFIG_CACHE.pop(str(config_path), None)
        _LAST_EXPANDED_CONFIG_BY_PATH[str(config_path)] = copy.deepcopy(current_normalized)


def _parse_env_value(raw_value: str) -> str:
    """Parse the small .env value subset Son of Anton writes itself."""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        quoted = value[1:-1]
        parsed: list[str] = []
        i = 0
        while i < len(quoted):
            ch = quoted[i]
            if ch == "\\" and i + 1 < len(quoted):
                next_ch = quoted[i + 1]
                if next_ch in {'"', "\\"}:
                    parsed.append(next_ch)
                    i += 2
                    continue
            parsed.append(ch)
            i += 1
        return "".join(parsed)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def load_env() -> Dict[str, str]:
    """Load environment variables from ~/.son-of-anton/.env.

    Normalizes line endings before parsing while treating each assignment's
    value as opaque data for boundary discovery.

    The parsed dict is memoised keyed on the .env file mtime, because
    ``get_env_value()`` is called dozens-to-hundreds of times per
    interactive menu render (`son-of-anton tools`, `son-of-anton setup`, status
    panels). Sanitisation is O(lines), so re-parsing the
    same file on every call was burning ~300ms of CPU per `son-of-anton tools`
    menu paint on top of the OAuth-refresh slowness. The mtime check
    invalidates the cache when the user edits .env mid-process.
    """
    global _env_cache
    env_path = get_env_path()

    try:
        mtime = env_path.stat().st_mtime
        size = env_path.stat().st_size
        cache_key = (str(env_path), mtime, size)
    except FileNotFoundError:
        cache_key = (str(env_path), None, None)
    except Exception:
        cache_key = None

    if cache_key is not None and _env_cache is not None:
        cached_key, cached_vars = _env_cache
        if cached_key == cache_key:
            return dict(cached_vars)

    env_vars: Dict[str, str] = {}

    if env_path.exists():
        # Always use explicit UTF-8; tolerate BOM via utf-8-sig since editors
        # may add one when saving a UTF-8 .env file.
        open_kw = {"encoding": "utf-8-sig", "errors": "replace"}
        with open(env_path, **open_kw) as f:
            raw_lines = f.readlines()
        # Normalize line endings without interpreting value contents as syntax.
        lines = _sanitize_env_lines(raw_lines)
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                # Strip the bash-compatible ``export `` prefix so lines like
                # ``export API_KEY=...`` parse as ``API_KEY`` rather than being
                # stored under the wrong key ``"export API_KEY"`` (#6659).
                if line.startswith('export '):
                    line = line[7:]
                key, _, value = line.partition('=')
                env_vars[key.strip()] = _parse_env_value(value)

    if cache_key is not None:
        _env_cache = (cache_key, dict(env_vars))

    return env_vars


# Module-level memo for load_env(), keyed on (path, mtime, size).
# Editing .env bumps mtime → next load_env() rebuilds. invalidate_env_cache()
# is the explicit knob for writers that update .env via this module
# (set_env_value, save_env, etc.) without relying on filesystem mtime
# resolution.
_env_cache: Optional[Tuple[Tuple[str, Optional[float], Optional[int]], Dict[str, str]]] = None


def invalidate_env_cache() -> None:
    """Clear the load_env() process-level memo.

    Writers that mutate .env (set_env_value, save_env, etc.) call this
    to guarantee the next load_env() sees their change even on
    filesystems with coarse mtime resolution. Reads invalidate naturally
    via the mtime/size check.
    """
    global _env_cache
    _env_cache = None


def _sanitize_env_lines(lines: list) -> list:
    """Normalize .env line endings without changing assignment semantics.

    Content after the first ``=`` is opaque value data. A known variable name
    embedded in that value must never be reinterpreted as another assignment;
    concatenated assignments are ambiguous and therefore remain on one line.
    """
    sanitized: list[str] = []
    for line in lines:
        raw = line.rstrip("\r\n")
        stripped = raw.strip()

        # Preserve blank lines and comments
        if not stripped or stripped.startswith("#"):
            sanitized.append(raw + "\n")
            continue

        sanitized.append(stripped + "\n")

    return sanitized


def _check_non_ascii_credential(key: str, value: str) -> str:
    """Warn and strip non-ASCII characters from credential values.

    API keys and tokens must be pure ASCII — they are sent as HTTP header
    values which httpx/httpcore encode as ASCII.  Non-ASCII characters
    (commonly introduced by copy-pasting from rich-text editors or PDFs
    that substitute lookalike Unicode glyphs for ASCII letters) cause
    ``UnicodeEncodeError: 'ascii' codec can't encode character`` at
    request time.

    Returns the sanitized (ASCII-only) value.  Prints a warning if any
    non-ASCII characters were found and removed.
    """
    try:
        value.encode("ascii")
        return value  # all ASCII — nothing to do
    except UnicodeEncodeError:
        pass

    # Build a readable list of the offending characters
    bad_chars: list[str] = []
    for i, ch in enumerate(value):
        if ord(ch) > 127:
            bad_chars.append(f"  position {i}: {ch!r} (U+{ord(ch):04X})")
    sanitized = value.encode("ascii", errors="ignore").decode("ascii")

    print(
        f"\n  Warning: {key} contains non-ASCII characters that will break API requests.\n"
        f"  This usually happens when copy-pasting from a PDF, rich-text editor,\n"
        f"  or web page that substitutes lookalike Unicode glyphs for ASCII letters.\n"
        f"\n"
        + "\n".join(f"  {line}" for line in bad_chars[:5])
        + ("\n  ... and more" if len(bad_chars) > 5 else "")
        + "\n\n  The non-ASCII characters have been stripped automatically.\n"
        "  If authentication fails, re-copy the key from the provider's portal.\n",
        file=sys.stderr,
    )
    return sanitized


def _quote_env_value(value: str) -> str:
    """Quote .env values containing characters with special dotenv meaning."""
    if value == "":
        return value
    # Internal whitespace (space/tab/etc.) must be quoted so shell `set -a; . file`
    # word-splits don't break paths like macOS "Application Support". Leading/
    # trailing whitespace is already covered by the strip check; any() covers
    # internal runs that strip() would leave alone.
    needs_quoting = (
        "#" in value
        or '"' in value
        or "'" in value
        or value != value.strip()
        or any(c.isspace() for c in value)
    )
    if not needs_quoting:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _env_line_defines_key(line: str, key: str) -> bool:
    """True when a .env line assigns ``key`` — plain or ``export``-prefixed.

    ``load_env()`` accepts the bash-compatible ``export KEY=value`` form
    (#6659), so the writers must recognise the same shape. Otherwise a
    hand-added ``export`` line is invisible to save (duplicate appended) and
    remove (line survives → the value resurrects on the next load, #40041).
    """
    stripped = line.strip()
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    return stripped.startswith(f"{key}=")


def save_env_value(key: str, value: str):
    """Save or update a value in ~/.son-of-anton/.env."""
    if is_managed():
        managed_error(f"set {key}")
        return
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    _reject_denylisted_env_var(key)
    value = value.replace("\n", "").replace("\r", "")
    # API keys / tokens must be ASCII — strip non-ASCII with a warning.
    value = _check_non_ascii_credential(key, value)
    ensure_son_of_anton_home()
    env_path = get_env_path()

    read_kw = {"encoding": "utf-8-sig", "errors": "replace"}
    write_kw = {"encoding": "utf-8"}

    lines = []
    if env_path.exists():
        with open(env_path, **read_kw) as f:
            lines = f.readlines()
        # Normalize safe line formatting without interpreting values as syntax.
        lines = _sanitize_env_lines(lines)

    serialized_value = _quote_env_value(value)

    # Find and update or append. Match both ``KEY=`` and the bash-compatible
    # ``export KEY=`` form — load_env() parses export lines (#6659), so a
    # user-added ``export GITHUB_TOKEN=...`` shows as set in every UI. If the
    # writer didn't match it, a save would append a SECOND line and a later
    # delete of that line would silently resurrect the old exported value
    # (#40041: "token detected but cannot be replaced through the UI").
    found = False
    for i, line in enumerate(lines):
        if _env_line_defines_key(line, key):
            lines[i] = f"{key}={serialized_value}\n"
            found = True
            break

    if not found:
        # Ensure there's a newline at the end of the file before appending
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={serialized_value}\n")
    
    fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix='.tmp', prefix='.env_')
    # Preserve original permissions so volume mounts aren't clobbered.
    original_mode = None
    if env_path.exists():
        try:
            original_mode = stat.S_IMODE(env_path.stat().st_mode)
        except OSError:
            pass
    try:
        with os.fdopen(fd, 'w', **write_kw) as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, env_path)
        # Preserve the original file mode (e.g. 0640 for volume mounts)
        # instead of letting _secure_file unconditionally tighten to 0600.
        if original_mode is not None:
            try:
                os.chmod(env_path, original_mode)
            except OSError:
                pass
        else:
            _secure_file(env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    os.environ[key] = value
    invalidate_env_cache()


def remove_env_value(key: str) -> bool:
    """Remove a key from ~/.son-of-anton/.env and os.environ.

    Returns True if the key was found and removed, False otherwise.
    """
    if is_managed():
        managed_error(f"remove {key}")
        return False
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    env_path = get_env_path()
    if not env_path.exists():
        os.environ.pop(key, None)
        return False

    read_kw = {"encoding": "utf-8-sig", "errors": "replace"}
    write_kw = {"encoding": "utf-8"}

    with open(env_path, **read_kw) as f:
        lines = f.readlines()
    lines = _sanitize_env_lines(lines)

    new_lines = [line for line in lines if not _env_line_defines_key(line, key)]
    found = len(new_lines) < len(lines)

    if found:
        fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix='.tmp', prefix='.env_')
        # Preserve original permissions so volume mounts aren't clobbered.
        original_mode = None
        try:
            original_mode = stat.S_IMODE(env_path.stat().st_mode)
        except OSError:
            pass
        try:
            with os.fdopen(fd, 'w', **write_kw) as f:
                f.writelines(new_lines)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp_path, env_path)
            # Preserve the original file mode (e.g. 0640 for volume
            # mounts) instead of letting _secure_file unconditionally tighten
            # to 0600. Mirrors save_env_value().
            if original_mode is not None:
                try:
                    os.chmod(env_path, original_mode)
                except OSError:
                    pass
            else:
                _secure_file(env_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    os.environ.pop(key, None)
    invalidate_env_cache()
    return found


def reload_env() -> int:
    """Re-read ~/.son-of-anton/.env into os.environ. Returns count of vars updated.

    Adds/updates vars that changed and removes vars that were deleted from
    the .env file (but only vars known to Son of Anton — OPTIONAL_ENV_VARS and
    _EXTRA_ENV_KEYS — to avoid clobbering unrelated environment).
    """
    env_vars = load_env()
    known_keys = set(OPTIONAL_ENV_VARS.keys()) | _EXTRA_ENV_KEYS
    count = 0
    for key, value in env_vars.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
            count += 1
    # Remove known Son of Anton vars that are no longer in .env
    for key in known_keys:
        if key not in env_vars and key in os.environ:
            del os.environ[key]
            count += 1
    return count


def get_env_value(key: str) -> Optional[str]:
    """Get a value from ``os.environ``, then ``~/.son-of-anton/.env``.

    The ``os.environ`` read routes through ``agent.secret_scope.get_secret``,
    the single seam every credential read goes through — same as its siblings
    ``get_env_value_prefer_dotenv`` and ``gateway.config._getenv``.
    """
    try:
        from agent.secret_scope import get_secret as _get_secret
    except Exception:
        if key in os.environ:
            return os.environ[key]
        return load_env().get(key)

    try:
        val = _get_secret(key)
    except Exception:
        val = os.environ.get(key)
    if val is not None:
        return val

    # Then check .env file
    env_vars = load_env()
    return env_vars.get(key)


def get_env_value_prefer_dotenv(key: str) -> Optional[str]:
    """Resolve a credential env value, preferring ``~/.son-of-anton/.env`` over ``os.environ``.

    Used for Son of Anton-managed credentials where a deliberate edit to ``.env``
    must take precedence over a stale value inherited from the parent shell
    (Codex CLI, test scripts, login profile exports). Without this, rotating
    a key in ``.env`` mid-session leaves callers serving the stale shell
    value and produces persistent 401s.

    The ``os.environ`` fallback routes through ``secret_scope.get_secret``,
    matching the credential-pool seeding path's behaviour.
    """
    env_vars = load_env()
    val = env_vars.get(key)
    if val:
        return val
    try:
        from agent.secret_scope import get_secret as _get_secret
    except Exception:
        return os.environ.get(key)

    try:
        return _get_secret(key)
    except Exception:
        return os.environ.get(key)


# =============================================================================
# Config display
# =============================================================================

def redact_key(key: str) -> str:
    """Redact an API key for display.

    Thin wrapper over :func:`agent.redact.mask_secret` — preserves the
    "(not set)" placeholder in dim color for the empty case.
    """
    from agent.redact import mask_secret
    return mask_secret(key, empty=color("(not set)", Colors.DIM))


# Key names (case-insensitive, exact match) whose VALUE is a credential and
# must be masked before printing any config dict to the terminal. Covers the
# fields a custom provider stuffs into the `model`/`custom_providers` blocks
# (`api_key`) plus the usual token/secret/password shapes. Exact-match only so
# benign keys like `token_count` or `secret_santa` don't get masked.
_SECRET_CONFIG_KEYS = frozenset({
    "api_key",
    "apikey",
    "key",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "secret",
    "client_secret",
    "password",
    "passwd",
    "auth",
    "authorization",
    "private_key",
    "bearer",
    "jwt",
})


def redact_config_value(value: Any, _depth: int = 0) -> Any:
    """Return a copy of ``value`` with credential-shaped keys masked for display.

    Recursively walks dicts/lists and replaces the value of any key in
    ``_SECRET_CONFIG_KEYS`` (case-insensitive) with a masked form via
    :func:`agent.redact.mask_secret`. Non-secret keys and scalar values pass
    through unchanged. Use this before ``print``-ing any config sub-tree that
    might carry a custom-provider ``api_key`` — ``print`` bypasses the logging
    redactor, and opaque tokens (e.g. Cloudflare ``cfut_...``) don't match the
    vendor-prefix regexes either, so structural key-name masking is required.
    """
    from agent.redact import mask_secret

    # Defensive bound on recursion depth for pathological/cyclic configs.
    if _depth > 20:
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _SECRET_CONFIG_KEYS and isinstance(v, str) and v:
                out[k] = mask_secret(v)
            else:
                out[k] = redact_config_value(v, _depth + 1)
        return out
    if isinstance(value, list):
        return [redact_config_value(v, _depth + 1) for v in value]
    return value


def show_config():
    """Display current configuration."""
    config = load_config()

    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│              ⚛ Son of Anton Configuration                    │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.CYAN))

    # Paths
    print()
    print(color("◆ Paths", Colors.CYAN, Colors.BOLD))
    print(f"  Config:       {get_config_path()}")
    print(f"  Secrets:      {get_env_path()}")
    print(f"  Install:      {get_project_root()}")
    
    # API Keys
    print()
    print(color("◆ API Keys", Colors.CYAN, Colors.BOLD))
    
    # Only credentials a shipped tool actually reads. The voice, browser, and
    # image-generation tools left the fork with their toolsets; their keys kept
    # being printed here, inviting people to configure services nothing would
    # ever call. The search and scrape keys below are read by tools/web_tools.py.
    keys = [
        ("OPENROUTER_API_KEY", "OpenRouter"),
        ("EXA_API_KEY", "Exa"),
        ("PARALLEL_API_KEY", "Parallel"),
        ("FIRECRAWL_API_KEY", "Firecrawl"),
        ("TAVILY_API_KEY", "Tavily"),
    ]
    
    for env_key, name in keys:
        value = get_env_value(env_key)
        print(f"  {name:<14} {redact_key(value)}")
    
    # Model settings
    print()
    print(color("◆ Model", Colors.CYAN, Colors.BOLD))
    print(f"  Model:        {redact_config_value(config.get('model', 'not set'))}")
    _cfg_max_turns = config.get('agent', {}).get('max_turns', DEFAULT_CONFIG['agent']['max_turns'])
    print(f"  Max turns:    {_cfg_max_turns}")
    # Warn on stale SON_OF_ANTON_MAX_ITERATIONS ghost in .env that disagrees with
    # config.toml (issue #17534). Read the .env FILE directly so we catch the
    # ghost even when the gateway bridge already overrode os.environ.
    try:
        _env_ghost = load_env().get("SON_OF_ANTON_MAX_ITERATIONS")
        if _env_ghost is not None and str(_env_ghost).strip() != str(_cfg_max_turns).strip():
            print(color(
                f"                ⚠ .env has stale SON_OF_ANTON_MAX_ITERATIONS={_env_ghost} "
                f"(run 'son-of-anton doctor --fix' to remove)",
                Colors.YELLOW,
            ))
    except Exception:
        pass
    
    # Display
    print()
    print(color("◆ Display", Colors.CYAN, Colors.BOLD))
    display = config.get('display', {})
    try:
        from son_of_anton_cli.personality import active_personality_name

        _active_personality = active_personality_name(config) or 'none'
    except Exception:
        _active_personality = display.get('personality') or 'none'
    print(f"  Personality:  {_active_personality}")
    print(f"  Reasoning:    {'on' if display.get('show_reasoning', True) else 'off'}")
    print(f"  Bell:         {'on' if display.get('bell_on_complete', False) else 'off'}")
    ump = display.get('user_message_preview', {}) if isinstance(display.get('user_message_preview', {}), dict) else {}
    ump_first = ump.get('first_lines', 2)
    ump_last = ump.get('last_lines', 2)
    print(f"  User preview: first {ump_first} line(s), last {ump_last} line(s)")

    # Terminal
    print()
    print(color("◆ Terminal", Colors.CYAN, Colors.BOLD))
    terminal = config.get('terminal', {})
    print(f"  Backend:      {terminal.get('backend', 'local')}")
    print(f"  Working dir:  {terminal.get('cwd', '.')}")
    print(f"  Timeout:      {terminal.get('timeout', 60)}s")
    
    # Timezone
    print()
    print(color("◆ Timezone", Colors.CYAN, Colors.BOLD))
    tz = config.get('timezone', '')
    if tz:
        print(f"  Timezone:     {tz}")
    else:
        print(f"  Timezone:     {color('(server-local)', Colors.DIM)}")

    # Compaction
    print()
    print(color("◆ Context Compaction", Colors.CYAN, Colors.BOLD))
    compaction = config.get('compaction', {})
    enabled = compaction.get('enabled', True)
    print(f"  Enabled:      {'yes' if enabled else 'no'}")
    if enabled:
        print(f"  Threshold:    {compaction.get('threshold', 0.50) * 100:.0f}%")
        _tt = compaction.get('threshold_tokens')
        if _tt is not None:
            try:
                _tt = int(_tt)
                if _tt > 0:
                    print(f"  Token cap:    {_tt:,} tokens (takes lower of ratio vs absolute)")
            except (TypeError, ValueError):
                pass
        print(f"  Target ratio: {compaction.get('target_ratio', 0.20) * 100:.0f}% of threshold preserved")
        print(f"  Protect last: {compaction.get('protect_last_n', 20)} messages")
        print(f"  Protect first: {compaction.get('protect_first_n', 3)} non-system head messages")
        _aux_comp = config.get('auxiliary', {}).get('compaction', {})
        _sm = _aux_comp.get('model', '') or '(auto)'
        print(f"  Model:        {_sm}")
        comp_provider = _aux_comp.get('provider', 'auto')
        if comp_provider and comp_provider != 'auto':
            print(f"  Provider:     {comp_provider}")
    
    # Auxiliary models
    auxiliary = config.get('auxiliary', {})
    aux_tasks = {
        "Vision":      auxiliary.get('vision', {}),
        "Web extract": auxiliary.get('web_extract', {}),
    }
    has_overrides = any(
        t.get('provider', 'auto') != 'auto' or t.get('model', '')
        for t in aux_tasks.values()
    )
    if has_overrides:
        print()
        print(color("◆ Auxiliary Models (overrides)", Colors.CYAN, Colors.BOLD))
        for label, task_cfg in aux_tasks.items():
            prov = task_cfg.get('provider', 'auto')
            mdl = task_cfg.get('model', '')
            if prov != 'auto' or mdl:
                parts = [f"provider={prov}"]
                if mdl:
                    parts.append(f"model={mdl}")
                print(f"  {label:12s}  {', '.join(parts)}")
    
    print()
    print(color("─" * 60, Colors.DIM))
    print(color("  Configuration is declared in config.toml (the Nix module's settings)", Colors.DIM))
    print()


def _cron_model_drift_axis_for_config_key(key: str) -> Optional[str]:
    """Return the cron drift guard axis affected by a config key, if any."""
    normalized = str(key or "").strip().lower()
    if normalized in {"model", "model.default", "model.model", "model.name"}:
        return "model"
    if normalized in {"model.provider", "provider"}:
        return "provider"
    return None


def cron_model_drift_guard_enabled(
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether cron must fail closed on unpinned inference drift.

    Only the literal TOML boolean ``false`` disables this spend-safety guard.
    Missing, malformed, or non-boolean values stay fail-closed. When *config*
    is omitted, load the active merged configuration so CLI warnings honor the
    same user/managed setting as the scheduler.
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            return True
    if not isinstance(config, dict):
        return True
    cron_config = config.get("cron")
    if not isinstance(cron_config, dict):
        return True
    return cron_config.get("model_drift_guard", True) is not False


def _cron_fleet_default_covers_axis(
    axis: str,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when cron.model / cron.model_provider covers *axis*.

    An axis covered by the explicit cron-fleet default no longer follows the
    global model/provider at fire time, so the drift guard never engages for
    it and switch-time warnings would be false alarms.
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            return False
    if not isinstance(config, dict):
        return False
    cron_config = config.get("cron")
    if not isinstance(cron_config, dict):
        return False
    key = "model" if axis == "model" else "model_provider"
    value = cron_config.get(key)
    return isinstance(value, str) and bool(value.strip())


_CRON_MODEL_IMPACT_JOB_LIMIT = 50
_CRON_MODEL_IMPACT_ID_LIMIT = 256
_CRON_MODEL_IMPACT_NAME_LIMIT = 120


def _model_assignment_text(value: Any) -> str:
    """Return a trimmed scalar model/provider value, or empty for malformed data."""
    return value.strip() if isinstance(value, str) else ""


def resolve_cron_model_drift_defaults(
    config: Any,
    *,
    environ: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    """Resolve the global provider/model values cron compares against snapshots.

    Mirrors the scheduler's global-model precedence: a truthy configured model
    wins ``SON_OF_ANTON_MODEL``; the environment is only a fallback. Per-job and cron
    fleet defaults are handled by the caller/classifier because they suppress a
    drift axis rather than changing the global assignment.
    """
    env = os.environ if environ is None else environ
    provider = ""
    model = _model_assignment_text(env.get("SON_OF_ANTON_MODEL", ""))
    model_config = config.get("model") if isinstance(config, dict) else None
    if isinstance(model_config, str):
        configured_model = model_config.strip()
        if configured_model:
            model = configured_model
    elif isinstance(model_config, dict):
        provider = _model_assignment_text(model_config.get("provider"))
        configured_model = _model_assignment_text(
            model_config.get("default")
            or model_config.get("model")
            or model_config.get("name")
        )
        if configured_model:
            model = configured_model
    return provider, model


def cron_model_drift_axes(
    job: Any,
    *,
    current_provider: Any = "",
    current_model: Any = "",
    config: Any = None,
) -> List[str]:
    """Return the unpinned axes that the fail-closed cron guard would block."""
    if not isinstance(job, dict) or not cron_model_drift_guard_enabled(config):
        return []

    current = {
        "provider": _model_assignment_text(current_provider).lower(),
        "model": _model_assignment_text(current_model).lower(),
    }
    drifted: List[str] = []
    for axis in ("provider", "model"):
        if _cron_fleet_default_covers_axis(axis, config):
            continue
        if _model_assignment_text(job.get(axis)):
            continue
        snapshot = _model_assignment_text(job.get(f"{axis}_snapshot")).lower()
        if snapshot and current[axis] and snapshot != current[axis]:
            drifted.append(axis)
    return drifted


def _valid_cron_impact_job_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    job_id = value.strip()
    if not job_id or len(job_id) > _CRON_MODEL_IMPACT_ID_LIMIT:
        return ""
    if any(unicodedata.category(char).startswith("C") for char in job_id):
        return ""
    return job_id


def _cron_impact_job_name(value: Any, job_id: str) -> str:
    if isinstance(value, str):
        printable = "".join(
            char for char in value if not unicodedata.category(char).startswith("C")
        )
        name = " ".join(printable.split())[:_CRON_MODEL_IMPACT_NAME_LIMIT].rstrip()
        if name:
            return name
    return f"Job {job_id}"[:_CRON_MODEL_IMPACT_NAME_LIMIT].rstrip()


def _unavailable_cron_model_impact(guard_enabled: bool) -> Dict[str, Any]:
    return {
        "available": False,
        "guard_enabled": guard_enabled,
        "affected_count": 0,
        "truncated": False,
        "jobs": [],
    }


def build_cron_model_impact(
    *,
    current_provider: Any = "",
    current_model: Any = "",
    config: Any = None,
    jobs: Any = None,
) -> Dict[str, Any]:
    """Build a bounded, profile-local summary of jobs blocked by model drift.

    Job-store inspection is deliberately best effort: a model assignment has
    already succeeded by the time Desktop requests this summary, so an unreadable
    store is represented as unavailable instead of failing or rolling back it.
    """
    guard_enabled = cron_model_drift_guard_enabled(config)
    if jobs is None:
        try:
            from cron.jobs import load_jobs

            jobs = load_jobs()
        except Exception:
            return _unavailable_cron_model_impact(guard_enabled)
    if not isinstance(jobs, list):
        return _unavailable_cron_model_impact(guard_enabled)

    result: Dict[str, Any] = {
        "available": True,
        "guard_enabled": guard_enabled,
        "affected_count": 0,
        "truncated": False,
        "jobs": [],
    }
    if not guard_enabled:
        return result

    from cron.jobs import is_job_runnable

    seen_ids: Set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if not is_job_runnable(job) or job.get("no_agent"):
            continue
        job_id = _valid_cron_impact_job_id(job.get("id"))
        if not job_id or job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        axes = cron_model_drift_axes(
            job,
            current_provider=current_provider,
            current_model=current_model,
            config=config,
        )
        if not axes:
            continue
        result["affected_count"] += 1
        if len(result["jobs"]) < _CRON_MODEL_IMPACT_JOB_LIMIT:
            result["jobs"].append(
                {
                    "id": job_id,
                    "name": _cron_impact_job_name(job.get("name"), job_id),
                    "drifted_axes": axes,
                }
            )

    result["truncated"] = result["affected_count"] > len(result["jobs"])
    return result


def _load_cron_jobs_for_config_warning() -> List[Dict[str, Any]]:
    """Best-effort read of the active profile's cron jobs database."""
    try:
        from cron.jobs import load_jobs

        jobs = load_jobs()
        return jobs if isinstance(jobs, list) else []
    except Exception:
        return []


def warn_unpinned_cron_jobs_after_model_config_change(
    key: str,
    value: Any,
    config: Optional[Dict[str, Any]] = None,
) -> None:
    """Warn when a global model/provider change will trip cron's drift guard."""
    axis = _cron_model_drift_axis_for_config_key(key)
    if axis is None:
        return

    new_value = _model_assignment_text(value)
    if not new_value:
        return
    impact = build_cron_model_impact(
        current_provider=new_value if axis == "provider" else "",
        current_model=new_value if axis == "model" else "",
        config=config,
        jobs=_load_cron_jobs_for_config_warning(),
    )
    affected = impact["affected_count"]
    if affected <= 0:
        return

    snapshot_field = f"{axis}_snapshot"
    noun = "job" if affected == 1 else "jobs"
    verb = "has" if affected == 1 else "have"
    print(
        f"⚠️  {affected} enabled unpinned cron {noun} {verb} stored "
        f"{snapshot_field} values that differ from the new global {axis}. "
        "They will fail closed on their next run instead of silently using the "
        "changed model/provider. Inspect with `son-of-anton cron list`, then pin the "
        "intended values with `son-of-anton cron edit <job_id> --provider <provider> "
        "--model <model>`."
    )


def _default_value_for_key(dotted_key: str):
    """Return the leaf value declared for *dotted_key* in ``DEFAULT_CONFIG``.

    Unknown keys and non-leaf paths return ``None`` so they retain the legacy
    best-effort coercion used by ``config set``.
    """
    node = DEFAULT_CONFIG
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if not isinstance(node, dict) else None


def set_config_value(key: str, value: str):
    """Persist one dotted config key (the ``/perm`` and ``/approvals`` writer).

    Refused on a Nix-managed install, whose module settings own config.toml.
    String-typed settings keep the literal value (``approvals.mode = "off"``
    must not become a TOML boolean); other values coerce true/false.
    """
    if is_managed():
        managed_error("set configuration values")
        return
    coerced: Any = value
    if not isinstance(_default_value_for_key(key), str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on"}:
            coerced = True
        elif lowered in {"false", "no", "off"}:
            coerced = False
    config_path = get_config_path()
    require_readable_config_before_write(config_path)
    user_config = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            user_config = fast_toml_load(f) or {}
    _set_nested(user_config, key, coerced)
    ensure_son_of_anton_home()
    from utils import atomic_toml_write
    atomic_toml_write(config_path, user_config, sort_keys=False)
    print(f"✓ Set {key} = {coerced} in {config_path}")


def get_config_value(key: str, *, as_json: bool = False):
    """Print a resolved configuration value."""
    if _is_env_config_key(key):
        env_value = get_env_value(key.upper())
        value = _MISSING if env_value is None else env_value
    else:
        value = _get_nested(load_config(), key)

    if value is _MISSING:
        print(f"Config key not set: {key}", file=sys.stderr)
        sys.exit(1)

    print(_format_config_get_value(value, as_json=as_json))


# =============================================================================
# Command handler
# =============================================================================

def config_command(args):
    """Handle the read-only config subcommands (show, get, path, env-path)."""
    subcmd = getattr(args, 'config_command', None)
    if subcmd is None or subcmd == "show":
        show_config()
    elif subcmd == "get":
        key = getattr(args, 'key', None)
        if not key:
            print("Usage: son-of-anton config get <key> [--json]")
            sys.exit(1)
        get_config_value(key, as_json=getattr(args, 'json', False))
    elif subcmd == "path":
        print(get_config_path())
    elif subcmd == "env-path":
        print(get_env_path())


# ── Profile-driven env var injection ─────────────────────────────────────────
# Any provider registered in providers/ with auth_type="api_key" automatically
# gets its env_vars exposed in OPTIONAL_ENV_VARS without editing this file.
# Runs once at import time.

_profile_env_vars_injected = False


def _inject_profile_env_vars() -> None:
    """Populate OPTIONAL_ENV_VARS from provider profiles not already listed.

    Called once at module load time. Idempotent — repeated calls are no-ops.
    """
    global _profile_env_vars_injected
    if _profile_env_vars_injected:
        return
    _profile_env_vars_injected = True
    try:
        from providers import list_providers
        for _pp in list_providers():
            if _pp.auth_type not in {"api_key",}:
                continue
            for _var in _pp.env_vars:
                if _var in OPTIONAL_ENV_VARS:
                    continue
                _is_key = not _var.endswith("_BASE_URL") and not _var.endswith("_URL")
                OPTIONAL_ENV_VARS[_var] = {
                    "description": f"{_pp.display_name or _pp.name} {'API key' if _is_key else 'base URL override'}",
                    "prompt": f"{_pp.display_name or _pp.name} {'API key' if _is_key else 'base URL (leave empty for default)'}",
                    "url": _pp.signup_url or None,
                    "password": _is_key,
                    "category": "provider",
                    "advanced": True,
                }
    except Exception:
        pass


# Eagerly inject so that OPTIONAL_ENV_VARS is fully populated at import time.
_inject_profile_env_vars()
