"""
Unified tool configuration for Son of Anton Agent.

`son-of-anton tools` and `son-of-anton setup tools` both enter this module.
Select a platform → toggle toolsets on/off → for newly enabled tools
that need API keys, run through provider-aware configuration.

Saves per-platform tool configuration to ~/.son-of-anton/config.yaml under
the `platform_toolsets` key.
"""

import json as _json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set


from son_of_anton_cli.config import (
    cfg_get,
    load_config, save_config, get_env_value, save_env_value,
)
from son_of_anton_cli.colors import Colors, color
from utils import base_url_hostname, is_truthy_value

logger = logging.getLogger(__name__)


def _post_setup_no_window_flags(*, streams_to_console: bool = False) -> int:
    """Win32 creationflags that stop post-setup children flashing a console.

    The GUI runs post-setup hooks through a detached, console-less
    ``son-of-anton tools post-setup <key>`` child. On Windows, every console child
    (npm.cmd, npx, pip, powershell, curl) spawned from that console-less
    parent materializes a brand-new console window — the "terminal flash"
    users see when clicking "Run setup". ``CREATE_NO_WINDOW`` (via
    :func:`son_of_anton_cli._subprocess_compat.windows_hide_flags`) suppresses it
    without breaking ``capture_output`` — unlike ``DETACHED_PROCESS``, stdio
    handles stay inheritable. Returns 0 on POSIX, so passing the result
    unconditionally is safe.

    ``streams_to_console=True`` marks children spawned WITHOUT stdio
    redirection (live installer output, e.g. the verbose cua-driver install).
    Hiding those in an interactive console session would silently swallow
    their output into an invisible console, so the flag is only applied when
    the current process has no usable console of its own (stdout is a
    pipe/log file — exactly the GUI-spawn case that flashes).
    """
    from son_of_anton_cli._subprocess_compat import windows_hide_flags

    flags = windows_hide_flags()
    if not flags:
        return 0
    if streams_to_console:
        try:
            if sys.stdout is not None and sys.stdout.isatty():
                return 0
        except Exception:
            pass
    return flags

# Platforms already warned about an all-invalid platform_toolsets list, so the
# runtime check in _get_platform_tools warns once per platform instead of on
# every tool resolution for a persistently-corrupt config (#38798).
_warned_invalid_platform_toolsets: Set[str] = set()

PROJECT_ROOT = Path(__file__).parent.parent.resolve()


# ─── UI Helpers (shared with setup.py) ────────────────────────────────────────

from son_of_anton_cli.cli_output import (  # noqa: E402 — late import block
    print_error as _print_error,
    print_info as _print_info,
    print_success as _print_success,
    print_warning as _print_warning,
    prompt as _prompt,
)

# ─── Toolset Registry ─────────────────────────────────────────────────────────

# Toolsets shown in the configurator, grouped for display.
# Each entry: (toolset_name, label, description)
# These map to keys in toolsets.py TOOLSETS dict.
CONFIGURABLE_TOOLSETS = [
    ("web",             "🔍 Web Search & Scraping",    "web_search, web_extract"),
    ("terminal",        "💻 Terminal & Processes",      "terminal, process"),
    ("file",            "📁 File Operations",           "read, write, patch, search"),
    ("code_execution",  "⚡ Code Execution",            "execute_code"),
    ("vision",          "👁️  Vision / Image Analysis",  "vision_analyze"),
    ("video",           "🎬 Video Analysis",            "video_analyze (requires video-capable model)"),
    ("skills",          "📚 Skills",                    "list, view, manage"),
    ("todo",            "📋 Task Planning",             "todo"),
    ("memory",          "💾 Memory",                    "persistent memory across sessions"),
    ("context_engine",  "🧩 Context Engine",            "runtime tools from the active context engine"),
    ("session_search",  "🔎 Session Search",            "search past conversations"),
    ("clarify",         "❓ Clarifying Questions",      "clarify"),
    ("delegation",      "👥 Task Delegation",           "delegate_task"),
    ("cronjob",         "⏰ Cron Jobs",                 "create/list/update/pause/resume/run, with optional attached skills"),
    ("discord",         "💬 Discord (read/participate)", "fetch messages, search members, create thread"),
    ("discord_admin",   "🛡️  Discord Server Admin",    "list channels/roles, pin, assign roles"),
]


def gui_toolset_label(label: str) -> str:
    """Strip leading emoji/icons from toolset titles for GUI surfaces.

    Registry labels use ``<emoji> <title>``; plugin toolsets prefix with ``🔌``.
    CLI/TUI keeps the raw ``label`` — only HTTP APIs call this helper.
    """
    text = (label or "").strip()
    if not text:
        return text
    parts = text.split(None, 1)
    if len(parts) == 2 and parts[0] and not any(ch.isascii() and ch.isalnum() for ch in parts[0]):
        return parts[1].strip()
    return text


# Toolsets that are OFF by default for new installs.
# They're still in _SON_OF_ANTON_CORE_TOOLS (available at runtime if enabled),
# but the setup checklist won't pre-select them for first-time users.
_DEFAULT_OFF_TOOLSETS = {"discord", "discord_admin", "video"}


# Config-only capabilities: they appear in `son-of-anton tools` for provider/API-key
# configuration (TOOL_CATEGORIES) but are NOT model toolsets — they ship zero
# tool schemas and their on/off switch lives in their own config section,
# not ``platform_toolsets``. Excluded from the
# per-platform enable/disable checklist; configured via the "Reconfigure an
# existing tool" flow and the GUI provider matrix instead.
_CONFIG_ONLY_TOOLSETS = set()


# Platform-scoped toolsets: only appear in the `son-of-anton tools` checklist for
# these platforms, and only resolve/save for these platforms.  A toolset
# absent from this map is available on every platform (current behaviour).
#
# Use this for tools whose APIs only make sense on one platform (Discord
# server admin, Slack workspace admin, etc.).  Keeps every other platform's
# checklist from filling up with irrelevant toggles.
_TOOLSET_PLATFORM_RESTRICTIONS: Dict[str, Set[str]] = {
    "discord": {"discord"},
    "discord_admin": {"discord"},
}


def _toolset_allowed_for_platform(ts_key: str, platform: str) -> bool:
    """Return True if ``ts_key`` is configurable on ``platform``.

    Toolsets without a restriction entry are allowed everywhere (the default).
    """
    allowed = _TOOLSET_PLATFORM_RESTRICTIONS.get(ts_key)
    return allowed is None or platform in allowed


def _toolset_configuration_platform(ts_key: str, default: str = "cli") -> str:
    """Return the platform a platform-less configuration UI should target.

    Most configurable toolsets retain the historical desktop/CLI target. A
    toolset restricted away from that platform must instead be configured on
    one of its supported platforms; otherwise the shared save helper correctly
    drops it and the UI reports a successful no-op.
    """
    allowed = _TOOLSET_PLATFORM_RESTRICTIONS.get(ts_key)
    if not allowed or default in allowed:
        return default
    return sorted(allowed)[0]


def _get_effective_configurable_toolsets():
    """Return CONFIGURABLE_TOOLSETS + any plugin-provided toolsets.

    Plugin toolsets are appended at the end so they appear after the
    built-in toolsets in the TUI checklist. A plugin whose toolset key
    already appears in ``CONFIGURABLE_TOOLSETS`` is skipped — bundled
    plugins share their toolset key with the built-in entry, and we want
    the built-in label/description to win.
    Without the dedupe, ``son-of-anton tools`` → "reconfigure existing" would
    list the same toolset twice.
    """
    result = list(CONFIGURABLE_TOOLSETS)
    seen = {ts_key for ts_key, _, _ in result}
    try:
        from son_of_anton_cli.plugins import discover_plugins, get_plugin_toolsets
        discover_plugins()  # idempotent — ensures plugins are loaded
        for entry in get_plugin_toolsets():
            if entry[0] in seen:
                continue
            seen.add(entry[0])
            result.append(entry)
    except Exception:
        pass
    return result


def _get_plugin_toolset_keys() -> set:
    """Return the set of toolset keys provided by plugins."""
    try:
        from son_of_anton_cli.plugins import get_plugin_toolset_keys_nowait
        # Non-blocking on the CLI startup path: while background plugin
        # discovery is still importing modules, this serves last launch's
        # persisted key set (used only to exclude plugin toolsets from
        # composite expansion) instead of joining the discovery thread.
        return get_plugin_toolset_keys_nowait()
    except Exception:
        return set()


def _checklist_toolset_keys(platform: str) -> Set[str]:
    """Return the toolset keys the ``son-of-anton tools`` checklist actually offers
    for ``platform``.

    This mirrors exactly what ``_prompt_toolset_checklist`` renders:
    ``_get_effective_configurable_toolsets()`` (built-in + plugin toolsets),
    filtered by ``_toolset_allowed_for_platform``. The checklist's returned
    selection can therefore only ever be a subset of this universe.

    Non-configurable toolsets that ``_get_platform_tools`` resolves at read
    time — check_fn-gated toolsets, recovered platform composites, MCP
    server names — are NOT in this set because the checklist never shows
    them. Use this to scope the added/removed diff the UI prints,
    so ``son-of-anton tools`` never claims to add or remove a toolset the user was
    never given a checkbox for. The underlying config is unaffected — those
    entries are preserved by ``_save_platform_tools`` regardless.
    """
    return {
        ts_key
        for ts_key, _, _ in _get_effective_configurable_toolsets()
        if _toolset_allowed_for_platform(ts_key, platform)
        and ts_key not in _CONFIG_ONLY_TOOLSETS
    }

# Platform display config — derived from the canonical registry so every
# module shares the same data.  Kept as dict-of-dicts for backward
# compatibility with existing ``PLATFORMS[key]["label"]`` access patterns.
from son_of_anton_cli.platforms import PLATFORMS as _PLATFORMS_REGISTRY

PLATFORMS = {
    k: {"label": info.label, "default_toolset": info.default_toolset}
    for k, info in _PLATFORMS_REGISTRY.items()
}


# ─── Tool Categories (provider-aware configuration) ──────────────────────────
# Maps toolset keys to their provider options. When a toolset is newly enabled,
# we use this to show provider selection and prompt for the right API keys.
# Toolsets not in this map either need no config or use the simple fallback.

TOOL_CATEGORIES = {
    "web": {
        "name": "Web Search & Extract",
        "setup_title": "Select Search Provider",
        "setup_note": "A free DuckDuckGo search skill is also included — skip this if you don't need a premium provider.",
        "icon": "🔍",
        # Per-provider rows are injected at runtime from
        # plugins.web.<vendor>.provider via _plugin_web_search_providers()
        # in _visible_providers(). Only non-provider UX setup-flow rows
        # for the firecrawl backend are listed here:
        #   - "Firecrawl Self-Hosted" — points firecrawl at a private
        #     Docker instance via FIRECRAWL_API_URL only.
        # See PR #25182 for the migration rationale.
        "providers": [
            {
                "name": "Firecrawl Self-Hosted",
                "badge": "free · self-hosted",
                "tag": "Run your own Firecrawl instance (Docker)",
                "web_backend": "firecrawl",
                "env_vars": [
                    {"key": "FIRECRAWL_API_URL", "prompt": "Your Firecrawl instance URL (e.g., http://localhost:3002)"},
                ],
            },
        ],
    },
    "langfuse": {
        "name": "Langfuse Observability",
        "icon": "📊",
        "providers": [
            {
                "name": "Langfuse Cloud",
                "tag": "Hosted Langfuse (cloud.langfuse.com)",
                "env_vars": [
                    {"key": "SON_OF_ANTON_LANGFUSE_PUBLIC_KEY", "prompt": "Langfuse public key (pk-lf-...)", "url": "https://cloud.langfuse.com"},
                    {"key": "SON_OF_ANTON_LANGFUSE_SECRET_KEY", "prompt": "Langfuse secret key (sk-lf-...)", "url": "https://cloud.langfuse.com"},
                ],
                "post_setup": "langfuse",
            },
            {
                "name": "Langfuse Self-Hosted",
                "tag": "Self-hosted Langfuse instance",
                "env_vars": [
                    {"key": "SON_OF_ANTON_LANGFUSE_PUBLIC_KEY", "prompt": "Langfuse public key (pk-lf-...)"},
                    {"key": "SON_OF_ANTON_LANGFUSE_SECRET_KEY", "prompt": "Langfuse secret key (sk-lf-...)"},
                    {"key": "SON_OF_ANTON_LANGFUSE_BASE_URL", "prompt": "Langfuse server URL (e.g. http://localhost:3000)", "default": "http://localhost:3000"},
                ],
                "post_setup": "langfuse",
            },
        ],
    },
}

# Simple env-var requirements for toolsets NOT in TOOL_CATEGORIES.
# Used as a fallback for toolsets that just need an API key.
#
# `vision` is listed here only so it registers as a *configurable* toolset
# (the value gates the reconfigure menu + the "[no API key]" suffix). Its
# actual setup runs through `_configure_vision_backend()` — a full
# provider+model picker like `son-of-anton model` — NOT this single-key prompt.
# `_toolset_has_keys("vision")` resolves via
# `resolve_vision_provider_client()`, so the tuple below is never prompted or
# read for vision; it's purely a presence marker.
TOOLSET_ENV_REQUIREMENTS = {
    "vision":     [("OPENAI_API_KEY",   "https://platform.openai.com/api-keys")],
}


def _pip_install(
    args: List[str],
    *,
    timeout: int = 300,
    capture_output: bool = True,
):
    """Install Python packages from a post-setup hook.

    Strategy (in order):
    1. ``uv pip install`` if uv is on PATH — fast, doesn't need pip in the venv.
    2. ``python -m pip install`` — works on stdlib venvs.
    3. ``python -m ensurepip --upgrade`` then retry pip — covers ``uv venv``
       which creates a venv WITHOUT pip.

    Why this exists: the Windows installer creates the venv via ``uv venv``,
    which doesn't seed pip. Post-setup hooks that shelled out to
    ``[sys.executable, '-m', 'pip', 'install', ...]`` failed with
    ``No module named pip`` on every fresh install. uv-first sidesteps that.

    Returns the ``subprocess.CompletedProcess`` from whichever tier succeeded
    (or the last failure for the caller to inspect).
    """
    venv_root = Path(sys.executable).parent.parent
    uv_env = {**os.environ, "VIRTUAL_ENV": str(venv_root)}

    # Managed uv first: $SON_OF_ANTON_HOME/bin is never on PATH, so a bare which()
    # misses the uv Son of Anton installed and prefers a system one when both exist.
    # ensure_uv() rather than a pure lookup because this runs during setup,
    # where installing uv is in scope — and tier 2 is a pip that the Windows
    # installer's `uv venv` does not seed, so failing to find uv here is the
    # difference between a working post-setup hook and "No module named pip".
    from son_of_anton_cli.managed_uv import ensure_uv

    uv_bin = ensure_uv()
    if uv_bin:
        try:
            result = subprocess.run(
                [uv_bin, "pip", "install", *args],
                capture_output=capture_output, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                env=uv_env,
                creationflags=_post_setup_no_window_flags(
                    streams_to_console=not capture_output
                ),
            )
            if result.returncode == 0:
                return result
            # Fall through to pip — uv may have failed for an unrelated reason
            # (resolution conflict, network), and pip might handle it.
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    pip_cmd = [sys.executable, "-m", "pip"]
    try:
        # Probe for pip; bootstrap via ensurepip if missing (uv venv lacks it).
        probe = subprocess.run(
            pip_cmd + ["--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=_post_setup_no_window_flags(),
        )
        if probe.returncode != 0:
            raise FileNotFoundError("pip not in venv")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            subprocess.run(
                [sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=True,
                creationflags=_post_setup_no_window_flags(),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            # Synthesize a result so callers see a clean failure path.
            return subprocess.CompletedProcess(
                pip_cmd, returncode=1, stdout="",
                stderr=f"pip not available and ensurepip failed: {e}",
            )

    return subprocess.run(
        pip_cmd + ["install", *args],
        capture_output=capture_output, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=_post_setup_no_window_flags(
            streams_to_console=not capture_output
        ),
    )

def _run_post_setup(post_setup_key: str):
    """Run post-setup hooks for tools that need extra installation steps."""
    from son_of_anton_constants import find_node_executable

    if post_setup_key == "ddgs":
        try:
            __import__("ddgs")
            _print_success("    ddgs is already installed")
        except ImportError:
            _print_info("    Installing ddgs (DuckDuckGo search package)...")
            try:
                result = _pip_install(["-U", "ddgs", "--quiet"], timeout=300)
                if result.returncode == 0:
                    _print_success("    ddgs installed")
                else:
                    _print_warning("    ddgs install failed:")
                    _print_info(f"      {(result.stderr or '').strip()[:300]}")
                    _print_info("    Run manually: uv pip install -U ddgs")
                    return
            except subprocess.TimeoutExpired:
                _print_warning("    ddgs install timed out (>5min)")
                _print_info("    Run manually: uv pip install -U ddgs")
                return
        _print_info("    No API key required. DuckDuckGo enforces server-side rate limits.")
        _print_info("    Pair with an extract provider if you also need web_extract.")


    elif post_setup_key == "langfuse":
        # Install the langfuse SDK.
        try:
            __import__("langfuse")
            _print_success("    langfuse SDK already installed")
        except ImportError:
            _print_info("    Installing langfuse SDK...")
            result = _pip_install(["langfuse", "--quiet"], timeout=120)
            if result.returncode == 0:
                _print_success("    langfuse SDK installed")
            else:
                _print_warning("    langfuse SDK install failed — run manually: uv pip install langfuse")
        # Opt the bundled observability/langfuse plugin into plugins.enabled.
        # The plugin ships in the repo but doesn't load until the user enables
        # it (standalone plugins are opt-in).
        try:
            from son_of_anton_cli.plugins_cmd import _get_enabled_set, _save_enabled_set
            enabled = _get_enabled_set()
            if "observability/langfuse" in enabled or "langfuse" in enabled:
                _print_success("    Plugin observability/langfuse already enabled")
            else:
                enabled.add("observability/langfuse")
                _save_enabled_set(enabled)
                _print_success("    Plugin observability/langfuse enabled")
        except Exception as exc:
            _print_warning(f"    Could not enable plugin automatically: {exc}")
            _print_info("    Run manually: son-of-anton plugins enable observability/langfuse")
        _print_info("    Restart Son of Anton for tracing to take effect.")
        _print_info("    Verify: son-of-anton plugins list")



def valid_post_setup_keys() -> Set[str]:
    """Return the set of post-setup keys declared by any visible provider.

    Collected from ``TOOL_CATEGORIES`` plus the plugin-registered web
    providers (which can also carry a ``post_setup``). This is the
    allowlist the ``son-of-anton tools post-setup`` command and the post-setup
    endpoint validate against, so a caller can't drive ``_run_post_setup``
    with an arbitrary key.
    """
    keys: Set[str] = set()
    for cat in TOOL_CATEGORIES.values():
        for prov in cat.get("providers", []):
            ps = prov.get("post_setup")
            if ps:
                keys.add(ps)
    # Plugin-registered providers can declare their own post_setup hooks.
    for builder in (
        _plugin_web_search_providers,
    ):
        try:
            for prov in builder():
                ps = prov.get("post_setup")
                if ps:
                    keys.add(ps)
        except Exception:  # pragma: no cover — defensive; plugins optional
            continue
    return keys


def run_post_setup_command(args) -> int:
    """``son-of-anton tools post-setup <key>`` — non-interactive post-setup runner.

    Runs the install/bootstrap hook a provider declares (pip install for
    ddgs/langfuse, etc.). This is the stable, scriptable target the setup
    flows spawn so they can drive backend setup without re-implementing
    the install logic.
    Returns a process exit code (0 ok, 2 unknown key).
    """
    key = getattr(args, "post_setup_key", None)
    if not key:
        _print_error("Usage: son-of-anton tools post-setup <key>")
        return 2
    valid = valid_post_setup_keys()
    if key not in valid:
        _print_error(
            f"Unknown post-setup key: {key!r}. "
            f"Valid keys: {', '.join(sorted(valid)) or '(none)'}"
        )
        return 2
    _print_info(f"Running post-setup hook: {key}")
    try:
        _run_post_setup(key)
    except Exception as exc:  # pragma: no cover — defensive
        _print_error(f"Post-setup failed: {exc}")
        return 1
    _print_success(f"Post-setup '{key}' complete")
    return 0


# ─── Platform / Toolset Helpers ───────────────────────────────────────────────

def _get_enabled_platforms() -> List[str]:
    """Return platform keys that are configured (have tokens or are CLI)."""
    enabled = ["cli"]
    if get_env_value("DISCORD_BOT_TOKEN"):
        enabled.append("discord")
    if get_env_value("SLACK_BOT_TOKEN"):
        enabled.append("slack")
    return enabled


def _platform_toolset_summary(config: dict, platforms: Optional[List[str]] = None) -> Dict[str, Set[str]]:
    """Return a summary of enabled toolsets per platform.

    When ``platforms`` is None, this uses ``_get_enabled_platforms`` to
    auto-detect platforms. Tests can pass an explicit list to avoid relying
    on environment variables.
    """
    if platforms is None:
        platforms = _get_enabled_platforms()

    summary: Dict[str, Set[str]] = {}
    for pkey in platforms:
        summary[pkey] = _get_platform_tools(config, pkey)
    return summary


def _parse_enabled_flag(value, default: bool = True) -> bool:
    """Parse bool-like config values used by tool/platform settings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def enabled_mcp_server_names(config: dict) -> Set[str]:
    """Names of MCP servers globally enabled in config.yaml or by a plugin.

    Shared by the gateway/CLI platform resolver (``_get_platform_tools``) and
    the cron per-job toolset resolver (``cron.scheduler``) so every path agrees
    on MCP membership. A server is enabled unless its config sets an explicitly
    falsey ``enabled`` (per ``_parse_enabled_flag``: false/0/no/off) — a missing
    flag or an unrecognized value is treated as enabled.

    Portable Agent Plugins contribute MCP servers in-memory rather than via
    ``config.yaml`` (see ``PluginManager.get_portable_mcp_servers``). Those are
    included here so their tools fold into platform toolsets like native
    servers do — the user's opt-in is enabling the plugin itself. Without this,
    a portable server registers with the MCP runtime but its tools never reach
    the model's schema.
    """
    mcp_servers = (config or {}).get("mcp_servers") or {}
    names = {
        str(name)
        for name, server_cfg in mcp_servers.items()
        if isinstance(server_cfg, dict)
        and _parse_enabled_flag(server_cfg.get("enabled", True), default=True)
    }
    try:
        from son_of_anton_cli.plugins import (
            get_plugin_manager,
            get_portable_mcp_server_names_nowait,
        )

        portable = get_portable_mcp_server_names_nowait()
        # Native config wins on a name collision (mirrors _load_mcp_config).
        names |= portable - set(mcp_servers)
    except Exception:
        logger.debug("Failed to include portable MCP servers", exc_info=True)
    return names


def _exempt_explicit_platform_native(
    default_off: Set[str], platform: str, *, explicitly_configured: bool
) -> None:
    """Let platform-native default-off toolsets through on explicit config.

    Toolsets that are both in ``_DEFAULT_OFF_TOOLSETS`` and restricted to
    ``platform`` via ``_TOOLSET_PLATFORM_RESTRICTIONS`` (currently
    ``discord``/``discord_admin`` on the discord platform) are the platform's
    own native tools. They are kept off for *unconfigured* platforms (security
    opt-in), but once a user explicitly saves a toolset list for the platform
    the composite they chose (e.g. ``son-of-anton-discord``, which contains those
    tools) is an opt-in — stripping them silently defeats the explicit
    configuration (#35527). Mutates ``default_off`` in place.
    """
    if not explicitly_configured:
        return
    for ts in list(default_off):
        allowed = _TOOLSET_PLATFORM_RESTRICTIONS.get(ts)
        if allowed is not None and platform in allowed:
            default_off.discard(ts)


def _get_platform_tools(
    config: dict,
    platform: str,
    *,
    include_default_mcp_servers: bool = True,
) -> Set[str]:
    """Resolve which individual toolset names are enabled for a platform."""
    from toolsets import resolve_toolset, TOOLSETS

    platform_toolsets = config.get("platform_toolsets") or {}
    toolset_names = platform_toolsets.get(platform)
    # Track whether the user explicitly saved a toolset list for this platform
    # (vs. falling back to the platform default). An explicit composite (e.g.
    # ``son-of-anton-discord``) is an opt-in to the platform's native default-off
    # toolsets — see _exempt_explicit_platform_native (#35527).
    explicitly_configured = isinstance(toolset_names, list)

    if toolset_names is None or not isinstance(toolset_names, list):
        plat_info = PLATFORMS.get(platform)
        if plat_info:
            default_ts = plat_info["default_toolset"]
        else:
            # Plugin platform — derive toolset name from platform key
            default_ts = f"son-of-anton-{platform}"
        toolset_names = [default_ts]

    # YAML may parse bare numeric names (e.g. ``12306:``) as int.
    # Normalise to str so downstream sorted() never mixes types.
    toolset_names = [str(ts) for ts in toolset_names]

    configurable_keys = {ts_key for ts_key, _, _ in CONFIGURABLE_TOOLSETS}
    plugin_ts_keys = _get_plugin_toolset_keys()
    platform_default_keys = {p["default_toolset"] for p in PLATFORMS.values()}
    # Plugin-provided toolsets are first-class on a platform-toolsets list —
    # explicit config like ``[son-of-anton-cli, video]`` must survive filtering just
    # like a built-in configurable toolset would. See issue #81163.
    explicit_known_keys = configurable_keys | plugin_ts_keys

    # If the saved list contains any configurable keys directly, the user
    # has explicitly configured this platform — use direct membership.
    # This avoids the subset-inference bug where composite toolsets like
    # "son-of-anton-cli" (which include all _SON_OF_ANTON_CORE_TOOLS) cause disabled
    # toolsets to re-appear as enabled.
    has_explicit_config = any(ts in explicit_known_keys for ts in toolset_names)

    if has_explicit_config:
        enabled_toolsets = {
            ts for ts in toolset_names
            if ts in explicit_known_keys and _toolset_allowed_for_platform(ts, platform)
        }
        # Mixed config: composite toolset alongside configurables. Without
        # expansion the composite name is silently dropped, leaving sessions
        # with only the configurable opt-ins and no native tools. Mirror the
        # else-branch's subset inference, but apply _DEFAULT_OFF_TOOLSETS only
        # to the implicit expansion — anything the user explicitly listed must
        # survive.
        composite_tools = set()
        for ts_name in toolset_names:
            if ts_name in configurable_keys or ts_name in plugin_ts_keys:
                continue
            if ts_name not in TOOLSETS:
                continue
            composite_tools.update(resolve_toolset(ts_name))

        if composite_tools:
            expanded = set()
            for ts_key, _, _ in CONFIGURABLE_TOOLSETS:
                if not _toolset_allowed_for_platform(ts_key, platform):
                    continue
                # Compare the toolset's STATIC membership: a tool registered
                # into a toolset (e.g. delegate_cli -> delegation, desktop-only
                # read_terminal -> terminal) that the composite never listed must
                # not drop the whole toolset. See issue #49622.
                ts_tools = set(resolve_toolset(ts_key, include_registry=False))
                if ts_tools and ts_tools.issubset(composite_tools):
                    expanded.add(ts_key)

            default_off = set(_DEFAULT_OFF_TOOLSETS)
            if platform in default_off and platform not in _TOOLSET_PLATFORM_RESTRICTIONS:
                default_off.remove(platform)
            _exempt_explicit_platform_native(
                default_off, platform, explicitly_configured=explicitly_configured
            )
            expanded -= default_off

            enabled_toolsets |= expanded

    else:
        # No explicit config — fall back to resolving composite toolset names
        # (e.g. "son-of-anton-cli") to individual tool names and reverse-mapping.
        all_tool_names = set()
        for ts_name in toolset_names:
            all_tool_names.update(resolve_toolset(ts_name))

        enabled_toolsets = set()
        for ts_key, _, _ in CONFIGURABLE_TOOLSETS:
            if not _toolset_allowed_for_platform(ts_key, platform):
                continue
            # Compare the toolset's STATIC membership against the composite (see
            # issue #49622): get_toolset() merges registry-registered tools into
            # a toolset, but platform composites enumerate static tool names, so
            # an all-tools subset test against the merged set drops the whole
            # toolset the moment a plugin/overlay tool joins it.
            ts_tools = set(resolve_toolset(ts_key, include_registry=False))
            if ts_tools and ts_tools.issubset(all_tool_names):
                enabled_toolsets.add(ts_key)

        default_off = set(_DEFAULT_OFF_TOOLSETS)
        # Legacy safety: if the platform's own name matches a default-off
        # toolset, keep that toolset enabled on first install.  Skip this
        # dodge for platform-restricted toolsets — those are always opt-in
        # even on their own platform (e.g. `discord` + `discord` should stay
        # OFF).
        if platform in default_off and platform not in _TOOLSET_PLATFORM_RESTRICTIONS:
            default_off.remove(platform)
        _exempt_explicit_platform_native(
            default_off, platform, explicitly_configured=explicitly_configured
        )
        enabled_toolsets -= default_off

    # Recover non-configurable platform toolsets (e.g. discord).  These are
    # part of the platform's default composite but
    # absent from CONFIGURABLE_TOOLSETS, so they can't appear in the TUI
    # checklist or in a user-saved config.  Must run in BOTH branches —
    # otherwise saving via `son-of-anton tools` (which flips has_explicit_config
    # to True) silently drops them.
    _plat_info = PLATFORMS.get(platform)
    _default_ts = _plat_info["default_toolset"] if _plat_info else f"son-of-anton-{platform}"
    platform_tool_universe = set(resolve_toolset(_default_ts))
    configurable_tool_universe = set()
    for ck in configurable_keys:
        configurable_tool_universe.update(resolve_toolset(ck))
    claimed = set()
    for ts_key in enabled_toolsets:
        claimed.update(resolve_toolset(ts_key))
    skip = configurable_keys | plugin_ts_keys | platform_default_keys
    skip |= {k for k in TOOLSETS if k.startswith("son-of-anton-")}
    skip |= set(_DEFAULT_OFF_TOOLSETS) - {platform}
    for ts_key, ts_def in TOOLSETS.items():
        if ts_key in skip:
            continue
        if ts_def.get("includes"):
            continue
        # Posture toolsets (e.g. ``coding``) are session-level selections made
        # by agent/coding_context.py — not per-platform capabilities to recover.
        if ts_def.get("posture"):
            continue
        # Static membership (see #49622): a registry-added tool absent from the
        # platform composite must not block recovery of a non-configurable
        # toolset whose authored tools the composite does list.
        ts_tools = set(resolve_toolset(ts_key, include_registry=False))
        if not ts_tools or not ts_tools.issubset(platform_tool_universe):
            continue
        if ts_tools.issubset(configurable_tool_universe):
            continue
        if not ts_tools.issubset(claimed):
            enabled_toolsets.add(ts_key)
            claimed.update(ts_tools)

    # Plugin toolsets: enabled by default unless explicitly disabled, or
    # unless the toolset is in _DEFAULT_OFF_TOOLSETS (shipped as a bundled
    # plugin but the user must opt in via `son-of-anton tools`).
    # A plugin toolset is "known" for a platform once `son-of-anton tools`
    # has been saved for that platform (tracked via known_plugin_toolsets).
    # Unknown plugins default to enabled; known-but-absent = disabled.
    if plugin_ts_keys:
        known_map = config.get("known_plugin_toolsets", {}) or {}
        known_for_platform = set(known_map.get(platform, []) or [])
        for pts in plugin_ts_keys:
            if pts in toolset_names:
                # Explicitly listed in config — enabled
                enabled_toolsets.add(pts)
            elif pts in _DEFAULT_OFF_TOOLSETS:
                # Opt-in plugin toolset — stay off until user picks it
                continue
            elif pts not in known_for_platform:
                # New plugin not yet seen by son-of-anton tools — default enabled
                enabled_toolsets.add(pts)
            # else: known but not in config = user disabled it

    # Context-engine tools are runtime-provided by the active engine, so they
    # are not part of any static platform composite. When a non-default engine
    # is selected, keep its recovery/status tools available even after a user
    # saves an explicit platform toolset list. Preserve the explicit empty-list
    # contract: selecting no configurable tools means no context-engine tools
    # either unless the user adds ``context_engine`` manually later.
    context_cfg = config.get("context") or {}
    if not isinstance(context_cfg, dict):
        context_cfg = {}
    context_engine_name = str(context_cfg.get("engine") or "compressor").strip().lower()
    explicit_empty_selection = (
        platform in platform_toolsets
        and isinstance(platform_toolsets.get(platform), list)
        and not toolset_names
    )
    if context_engine_name and context_engine_name != "compressor" and not explicit_empty_selection:
        enabled_toolsets.add("context_engine")

    # Preserve any explicit non-configurable toolset entries (for example,
    # custom toolsets or MCP server names saved in platform_toolsets).
    explicit_passthrough = {
        ts
        for ts in toolset_names
        if ts not in configurable_keys
        and ts not in plugin_ts_keys
        and ts not in platform_default_keys
    }

    # MCP servers are expected to be available on all platforms by default.
    # If the platform explicitly lists one or more MCP server names, treat that
    # as an allowlist. Otherwise include every globally enabled MCP server.
    # Special sentinel: "no_mcp" in the toolset list disables all MCP servers.
    enabled_mcp_servers = enabled_mcp_server_names(config)
    # Allow "no_mcp" sentinel to opt out of all MCP servers for this platform
    if "no_mcp" in toolset_names:
        explicit_mcp_servers = set()
        enabled_toolsets.update(explicit_passthrough - enabled_mcp_servers - {"no_mcp"})
    else:
        explicit_mcp_servers = explicit_passthrough & enabled_mcp_servers
        enabled_toolsets.update(explicit_passthrough - enabled_mcp_servers)
    if include_default_mcp_servers:
        if explicit_mcp_servers or "no_mcp" in toolset_names:
            enabled_toolsets.update(explicit_mcp_servers)
        else:
            enabled_toolsets.update(enabled_mcp_servers)
    else:
        enabled_toolsets.update(explicit_mcp_servers)

    # Honor agent.disabled_toolsets from config.yaml — allows users to
    # globally suppress specific toolsets (e.g. "memory") across all
    # platforms without per-platform toolset configuration.  This runs
    # last so it overrides everything above.  The value may arrive as a
    # JSON-array string (e.g. "['memory']") from `son-of-anton config set` or a
    # JSON-mode editor save; parse it so the list is not silently dead (#86661).
    agent_cfg = config.get("agent") or {}
    disabled_toolsets = agent_cfg.get("disabled_toolsets") or []
    if disabled_toolsets:
        from agent.skill_utils import parse_config_string_list

        disabled_set = {
            name.strip() for name in parse_config_string_list(disabled_toolsets) if name.strip()
        }
        enabled_toolsets -= disabled_set

    # #38798: if this platform was explicitly configured but every toolset name
    # is invalid (e.g. a migration or hand-edit left `son-of-anton` instead of
    # `son-of-anton-cli`), resolve_toolset() returns [] for each and the platform ends
    # up with no native tools — silently, with no error. Surface it at the point
    # tools are resolved for a session so an already-corrupted config is caught
    # at runtime, not only during the next `son-of-anton update`/`son-of-anton doctor`.
    _explicit = platform_toolsets.get(platform)
    if isinstance(_explicit, list) and _explicit:
        from toolsets import validate_toolset

        _named = [str(t) for t in _explicit if isinstance(t, str) and t]
        if (
            _named
            and not any(validate_toolset(t) for t in _named)
            and platform not in _warned_invalid_platform_toolsets
        ):
            _warned_invalid_platform_toolsets.add(platform)
            logger.warning(
                "platform '%s' has no valid toolsets configured (unknown "
                "name(s): %s) - tools will be unavailable. Run `son-of-anton tools` "
                "to reconfigure. See issue #38798.",
                platform,
                ", ".join(_named),
            )

    return enabled_toolsets


def _save_platform_tools(config: dict, platform: str, enabled_toolset_keys: Set[str]):
    """Save the selected toolset keys for a platform to config.

    Preserves any non-configurable toolset entries (like MCP server names)
    that were already in the config for this platform.
    """
    config.setdefault("platform_toolsets", {})

    # Drop platform-scoped toolsets that don't apply here.  Prevents the
    # "Configure all platforms" checklist (or a hand-edited config.yaml)
    # from turning on, say, the `discord` toolset for another platform.
    enabled_toolset_keys = {
        ts for ts in enabled_toolset_keys
        if _toolset_allowed_for_platform(ts, platform)
    }

    # Get the set of all configurable toolset keys (built-in + plugin)
    configurable_keys = {ts_key for ts_key, _, _ in CONFIGURABLE_TOOLSETS}
    plugin_keys = _get_plugin_toolset_keys()
    configurable_keys |= plugin_keys

    # Also exclude platform default toolsets (son-of-anton-cli, etc.)
    # These are "super" toolsets that resolve to ALL tools, so preserving them
    # would silently override the user's unchecked selections on the next read.
    platform_default_keys = {p["default_toolset"] for p in PLATFORMS.values()}

    # Get existing toolsets for this platform
    existing_toolsets = cfg_get(config, "platform_toolsets", platform, default=[])
    if not isinstance(existing_toolsets, list):
        existing_toolsets = []
    existing_toolsets = [str(ts) for ts in existing_toolsets]

    # Preserve any entries that are NOT configurable toolsets and NOT platform
    # defaults (i.e. only MCP server names should be preserved)
    preserved_entries = {
        entry for entry in existing_toolsets
        if entry not in configurable_keys and entry not in platform_default_keys
    }
    # Opening `son-of-anton tools` is the user's opt-in to reconfigure tools, so treat
    # saving from the picker as consent to clear the "no_mcp" sentinel. The
    # picker has no checkbox for no_mcp, so without this users who once set it
    # by hand could never re-enable MCP servers through the UI.
    preserved_entries.discard("no_mcp")

    # Merge preserved entries with new enabled toolsets
    config["platform_toolsets"][platform] = sorted(enabled_toolset_keys | preserved_entries)

    # Track which plugin toolsets are "known" for this platform so we can
    # distinguish "new plugin, default enabled" from "user disabled it".
    if plugin_keys:
        # setdefault does NOT replace a present-but-null key ("known_plugin_toolsets:"
        # in config.yaml parses to None) — normalize before indexing into it.
        if not isinstance(config.get("known_plugin_toolsets"), dict):
            config["known_plugin_toolsets"] = {}
        config["known_plugin_toolsets"][platform] = sorted(plugin_keys)

    # Same record for builtin toolsets: which ones this platform's checklist
    # has actually put in front of the user. Without it, a toolset the user
    # unchecks here is indistinguishable from one that shipped after they
    # saved. Recorded from the full catalog, since that is what the picker
    # showed.
    if not isinstance(config.get("known_builtin_toolsets"), dict):
        config["known_builtin_toolsets"] = {}
    config["known_builtin_toolsets"][platform] = sorted(
        ts_key for ts_key, _, _ in CONFIGURABLE_TOOLSETS
    )

    # Reconcile with agent.disabled_toolsets. _get_platform_tools() applies
    # that list as a final override AFTER reading platform_toolsets.<platform>,
    # so a toolset listed there stays permanently OFF no matter what this
    # function writes — the toggle "saves" but silently can't ever take
    # effect. Blank Slate installs pre-populate this list with ~27 toolsets,
    # making most of the desktop Toolsets UI unusable for re-enabling
    # anything (issue #49995).
    #
    # Only toolsets the user just explicitly enabled FOR THIS PLATFORM are
    # cleared from the global disabled list — toolsets the user did not
    # touch (still unchecked) or that remain disabled on other platforms
    # are left alone, so agent.disabled_toolsets keeps working as a
    # cross-platform suppression list for anything not actively re-enabled.
    agent_cfg = config.get("agent")
    if isinstance(agent_cfg, dict):
        disabled_toolsets = agent_cfg.get("disabled_toolsets")
        if disabled_toolsets:
            from agent.skill_utils import parse_config_string_list

            parsed_disabled = parse_config_string_list(disabled_toolsets)
            newly_enabled = enabled_toolset_keys - preserved_entries
            if newly_enabled:
                remaining = [
                    ts for ts in parsed_disabled if ts not in newly_enabled
                ]
                if remaining != parsed_disabled:
                    agent_cfg["disabled_toolsets"] = remaining

    save_config(config)


def _toolset_has_keys(
    ts_key: str,
    config: dict = None,
    *,
    force_fresh: bool = False,
    features: Optional[dict] = None,
) -> bool:
    """Check if a toolset's required API keys are configured.

    ``features`` is accepted for caller compatibility and ignored — the
    fork has no managed-subscription rows to count as configured.
    """
    if config is None:
        config = load_config()

    if ts_key == "vision":
        try:
            from agent.auxiliary_client import resolve_vision_provider_client

            _provider, client, _model = resolve_vision_provider_client()
            return client is not None
        except Exception:
            return False

    # Check TOOL_CATEGORIES first (provider-aware)
    cat = TOOL_CATEGORIES.get(ts_key)
    if cat:
        for provider in _visible_providers(
            cat,
            config,
            force_fresh=force_fresh,
            features=features,
        ):
            env_vars = provider.get("env_vars", [])
            if not env_vars:
                return True  # No-key provider (e.g. Local Browser, Edge TTS)
            if all(get_env_value(e["key"]) for e in env_vars):
                return True
        return False

    # Fallback to simple requirements
    requirements = TOOLSET_ENV_REQUIREMENTS.get(ts_key, [])
    if not requirements:
        return True
    return all(get_env_value(var) for var, _ in requirements)


# ─── Menu Helpers ─────────────────────────────────────────────────────────────

def _prompt_choice(question: str, choices: list, default: int = 0) -> int:
    """Single-select menu (arrow keys). Delegates to curses_radiolist."""
    from son_of_anton_cli.curses_ui import curses_radiolist
    return curses_radiolist(question, choices, selected=default, cancel_returns=default)


# ─── Token Estimation ────────────────────────────────────────────────────────

# Profile-keyed cache so one process can serve distinct plugin tool catalogs.
_tool_token_cache: Optional[Dict[tuple[str, int], Dict[str, int]]] = None


def _estimate_tool_tokens() -> Dict[str, int]:
    """Return estimated token counts per individual tool name.

    Uses tiktoken (cl100k_base) to count tokens in the JSON-serialised
    OpenAI-format tool schema.  Triggers tool discovery on first call,
    then caches the result for the rest of the process.

    Returns an empty dict when tiktoken or the registry is unavailable.
    """
    global _tool_token_cache
    from son_of_anton_constants import son_of_anton_home_key

    scope = son_of_anton_home_key()

    try:
        # Trigger full tool discovery (imports all tool modules).
        import model_tools  # noqa: F401
        from tools.registry import registry
        cache_key = (scope, registry._generation)
    except Exception:
        logger.debug("Tool registry unavailable; skipping token estimation")
        cache_key = (scope, -1)
        _tool_token_cache = _tool_token_cache or {}
        _tool_token_cache[cache_key] = {}
        return _tool_token_cache[cache_key]

    if _tool_token_cache is not None and cache_key in _tool_token_cache:
        return _tool_token_cache[cache_key]

    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:
        logger.debug("tiktoken unavailable; skipping tool token estimation")
        _tool_token_cache = _tool_token_cache or {}
        _tool_token_cache[cache_key] = {}
        return _tool_token_cache[cache_key]

    counts: Dict[str, int] = {}
    for name in registry.get_all_tool_names():
        schema = registry.get_schema(name)
        if schema:
            # Mirror what gets sent to the API:
            # {"type": "function", "function": <schema>}
            text = _json.dumps({"type": "function", "function": schema})
            counts[name] = len(enc.encode(text))
    _tool_token_cache = _tool_token_cache or {}
    _tool_token_cache[cache_key] = counts
    return counts


def _prompt_toolset_checklist(
    platform_label: str,
    enabled: Set[str],
    platform: str = "cli",
    *,
    force_fresh: bool = True,
) -> Set[str]:
    """Multi-select checklist of toolsets. Returns set of selected toolset keys."""
    from son_of_anton_cli.curses_ui import curses_checklist
    from toolsets import resolve_toolset

    # Pre-compute per-tool token counts (cached after first call).
    tool_tokens = _estimate_tool_tokens()

    effective_all = _get_effective_configurable_toolsets()
    # Drop platform-scoped toolsets that don't apply to this platform, and
    # config-only capabilities that have no per-platform toggle.
    effective = [
        (k, l, d) for (k, l, d) in effective_all
        if _toolset_allowed_for_platform(k, platform)
        and k not in _CONFIG_ONLY_TOOLSETS
    ]

    labels = []
    for ts_key, ts_label, ts_desc in effective:
        suffix = ""
        if (
            not _toolset_has_keys(ts_key, force_fresh=force_fresh)
            and (TOOL_CATEGORIES.get(ts_key) or TOOLSET_ENV_REQUIREMENTS.get(ts_key))
        ):
            suffix = "  [no API key]"
        labels.append(f"{ts_label}  ({ts_desc}){suffix}")

    pre_selected = {
        i for i, (ts_key, _, _) in enumerate(effective)
        if ts_key in enabled
    }

    # Build a live status function that shows deduplicated total token cost.
    status_fn = None
    if tool_tokens:
        ts_keys = [ts_key for ts_key, _, _ in effective]

        def status_fn(chosen: set) -> str:
            # Collect unique tool names across all selected toolsets
            all_tools: set = set()
            for idx in chosen:
                all_tools.update(resolve_toolset(ts_keys[idx]))
            total = sum(tool_tokens.get(name, 0) for name in all_tools)
            if total >= 1000:
                return f"Est. tool context: ~{total / 1000:.1f}k tokens"
            return f"Est. tool context: ~{total} tokens"

    chosen = curses_checklist(
        f"Tools for {platform_label}",
        labels,
        pre_selected,
        cancel_returns=pre_selected,
        status_fn=status_fn,
    )
    return {effective[i][0] for i in chosen}


# ─── Provider-Aware Configuration ────────────────────────────────────────────

def _configure_toolset(
    ts_key: str,
    config: dict,
    *,
    force_fresh: bool = True,
):
    """Configure a toolset - provider selection + API keys.
    
    Uses TOOL_CATEGORIES for provider-aware config, falls back to simple
    env var prompts for toolsets not in TOOL_CATEGORIES.
    """
    cat = TOOL_CATEGORIES.get(ts_key)

    if cat:
        _configure_tool_category(ts_key, cat, config, force_fresh=force_fresh)
    else:
        # Simple fallback for vision and similar config-less toolsets.
        _configure_simple_requirements(ts_key)


# Mirror of the web-search provider helper for the web backend. Surfaces
# every plugin-registered web provider so it appears in the
# "Web Search & Extract" picker. All seven providers (brave-free, ddgs,
# searxng, exa, parallel, tavily, firecrawl) live as plugins after
# PR #25182 — this helper is the sole source of truth for the category's
# provider rows. The hardcoded entries that used to drive the category
# were deleted in the same PR; only the two non-provider UX rows
# ("Nous Subscription" managed-gateway entry, "Firecrawl Self-Hosted")
# remain in TOOL_CATEGORIES because they describe alternative *setup
# flows* for the firecrawl backend rather than distinct providers.
def _plugin_web_search_providers() -> list[dict]:
    """Build picker-row dicts from plugin-registered web search providers.

    Each returned dict is a regular ``TOOL_CATEGORIES`` provider row. It
    populates both ``web_backend`` (legacy field consumed by setup +
    selection helpers) and ``web_search_plugin_name`` (informational
    marker) so the picker behaves identically whether a provider is
    hardcoded or plugin-registered.

    After PR #25182, all seven web providers (brave-free, ddgs, searxng,
    exa, parallel, tavily, firecrawl) are plugins; this helper is the sole
    source of provider rows for the Web Search & Extract category.
    """
    try:
        from agent.web_search_registry import list_providers as _list_web_providers
        from son_of_anton_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        providers = _list_web_providers()
    except Exception:
        return []

    rows: list[dict] = []
    for provider in providers:
        name = getattr(provider, "name", None)
        if not name:
            continue
        try:
            schema = provider.get_setup_schema()
        except Exception:
            continue
        if not isinstance(schema, dict):
            continue
        # A schema may expose tier ``variants`` (e.g. Exa/Parallel free
        # keyless endpoint vs paid SDK) — flatten the base row plus each
        # variant into separate picker rows sharing the same backend name,
        # distinguished by ``web_tier`` (persisted to
        # ``web.provider_tier.<name>`` on selection).
        schemas = [schema] + [
            v for v in (schema.get("variants") or []) if isinstance(v, dict)
        ]
        for entry in schemas:
            row = {
                "name": entry.get("name", provider.display_name),
                "badge": entry.get("badge", ""),
                "tag": entry.get("tag", ""),
                "env_vars": entry.get("env_vars", []),
                "web_backend": name,
                "web_search_plugin_name": name,
            }
            if entry.get("web_tier"):
                row["web_tier"] = entry["web_tier"]
            # Optional pass-through fields the schema can opt into.
            if entry.get("post_setup"):
                row["post_setup"] = entry["post_setup"]
            rows.append(row)
    return rows


def web_provider_capabilities(backend: str) -> list:
    """Return the capabilities (``search`` / ``extract``) a web backend supports.

    Consults the plugin registry's provider instance (``supports_search`` /
    ``supports_extract``) so the Capabilities GUI can offer per-capability
    selection (``web.search_backend`` / ``web.extract_backend``) only where it
    makes sense — e.g. ddgs and brave-free are search-only. Falls back to both
    capabilities when the backend isn't registered (hardcoded setup-flow rows
    like the managed Firecrawl entries resolve before plugin discovery in some
    test contexts, and firecrawl itself supports both).
    """
    try:
        from agent.web_search_registry import get_provider

        provider = get_provider(backend)
        if provider is not None:
            caps = []
            if provider.supports_search():
                caps.append("search")
            if provider.supports_extract():
                caps.append("extract")
            return caps
    except Exception:
        pass
    return ["search", "extract"]


def _visible_providers(
    cat: dict,
    config: dict,
    *,
    force_fresh: bool = False,
    features: Optional[dict] = None,
) -> list[dict]:
    """Return provider entries visible for the current auth/config state.

    ``features`` is accepted for caller compatibility and ignored — the
    fork has no managed-subscription rows to filter.
    """
    visible = list(cat.get("providers", []))

    # Inject plugin-registered web search backends. After PR #25182, this
    # is the SOLE source of provider rows for the Web Search & Extract
    # category — the per-provider hardcoded entries were deleted. The one
    # remaining hardcoded row ("Firecrawl Self-Hosted") is a non-provider UX
    # setup-flow row for firecrawl.
    if cat.get("name") == "Web Search & Extract":
        visible.extend(_plugin_web_search_providers())

    return visible


_POST_SETUP_INSTALLED: dict = {
    # post_setup_key -> predicate(): True when the install side-effect
    # is already satisfied. Used by `_toolset_needs_configuration_prompt`
    # to force the provider-setup flow when a no-key provider still needs
    # a binary/dependency install (otherwise an already-configured user
    # who toggles the toolset on via `son-of-anton tools` gets a silent no-op
    # because the gate sees "no env vars to ask about" and skips the
    # provider-setup flow that would have run the post_setup hook).
    #
    # Only entries here are gated; other post_setup hooks keep their
    # existing behaviour. Add an entry when (a) the post_setup is the ONLY
    # install side-effect for a no-key provider, and (b) an installed-state
    # check is local, bounded, and doesn't trigger a heavy import.
}


def _post_setup_already_installed(post_setup_key: str) -> bool:
    """Return True when the post_setup install side-effect is satisfied."""
    predicate = _POST_SETUP_INSTALLED.get(post_setup_key)
    if predicate is None:
        # No install-state check registered → assume satisfied (don't
        # change behaviour for hooks we haven't explicitly opted in).
        return True
    try:
        return bool(predicate())
    except Exception:
        return True


def _module_installed(module_name: str) -> bool:
    """Cheap importable-without-importing check (no heavy side effects)."""
    import importlib.util

    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


# Python dependencies installed explicitly through ``son-of-anton tools`` are not
# part of the managed runtime's locked ``all`` sync. A runtime replacement
# therefore needs a small, static allowlist that can be snapshotted before the
# old site-packages disappears and restored afterward. Keep these install
# arguments in sync with the corresponding ``_run_post_setup`` branches.
_RESTORABLE_PYTHON_TOOL_DEPENDENCIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ddgs": ("ddgs", ("-U", "ddgs")),
    "langfuse": ("langfuse", ("langfuse",)),
}


def active_restorable_python_tool_dependencies() -> list[str]:
    """Return ``son-of-anton tools`` Python dependencies present in this runtime."""
    return [
        name
        for name, (module_name, _install_args) in (
            _RESTORABLE_PYTHON_TOOL_DEPENDENCIES.items()
        )
        if _module_installed(module_name)
    ]


def restorable_python_tool_dependency(
    name: str,
) -> tuple[str, tuple[str, ...]] | None:
    """Return the import probe and pip arguments for an allowlisted tool."""
    return _RESTORABLE_PYTHON_TOOL_DEPENDENCIES.get(name)


# post_setup_key -> predicate(): True when the install side-effect is already
# satisfied. Used by ``provider_readiness_status`` to decide whether a keyless
# post_setup row (ddgs, langfuse) is honestly "ready" or still "needs_setup".
# Mirrors the installed-checks ``_run_post_setup`` itself performs before
# installing.
_POST_SETUP_READY: dict = {
    "ddgs": lambda: _module_installed("ddgs"),
    "langfuse": lambda: _module_installed("langfuse"),
}


def provider_readiness_status(
    provider: dict,
    config: dict,
    *,
    features=None,
    is_active: Optional[bool] = None,
) -> str:
    """Compute an honest readiness state for a provider picker row.

    Returns one of:

    - ``"ready"``       — usable as-is (keys set / entitled / installed).
    - ``"needs_keys"``  — declares env vars and at least one is unset.
    - ``"needs_auth"``  — needs a sign-in: Nous Portal login/entitlement for
      managed Tool Gateway rows.
    - ``"needs_setup"`` — keyless row whose ``post_setup`` install hook has
      verifiably not run yet (see ``_POST_SETUP_READY``).

    Keyless ≠ usable: this is the server-side truth the GUI "Ready" pill
    renders from (the old client-side heuristic showed Ready for every
    zero-env-var row).

    ``features`` is accepted for caller compatibility and ignored.
    ``is_active`` is the completed-setup fallback signal for post_setup
    hooks with no registered installed-check (selecting a row runs its
    hook, so the active row has been set up).
    """
    env_vars = provider.get("env_vars", [])
    if env_vars:
        if all(get_env_value(e["key"]) for e in env_vars):
            return "ready"
        return "needs_keys"

    post_setup = provider.get("post_setup")
    if post_setup:
        predicate = _POST_SETUP_READY.get(post_setup)
        if predicate is not None:
            try:
                return "ready" if predicate() else "needs_setup"
            except Exception:
                # Flaky detection must not manufacture a warning state.
                return "ready"
        # No reliable installed-check registered → treat the active-provider
        # signal as "setup completed" (selecting the row runs the hook).
        if is_active is None:
            is_active = _is_provider_active(provider, config)
        return "ready" if is_active else "needs_setup"

    return "ready"


def _toolset_needs_configuration_prompt(
    ts_key: str,
    config: dict,
    *,
    force_fresh: bool = False,
) -> bool:
    """Return True when enabling this toolset should open provider setup."""
    cat = TOOL_CATEGORIES.get(ts_key)
    if not cat:
        return not _toolset_has_keys(ts_key, config, force_fresh=force_fresh)

    # If any visible provider has a registered post_setup install-state
    # check that hasn't been satisfied, force the configuration flow so
    # `_configure_provider` invokes `_run_post_setup` and the install
    # actually runs.
    for provider in _visible_providers(cat, config, force_fresh=force_fresh):
        post_setup = provider.get("post_setup")
        if post_setup and not _post_setup_already_installed(post_setup):
            return True

    if ts_key == "web":
        web_cfg = config.get("web", {})
        return not isinstance(web_cfg, dict) or "backend" not in web_cfg

    return not _toolset_has_keys(ts_key, config, force_fresh=force_fresh)


def _configure_tool_category(
    ts_key: str,
    cat: dict,
    config: dict,
    *,
    force_fresh: bool = True,
):
    """Configure a tool category with provider selection."""
    icon = cat.get("icon", "")
    name = cat["name"]
    providers = _visible_providers(cat, config, force_fresh=force_fresh)

    # Check Python version requirement
    if cat.get("requires_python"):
        req = cat["requires_python"]
        if sys.version_info < req:
            print()
            _print_error(f"  {name} requires Python {req[0]}.{req[1]}+ (current: {sys.version_info.major}.{sys.version_info.minor})")
            _print_info("  Upgrade Python and reinstall to enable this tool.")
            return

    if len(providers) == 1:
        # Single provider - configure directly
        provider = providers[0]
        print()
        print(color(f"  --- {icon} {name} ({provider['name']}) ---", Colors.CYAN))
        if provider.get("tag"):
            _print_info(f"  {provider['tag']}")
        # For single-provider tools, show a note if available
        if cat.get("setup_note"):
            _print_info(f"  {cat['setup_note']}")
        _configure_provider(provider, config, force_fresh=force_fresh)
    else:
        # Multiple providers - let user choose
        print()
        # Use custom title if provided (e.g. "Select Search Provider")
        title = cat.get("setup_title", "Choose a provider")
        print(color(f"  --- {icon} {name} - {title} ---", Colors.CYAN))
        if cat.get("setup_note"):
            _print_info(f"  {cat['setup_note']}")
        print()

        # Plain text labels only (no ANSI codes in menu items)
        provider_choices = []
        for p in providers:
            badge = f" [{p['badge']}]" if p.get("badge") else ""
            tag = f" — {p['tag']}" if p.get("tag") else ""
            configured = ""
            env_vars = p.get("env_vars", [])
            if not env_vars or all(get_env_value(v["key"]) for v in env_vars):
                if _is_provider_active(p, config, force_fresh=force_fresh):
                    configured = " [active]"
                elif not env_vars:
                    configured = ""
                else:
                    configured = " [configured]"
            provider_choices.append(f"{p['name']}{badge}{tag}{configured}")

        # Add skip option
        provider_choices.append("Skip — keep defaults / configure later")

        # Detect current provider as default
        default_idx = _detect_active_provider_index(
            providers,
            config,
            force_fresh=force_fresh,
        )

        provider_idx = _prompt_choice(f"  {title}:", provider_choices, default_idx)

        # Skip selected
        if provider_idx >= len(providers):
            _print_info(f"  Skipped {name}")
            return

        _configure_provider(providers[provider_idx], config, force_fresh=force_fresh)


def _web_tier_matches(provider: dict, config: dict) -> bool:
    """Return True when a web picker row's tier matches the configured tier.

    Tiered rows (Exa/Parallel Free vs Paid) share one ``web_backend`` name
    and differ only in ``web_tier``. The configured tier lives at
    ``web.provider_tier.<backend>`` (set on selection). Matching rules:

    - row has no ``web_tier`` → tier-agnostic row, matches (legacy rows)
    - configured tier set     → must equal the row's tier
    - configured tier unset   → "auto": the effective tier is paid when the
      row's env vars are all present, free otherwise — highlight the row
      the runtime would actually use
    """
    row_tier = provider.get("web_tier")
    if not row_tier:
        return True
    web_cfg = config.get("web")
    if not isinstance(web_cfg, dict):
        web_cfg = {}
    tiers = web_cfg.get("provider_tier")
    if not isinstance(tiers, dict):
        tiers = {}
    configured = str(tiers.get(provider["web_backend"], "") or "").lower().strip()
    if configured in ("free", "paid"):
        return configured == row_tier
    # Auto: mirror plugins.web.keyless_mcp.use_keyless — key present → paid.
    try:
        from agent.web_search_provider import get_provider_env

        key_var = {"exa": "EXA_API_KEY", "parallel": "PARALLEL_API_KEY"}.get(
            provider["web_backend"]
        )
        has_key = bool(get_provider_env(key_var)) if key_var else False
    except Exception:
        has_key = False
    return row_tier == ("paid" if has_key else "free")


def _is_provider_active(
    provider: dict,
    config: dict,
    *,
    force_fresh: bool = False,
) -> bool:
    """Check if a provider entry matches the currently active config."""
    if provider.get("web_backend"):
        current = cfg_get(config, "web", "backend")
        if current != provider["web_backend"]:
            return False
        return _web_tier_matches(provider, config)
    return False


def _detect_active_provider_index(
    providers: list,
    config: dict,
    *,
    force_fresh: bool = False,
) -> int:
    """Return the index of the currently active provider, or 0."""
    for i, p in enumerate(providers):
        if _is_provider_active(p, config, force_fresh=force_fresh):
            return i
        # Fallback: env vars present → likely configured
        env_vars = p.get("env_vars", [])
        if env_vars and all(get_env_value(v["key"]) for v in env_vars):
            return i
    return 0


def _write_provider_config(provider: dict, config: dict, *, managed_feature) -> None:
    """Persist the provider/backend config keys for a selected provider.

    This is the pure, non-interactive core of :func:`_configure_provider` —
    it writes ``web.backend`` based on the provider's markers, but does NOT
    prompt for env vars, run post-setup hooks, gate on Nous auth, or run
    interactive model pickers.

    Selection model: every row writes exactly ONE provider string per
    category. Managed "Nous Subscription" rows write ``nous``; BYOK rows
    write the vendor name. ``use_gateway`` is no longer written — a fresh
    pick removes any legacy key from the touched section so the read-time
    legacy shim (use_gateway: true => nous) cannot override the new choice.
    """
    def _set_selection(section_key: str, name_key: str, vendor_value) -> None:
        section = config.setdefault(section_key, {})
        if not isinstance(section, dict):
            section = {}
            config[section_key] = section
        section[name_key] = vendor_value
        section.pop("use_gateway", None)

    # Set web search backend in config if applicable
    if provider.get("web_backend"):
        _set_selection("web", "backend", provider["web_backend"])
        web_cfg = config.get("web")
        if isinstance(web_cfg, dict):
            if provider.get("web_tier"):
                tiers = web_cfg.setdefault("provider_tier", {})
                if isinstance(tiers, dict):
                    tiers[provider["web_backend"]] = provider["web_tier"]
            else:
                stale_tiers = web_cfg.get("provider_tier")
                if isinstance(stale_tiers, dict):
                    stale_tiers.pop(provider["web_backend"], None)

    # Managed rows for categories without a marker handled above still
    # persist the "nous" selection.
    if managed_feature and managed_feature != "web":
        section = config.setdefault(managed_feature, {})
        if isinstance(section, dict):
            section.pop("use_gateway", None)
    elif not managed_feature:
        # User picked a non-gateway provider — clear any stale legacy
        # use_gateway key on the category so the read-time shim cannot
        # override the fresh selection.
        if "web_backend" in provider:
            section = config.get("web")
            if isinstance(section, dict):
                section.pop("use_gateway", None)


def apply_provider_selection(ts_key: str, provider_name: str, config: dict) -> None:
    """Non-interactively persist a provider selection for a toolset.

    Resolves ``provider_name`` within ``ts_key``'s category (matching the
    rows the GUI/CLI picker shows via :func:`_visible_providers`) and writes
    the corresponding backend/provider config keys. Unlike
    :func:`_configure_provider`, this does NOT prompt for API keys, run
    post-setup hooks, gate on Nous Portal auth, or run interactive model
    pickers — those are handled separately (env endpoints, post-setup
    endpoints, the model picker) in the GUI.

    Raises ``KeyError`` if the toolset has no category or the provider name
    is not found among the visible providers.
    """
    cat = TOOL_CATEGORIES.get(ts_key)
    if cat is None:
        raise KeyError(f"Toolset has no configurable category: {ts_key}")

    providers = _visible_providers(cat, config, force_fresh=True)
    provider = next((p for p in providers if p.get("name") == provider_name), None)
    if provider is None:
        raise KeyError(f"Unknown provider {provider_name!r} for toolset {ts_key!r}")

    managed_feature = provider.get("managed_nous_feature")
    _write_provider_config(provider, config, managed_feature=managed_feature)


def _configure_provider(
    provider: dict,
    config: dict,
    *,
    force_fresh: bool = True,
):
    """Configure a single provider - prompt for API keys and set config."""
    env_vars = provider.get("env_vars", [])
    managed_feature = provider.get("managed_nous_feature")

    # Persist the provider/backend config keys + use_gateway flags. Shared
    # with the GUI provider-select endpoint via apply_provider_selection so
    # there is a single source of truth for these writes.
    _write_provider_config(provider, config, managed_feature=managed_feature)

    # Set web search backend in config if applicable
    if provider.get("web_backend"):
        _print_success(f"  Web backend set to: {provider['web_backend']}")

    if not env_vars:
        if provider.get("post_setup"):
            _run_post_setup(provider["post_setup"])
        _print_success(f"  {provider['name']} - no configuration needed!")
        if managed_feature:
            _print_info("  Requests for this tool will be billed to your Nous subscription.")

    # Prompt for each required env var
    all_configured = True
    for var in env_vars:
        existing = get_env_value(var["key"])
        if existing:
            _print_success(f"  {var['key']}: already configured")
            # Don't ask to update - this is a new enable flow.
            # Reconfigure is handled separately.
        else:
            url = var.get("url", "")
            if url:
                _print_info(f"  Get yours at: {url}")

            default_val = var.get("default", "")
            if default_val:
                value = _prompt(f"    {var.get('prompt', var['key'])}", default_val)
            else:
                value = _prompt(f"    {var.get('prompt', var['key'])}", password=True)

            if value:
                save_env_value(var["key"], value)
                _print_success("    Saved")
            else:
                _print_warning("    Skipped")
                all_configured = False

    # Run post-setup hooks if needed
    if provider.get("post_setup") and all_configured:
        _run_post_setup(provider["post_setup"])

    if all_configured:
        _print_success(f"  {provider['name']} configured!")


def _configure_vision_backend() -> None:
    """Interactive vision-backend configuration.

    Vision is an auxiliary task whose provider/model are resolved from
    ``auxiliary.vision.{provider,model,base_url}`` in config.yaml (see
    ``agent/auxiliary_client.resolve_vision_provider_client``). Rather than
    forcing the user onto OpenRouter, let them pick any authenticated
    provider + model — the same surface as ``son-of-anton model`` — or point at a
    custom OpenAI-compatible endpoint. "Auto" leaves the config keys empty so
    the resolver uses the main model / aggregator fallback chain.
    """
    print()
    print(color("  Vision / Image Analysis needs a multimodal model.", Colors.YELLOW))
    print(color(
        "  Pick any provider + model (like /model), or let it auto-detect.",
        Colors.DIM,
    ))

    choices = [
        "Auto — use your main model / aggregator fallback (recommended)",
        "Pick a provider and model",
        "Custom OpenAI-compatible endpoint — base URL, API key, model",
        "Skip",
    ]
    idx = _prompt_choice("  Configure vision backend", choices, 0)

    config = load_config()
    aux = config.setdefault("auxiliary", {})
    if not isinstance(aux, dict):
        aux = {}
        config["auxiliary"] = aux
    vision_cfg = aux.setdefault("vision", {})
    if not isinstance(vision_cfg, dict):
        vision_cfg = {}
        aux["vision"] = vision_cfg

    if idx == 0:
        # Auto: clear any pinned override so the resolver auto-detects.
        for key in ("provider", "model", "base_url", "api_key", "api_mode"):
            vision_cfg.pop(key, None)
        save_config(config)
        _print_success("  Vision set to auto (main model / aggregator fallback)")
        return

    if idx == 1:
        _configure_vision_provider_model(config, vision_cfg)
        return

    if idx == 2:
        base_url = _prompt("    Base URL (blank for OpenAI)").strip() or "https://api.openai.com/v1"
        is_native_openai = base_url_hostname(base_url) == "api.openai.com"
        key_label = "    OPENAI_API_KEY" if is_native_openai else "    API key"
        api_key = _prompt(key_label, password=True)
        if not (api_key and api_key.strip()):
            _print_warning("    Skipped")
            return
        default_model = "gpt-4o-mini" if is_native_openai else ""
        model = _prompt(
            f"    Vision model{f' (blank for {default_model})' if default_model else ''}"
        ).strip() or default_model
        save_env_value("OPENAI_API_KEY", api_key.strip())
        # Only base_url + model go to config.yaml; the key is the secret.
        # Pin provider="custom" so the resolver routes through this endpoint —
        # leaving it at the "auto" default would make _resolve_task_provider_model
        # ignore the base_url (it only honors base_url when paired with an
        # api_key in config or a non-auto provider).
        vision_cfg["provider"] = "custom"
        vision_cfg["base_url"] = base_url
        if model:
            vision_cfg["model"] = model
        else:
            vision_cfg.pop("model", None)
        save_config(config)
        _print_success(f"  Vision set to custom endpoint{f' ({model})' if model else ''}")
        return

    # Skip
    _print_info("  Skipped vision configuration")


def _configure_vision_provider_model(config: dict, vision_cfg: dict) -> None:
    """Provider + model picker for vision, mirroring the ``/model`` surface.

    Provider rows come from ``build_aux_picker_rows()`` — the shared aux-picker
    substrate — so this picker lists exactly what the ``son-of-anton model`` aux-task
    picker lists, including the user's own ``providers:`` / ``custom_providers:``
    endpoints. Lets the user pick a provider and then a model from its curated
    list (or type a custom id), and persists ``auxiliary.vision.provider`` +
    ``.model``.
    """
    try:
        from son_of_anton_cli.inventory import (
            build_aux_picker_rows,
            format_aux_picker_entries,
        )
    except Exception as exc:  # pragma: no cover - import guard
        _print_warning(f"  Could not load provider list: {exc}")
        return

    current_provider = str(vision_cfg.get("provider") or "").strip()
    current_model = str(vision_cfg.get("model") or "").strip()
    current_base_url = str(vision_cfg.get("base_url") or "").strip()

    try:
        providers = build_aux_picker_rows(
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
            max_models=40,
        )
    except Exception as exc:
        _print_warning(f"  Could not detect providers: {exc}")
        providers = []

    if not providers:
        _print_warning(
            "  No authenticated providers found. Configure a provider first "
            "with `son-of-anton model`, then re-run this."
        )
        return

    provider_labels = [
        label
        for _slug, label, _models in format_aux_picker_entries(
            providers,
            current_provider=current_provider,
            current_base_url=current_base_url,
        )
    ]
    provider_labels.append("Cancel")

    pidx = _prompt_choice("  Choose vision provider:", provider_labels, 0)
    if pidx >= len(providers):
        _print_info("  Cancelled")
        return

    chosen = providers[pidx]
    slug = chosen.get("slug")
    models = list(chosen.get("models", []))

    model_choices = list(models) + ["Type a custom model id…"]
    midx = _prompt_choice(
        f"  Choose vision model for {chosen.get('name') or slug}:",
        model_choices,
        0,
    )
    if midx < len(models):
        model = models[midx]
    else:
        model = _prompt("    Model id").strip()
        if not model:
            _print_warning("  No model entered — cancelled")
            return

    vision_cfg["provider"] = slug
    vision_cfg["model"] = model
    # A provider selection supersedes any prior custom endpoint override.
    vision_cfg.pop("base_url", None)
    vision_cfg.pop("api_key", None)
    save_config(config)
    _print_success(f"  Vision set to {slug} / {model}")


def _configure_simple_requirements(ts_key: str):
    """Simple fallback for toolsets that just need env vars (no provider selection)."""
    if ts_key == "vision":
        if _toolset_has_keys("vision"):
            return
        _configure_vision_backend()
        return

    requirements = TOOLSET_ENV_REQUIREMENTS.get(ts_key, [])
    if not requirements:
        return

    missing = [(var, url) for var, url in requirements if not get_env_value(var)]
    if not missing:
        return

    ts_label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts_key), ts_key)
    print()
    print(color(f"  {ts_label} requires configuration:", Colors.YELLOW))

    for var, url in missing:
        if url:
            _print_info(f"  Get key at: {url}")
        value = _prompt(f"    {var}", password=True)
        if value and value.strip():
            save_env_value(var, value.strip())
            _print_success("    Saved")
        else:
            _print_warning("    Skipped")


def _reconfigure_tool(
    config: dict,
    *,
    force_fresh: bool = True,
):
    """Let user reconfigure an existing tool's provider or API key."""
    # Build list of configurable tools that are currently set up
    configurable = []
    for ts_key, ts_label, _ in _get_effective_configurable_toolsets():
        cat = TOOL_CATEGORIES.get(ts_key)
        reqs = TOOLSET_ENV_REQUIREMENTS.get(ts_key)
        if cat or reqs:
            if (
                _toolset_has_keys(ts_key, config, force_fresh=force_fresh)
                or _toolset_enabled_for_reconfigure(ts_key, config)
            ):
                configurable.append((ts_key, ts_label))

    if not configurable:
        _print_info("No configured tools to reconfigure.")
        return

    choices = [label for _, label in configurable]
    choices.append("Cancel")

    idx = _prompt_choice("  Which tool would you like to reconfigure?", choices, len(choices) - 1)

    if idx >= len(configurable):
        return  # Cancel

    ts_key, ts_label = configurable[idx]
    cat = TOOL_CATEGORIES.get(ts_key)

    if cat:
        _configure_tool_category_for_reconfig(
            ts_key,
            cat,
            config,
            force_fresh=force_fresh,
        )
    else:
        _reconfigure_simple_requirements(ts_key)

    save_config(config)


def _toolset_enabled_for_reconfigure(ts_key: str, config: dict) -> bool:
    """Return True if a configurable toolset is enabled anywhere.

    Reconfigure must include enabled-but-unconfigured categories so users can
    finish provider/API-key setup without disabling and re-enabling the toolset.
    """
    for platform in PLATFORMS:
        if not _toolset_allowed_for_platform(ts_key, platform):
            continue
        try:
            enabled = _get_platform_tools(
                config,
                platform,
                include_default_mcp_servers=False,
            )
        except Exception:
            continue
        if ts_key in enabled:
            return True
    return False


def _configure_tool_category_for_reconfig(
    ts_key: str,
    cat: dict,
    config: dict,
    *,
    force_fresh: bool = True,
):
    """Reconfigure a tool category - provider selection + API key update."""
    icon = cat.get("icon", "")
    name = cat["name"]
    providers = _visible_providers(cat, config, force_fresh=force_fresh)

    if len(providers) == 1:
        provider = providers[0]
        print()
        print(color(f"  --- {icon} {name} ({provider['name']}) ---", Colors.CYAN))
        if hidden_nous_message:
            for line in hidden_nous_message.splitlines():
                _print_warning(f"  {line}")
        _reconfigure_provider(provider, config, force_fresh=force_fresh)
    else:
        print()
        print(color(f"  --- {icon} {name} - Choose a provider ---", Colors.CYAN))
        print()

        provider_choices = []
        for p in providers:
            badge = f" [{p['badge']}]" if p.get("badge") else ""
            tag = f" — {p['tag']}" if p.get("tag") else ""
            configured = ""
            env_vars = p.get("env_vars", [])
            if not env_vars or all(get_env_value(v["key"]) for v in env_vars):
                if _is_provider_active(p, config, force_fresh=force_fresh):
                    configured = " [active]"
                elif not env_vars:
                    configured = ""
                else:
                    configured = " [configured]"
            provider_choices.append(f"{p['name']}{badge}{tag}{configured}")

        default_idx = _detect_active_provider_index(
            providers,
            config,
            force_fresh=force_fresh,
        )

        provider_idx = _prompt_choice("  Select provider:", provider_choices, default_idx)
        _reconfigure_provider(
            providers[provider_idx],
            config,
            force_fresh=force_fresh,
        )


def _reconfigure_provider(
    provider: dict,
    config: dict,
    *,
    force_fresh: bool = True,
):
    """Reconfigure a provider - update API keys."""
    env_vars = provider.get("env_vars", [])

    # Selection model (mirrors _write_provider_config): every row writes ONE
    # provider string per category — the vendor name for BYOK rows — and
    # drops any legacy use_gateway key.
    # Set web search backend in config if applicable
    if provider.get("web_backend"):
        web_cfg = config.setdefault("web", {})
        web_cfg["backend"] = provider["web_backend"]
        web_cfg.pop("use_gateway", None)
        if provider.get("web_tier"):
            tiers = web_cfg.setdefault("provider_tier", {})
            if isinstance(tiers, dict):
                tiers[provider["web_backend"]] = provider["web_tier"]
            _print_success(
                f"  Web backend set to: {provider['web_backend']} "
                f"({provider['web_tier']} tier)"
            )
        else:
            stale_tiers = web_cfg.get("provider_tier")
            if isinstance(stale_tiers, dict):
                stale_tiers.pop(provider["web_backend"], None)
            _print_success(f"  Web backend set to: {provider['web_backend']}")


    if not env_vars:
        if provider.get("post_setup"):
            _run_post_setup(provider["post_setup"])
        _print_success(f"  {provider['name']} - no configuration needed!")
        if managed_feature:
            _print_info("  Requests for this tool will be billed to your Nous subscription.")

    for var in env_vars:
        existing = get_env_value(var["key"])
        if existing:
            _print_info(f"  {var['key']}: configured ({existing[:8]}...)")
        url = var.get("url", "")
        if url:
            _print_info(f"  Get yours at: {url}")
        default_val = var.get("default", "")
        value = _prompt(f"    {var.get('prompt', var['key'])} (Enter to keep current)", password=not default_val)
        if value and value.strip():
            save_env_value(var["key"], value.strip())
            _print_success("    Updated")
        else:
            _print_info("    Kept current")

    if provider.get("post_setup"):
        _run_post_setup(provider["post_setup"])



def _reconfigure_simple_requirements(ts_key: str):
    """Reconfigure simple env var requirements."""
    if ts_key == "vision":
        # Vision has its own provider/model picker (any provider, like
        # `son-of-anton model`). Run it directly so reconfigure doesn't fall back to
        # the generic single-key prompt (which would re-ask for OPENROUTER_API_KEY).
        _configure_vision_backend()
        return

    requirements = TOOLSET_ENV_REQUIREMENTS.get(ts_key, [])
    if not requirements:
        return

    ts_label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts_key), ts_key)
    print()
    print(color(f"  {ts_label}:", Colors.CYAN))

    for var, url in requirements:
        existing = get_env_value(var)
        if existing:
            _print_info(f"  {var}: configured ({existing[:8]}...)")
        if url:
            _print_info(f"  Get key at: {url}")
        value = _prompt(f"    {var} (Enter to keep current)", password=True)
        if value and value.strip():
            save_env_value(var, value.strip())
            _print_success("    Updated")
        else:
            _print_info("    Kept current")


# ─── Main Entry Point ─────────────────────────────────────────────────────────

def tools_command(args=None, first_install: bool = False, config: dict = None):
    """Entry point for `son-of-anton tools` and `son-of-anton setup tools`.

    Args:
        first_install: When True (set by the setup wizard on fresh installs),
            skip the platform menu, go straight to the CLI checklist, and
            prompt for API keys on all enabled tools that need them.
        config: Optional config dict to use.  When called from the setup
            wizard, the wizard passes its own dict so that platform_toolsets
            are written into it and survive the wizard's final save_config().
    """
    if config is None:
        config = load_config()
    enabled_platforms = _get_enabled_platforms()

    print()

    # Non-interactive summary mode for CLI usage
    if getattr(args, "summary", False):
        total = len(_get_effective_configurable_toolsets())
        print(color("⚛ Tool Summary", Colors.CYAN, Colors.BOLD))
        print()
        summary = _platform_toolset_summary(config, enabled_platforms)
        for pkey in enabled_platforms:
            pinfo = PLATFORMS[pkey]
            enabled = summary.get(pkey, set())
            count = len(enabled)
            print(color(f"  {pinfo['label']}", Colors.BOLD) + color(f"  ({count}/{total})", Colors.DIM))
            if enabled:
                for ts_key in sorted(enabled):
                    label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts_key), ts_key)
                    print(color(f"    ✓ {label}", Colors.GREEN))
            else:
                print(color("    (none enabled)", Colors.DIM))
        print()
        return
    print(color("⚛ Son of Anton Tool Configuration", Colors.CYAN, Colors.BOLD))
    print(color("  Enable or disable tools per platform.", Colors.DIM))
    print(color("  Tools that need API keys will be configured when enabled.", Colors.DIM))
    print(color("  Guide: https://son-of-anton.nousresearch.com/docs/user-guide/features/tools", Colors.DIM))
    print()

    # ── First-time install: linear flow, no platform menu ──
    if first_install:
        for pkey in enabled_platforms:
            pinfo = PLATFORMS[pkey]
            current_enabled = _get_platform_tools(config, pkey, include_default_mcp_servers=False)

            # Uncheck toolsets that should be off by default
            checklist_preselected = current_enabled - _DEFAULT_OFF_TOOLSETS

            # Show checklist
            new_enabled = _prompt_toolset_checklist(pinfo["label"], checklist_preselected, pkey)

            # Only diff against toolsets the checklist actually offered. The
            # resolved ``current_enabled`` can include non-configurable
            # toolsets and recovered platform composites the user was
            # never shown a checkbox for; without this scope the summary would
            # print spurious removals even though the config keeps
            # them. See _checklist_toolset_keys.
            _diff_universe = _checklist_toolset_keys(pkey)
            added = (new_enabled - current_enabled) & _diff_universe
            removed = (current_enabled - new_enabled) & _diff_universe
            if added:
                for ts in sorted(added):
                    label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts), ts)
                    print(color(f"  + {label}", Colors.GREEN))
            if removed:
                for ts in sorted(removed):
                    label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts), ts)
                    print(color(f"  - {label}", Colors.RED))

            auto_configured = apply_nous_managed_defaults(
                config,
                enabled_toolsets=new_enabled,
                force_fresh=True,
            )
            for ts_key in sorted(auto_configured):
                label = next((l for k, l, _ in CONFIGURABLE_TOOLSETS if k == ts_key), ts_key)
                print(color(f"  ✓ {label}: using your Nous subscription defaults", Colors.GREEN))

            # Walk through ALL selected tools that have provider options or
            # need API keys.  This ensures providers are shown even when
            # a free provider exists.
            to_configure = [
                ts_key for ts_key in sorted(new_enabled)
                if (TOOL_CATEGORIES.get(ts_key) or TOOLSET_ENV_REQUIREMENTS.get(ts_key))
                and ts_key not in auto_configured
            ]

            if to_configure:
                print()
                print(color(f"  Configuring {len(to_configure)} tool(s):", Colors.YELLOW))
                for ts_key in to_configure:
                    label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts_key), ts_key)
                    print(color(f"    • {label}", Colors.DIM))
                print(color("  You can skip any tool you don't need right now.", Colors.DIM))
                print()
                for ts_key in to_configure:
                    _configure_toolset(ts_key, config)

            _save_platform_tools(config, pkey, new_enabled)
            save_config(config)
            print(color(f"  ✓ Saved {pinfo['label']} tool configuration", Colors.GREEN))
            print()

        return

    # ── Returning user: platform menu loop ──
    # Build platform choices
    platform_choices = []
    platform_keys = []
    for pkey in enabled_platforms:
        pinfo = PLATFORMS[pkey]
        current = _get_platform_tools(config, pkey, include_default_mcp_servers=False)
        count = len(current)
        total = len(_get_effective_configurable_toolsets())
        platform_choices.append(f"Configure {pinfo['label']}  ({count}/{total} enabled)")
        platform_keys.append(pkey)

    if len(platform_keys) > 1:
        platform_choices.append("Configure all platforms (global)")
    platform_choices.append("Reconfigure an existing tool's provider or API key")

    # Show MCP option if any MCP servers are configured
    _has_mcp = bool(config.get("mcp_servers"))
    if _has_mcp:
        platform_choices.append("Configure MCP server tools")

    platform_choices.append("Done")

    # Index offsets for the extra options after per-platform entries
    _global_idx = len(platform_keys) if len(platform_keys) > 1 else -1
    _reconfig_idx = len(platform_keys) + (1 if len(platform_keys) > 1 else 0)
    _mcp_idx = (_reconfig_idx + 1) if _has_mcp else -1
    _done_idx = _reconfig_idx + (2 if _has_mcp else 1)

    while True:
        idx = _prompt_choice("Select an option:", platform_choices, default=0)

        # "Done" selected
        if idx == _done_idx:
            break

        # "Reconfigure" selected
        if idx == _reconfig_idx:
            _reconfigure_tool(config, force_fresh=True)
            print()
            continue

        # "Configure MCP tools" selected
        if idx == _mcp_idx:
            _configure_mcp_tools_interactive(config)
            print()
            continue

        # "Configure all platforms (global)" selected
        if idx == _global_idx:
            # Use the union of all platforms' current tools as the starting state
            all_current = set()
            for pk in platform_keys:
                all_current |= _get_platform_tools(config, pk, include_default_mcp_servers=False)
            new_enabled = _prompt_toolset_checklist(
                "All platforms",
                all_current,
                force_fresh=True,
            )
            selected_to_configure = [
                ts_key for ts_key in sorted(new_enabled)
                if (TOOL_CATEGORIES.get(ts_key) or TOOLSET_ENV_REQUIREMENTS.get(ts_key))
                and _toolset_needs_configuration_prompt(
                    ts_key,
                    config,
                    force_fresh=True,
                )
            ]

            selected_to_configure_set = set(selected_to_configure)

            if selected_to_configure:
                print()
                print(color(f"  Configuring {len(selected_to_configure)} selected tool(s):", Colors.YELLOW))
                for ts_key in selected_to_configure:
                    label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts_key), ts_key)
                    print(color(f"    • {label}", Colors.DIM))
                print(color("  You can skip any tool you don't need right now.", Colors.DIM))
                print()
                for ts_key in selected_to_configure:
                    _configure_toolset(ts_key, config)

            if new_enabled != all_current or selected_to_configure:
                for pk in platform_keys:
                    prev = _get_platform_tools(config, pk, include_default_mcp_servers=False)
                    # Scope the printed diff to the checklist's universe (see
                    # _checklist_toolset_keys) so non-configurable toolsets aren't
                    # reported as added/removed.
                    _diff_universe = _checklist_toolset_keys(pk)
                    added = (new_enabled - prev) & _diff_universe
                    removed = (prev - new_enabled) & _diff_universe
                    pinfo_inner = PLATFORMS[pk]
                    if added or removed:
                        print(color(f"  {pinfo_inner['label']}:", Colors.DIM))
                        for ts in sorted(added):
                            label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts), ts)
                            print(color(f"    + {label}", Colors.GREEN))
                        for ts in sorted(removed):
                            label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts), ts)
                            print(color(f"    - {label}", Colors.RED))
                    # Configure API keys for newly enabled tools not already
                    # handled by the global selected-tool pass above. This
                    # preserves the old per-platform enable flow but avoids
                    # dropping users back to the main menu when a selected tool
                    # was already enabled globally and only lacked provider
                    # configuration.
                    for ts_key in sorted(added - selected_to_configure_set):
                        if (TOOL_CATEGORIES.get(ts_key) or TOOLSET_ENV_REQUIREMENTS.get(ts_key)):
                            if _toolset_needs_configuration_prompt(
                                ts_key,
                                config,
                                force_fresh=True,
                            ):
                                _configure_toolset(ts_key, config)
                    _save_platform_tools(config, pk, new_enabled)
                save_config(config)
                print(color("  ✓ Saved configuration for all platforms", Colors.GREEN))
                # Update choice labels
                for ci, pk in enumerate(platform_keys):
                    new_count = len(_get_platform_tools(config, pk, include_default_mcp_servers=False))
                    total = len(_get_effective_configurable_toolsets())
                    platform_choices[ci] = f"Configure {PLATFORMS[pk]['label']}  ({new_count}/{total} enabled)"
            else:
                print(color("  No changes", Colors.DIM))
            print()
            continue

        pkey = platform_keys[idx]
        pinfo = PLATFORMS[pkey]

        # Get current enabled toolsets for this platform
        current_enabled = _get_platform_tools(config, pkey, include_default_mcp_servers=False)

        # Show checklist
        new_enabled = _prompt_toolset_checklist(
            pinfo["label"],
            current_enabled,
            force_fresh=True,
        )

        # Selected toolsets still missing provider/API-key setup must open
        # configuration even when the checklist selection itself didn't
        # change (e.g. Web Search already enabled but web.backend missing).
        # Mirrors the "Configure all platforms (global)" flow above.
        selected_to_configure = [
            ts_key for ts_key in sorted(new_enabled)
            if (TOOL_CATEGORIES.get(ts_key) or TOOLSET_ENV_REQUIREMENTS.get(ts_key))
            and _toolset_needs_configuration_prompt(
                ts_key,
                config,
                force_fresh=True,
            )
        ]

        selected_to_configure_set = set(selected_to_configure)

        if selected_to_configure:
            print()
            print(color(f"  Configuring {len(selected_to_configure)} selected tool(s):", Colors.YELLOW))
            for ts_key in selected_to_configure:
                label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts_key), ts_key)
                print(color(f"    • {label}", Colors.DIM))
            print(color("  You can skip any tool you don't need right now.", Colors.DIM))
            print()
            for ts_key in selected_to_configure:
                _configure_toolset(ts_key, config)

        if new_enabled != current_enabled or selected_to_configure:
            # Scope the printed diff to the checklist's universe (see
            # _checklist_toolset_keys) so non-configurable toolsets aren't
            # reported as added/removed.
            _diff_universe = _checklist_toolset_keys(pkey)
            added = (new_enabled - current_enabled) & _diff_universe
            removed = (current_enabled - new_enabled) & _diff_universe

            if added:
                for ts in sorted(added):
                    label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts), ts)
                    print(color(f"  + {label}", Colors.GREEN))
            if removed:
                for ts in sorted(removed):
                    label = next((l for k, l, _ in _get_effective_configurable_toolsets() if k == ts), ts)
                    print(color(f"  - {label}", Colors.RED))

            # Configure newly enabled toolsets that need API keys, skipping
            # any already handled by the selected-tool pass above.
            for ts_key in sorted(added - selected_to_configure_set):
                if (TOOL_CATEGORIES.get(ts_key) or TOOLSET_ENV_REQUIREMENTS.get(ts_key)):
                    if _toolset_needs_configuration_prompt(
                        ts_key,
                        config,
                        force_fresh=True,
                    ):
                        _configure_toolset(ts_key, config)

            _save_platform_tools(config, pkey, new_enabled)
            save_config(config)
            print(color(f"  ✓ Saved {pinfo['label']} configuration", Colors.GREEN))
        else:
            print(color(f"  No changes to {pinfo['label']}", Colors.DIM))

        print()

        # Update the choice label with new count
        new_count = len(_get_platform_tools(config, pkey, include_default_mcp_servers=False))
        total = len(_get_effective_configurable_toolsets())
        platform_choices[idx] = f"Configure {pinfo['label']}  ({new_count}/{total} enabled)"

    print()
    from son_of_anton_constants import display_son_of_anton_home
    print(color(f"  Tool configuration saved to {display_son_of_anton_home()}/config.yaml", Colors.DIM))
    print(color("  Changes take effect on next 'son-of-anton' or gateway restart.", Colors.DIM))
    print()


# ─── MCP Tools Interactive Configuration ─────────────────────────────────────


def _configure_mcp_tools_interactive(config: dict):
    """Probe MCP servers for available tools and let user toggle them on/off.

    Connects to each configured MCP server, discovers tools, then shows
    a per-server curses checklist.  Writes changes back as ``tools.exclude``
    entries in config.yaml.
    """
    from son_of_anton_cli.curses_ui import curses_checklist

    mcp_servers = config.get("mcp_servers") or {}
    if not mcp_servers:
        _print_info("No MCP servers configured.")
        return

    # Count enabled servers
    enabled_names = [
        k for k, v in mcp_servers.items()
        if v.get("enabled", True) not in {False, "false", "0", "no", "off"}
    ]
    if not enabled_names:
        _print_info("All MCP servers are disabled.")
        return

    print()
    print(color("  Discovering tools from MCP servers...", Colors.YELLOW))
    print(color(f"  Connecting to {len(enabled_names)} server(s): {', '.join(enabled_names)}", Colors.DIM))

    try:
        from tools.mcp_tool import probe_mcp_server_tools
        server_tools = probe_mcp_server_tools()
    except Exception as exc:
        _print_error(f"Failed to probe MCP servers: {exc}")
        return

    if not server_tools:
        _print_warning("Could not discover tools from any MCP server.")
        _print_info("Check that server commands/URLs are correct and dependencies are installed.")
        return

    # Report discovery results
    failed = [n for n in enabled_names if n not in server_tools]
    if failed:
        for name in failed:
            _print_warning(f"  Could not connect to '{name}'")

    total_tools = sum(len(tools) for tools in server_tools.values())
    print(color(f"  Found {total_tools} tool(s) across {len(server_tools)} server(s)", Colors.GREEN))
    print()

    any_changes = False

    for server_name, tools in server_tools.items():
        if not tools:
            _print_info(f"  {server_name}: no tools found")
            continue

        srv_cfg = mcp_servers.get(server_name, {})
        tools_cfg = srv_cfg.get("tools") or {}
        include_list = tools_cfg.get("include") or []
        exclude_list = tools_cfg.get("exclude") or []

        # Build checklist labels
        labels = []
        for tool_name, description in tools:
            desc_short = description[:70] + "..." if len(description) > 70 else description
            if desc_short:
                labels.append(f"{tool_name}  ({desc_short})")
            else:
                labels.append(tool_name)

        # Determine which tools are currently enabled
        pre_selected: Set[int] = set()
        tool_names = [t[0] for t in tools]
        for i, tool_name in enumerate(tool_names):
            if include_list:
                # Include mode: only included tools are selected
                if tool_name in include_list:
                    pre_selected.add(i)
            elif exclude_list:
                # Exclude mode: everything except excluded
                if tool_name not in exclude_list:
                    pre_selected.add(i)
            else:
                # No filter: all enabled
                pre_selected.add(i)

        chosen = curses_checklist(
            f"MCP Server: {server_name}  ({len(tools)} tools)",
            labels,
            pre_selected,
            cancel_returns=pre_selected,
        )

        if chosen == pre_selected:
            _print_info(f"  {server_name}: no changes")
            continue

        # Compute new include list (the chosen tools). We standardize on
        # tools.include across the codebase (catalog installs, son-of-anton mcp
        # configure, and this UI) so a server\'s on-disk config shape doesn\'t
        # depend on which UI the user touched last.
        chosen_names = [tool_names[i] for i in sorted(chosen)]

        # Update config
        srv_cfg = mcp_servers.setdefault(server_name, {})
        tools_cfg = srv_cfg.setdefault("tools", {})

        if len(chosen) == len(tools):
            # All tools enabled — clear filters (cleanest config shape; the
            # server\'s native tool set is the active set, and any tools the
            # server adds later are auto-enabled).
            tools_cfg.pop("exclude", None)
            tools_cfg.pop("include", None)
        else:
            tools_cfg["include"] = chosen_names
            # Drop any legacy exclude block — we\'re include-mode now.
            tools_cfg.pop("exclude", None)

        enabled_count = len(chosen)
        disabled_count = len(tools) - enabled_count
        _print_success(
            f"  {server_name}: {enabled_count} enabled, {disabled_count} disabled"
        )
        any_changes = True

    if any_changes:
        save_config(config)
        print()
        print(color("  ✓ MCP tool configuration saved", Colors.GREEN))
    else:
        print(color("  No changes to MCP tools", Colors.DIM))


# ─── Non-interactive disable/enable ──────────────────────────────────────────


def _apply_toolset_change(config: dict, platform: str, toolset_names: List[str], action: str):
    """Add or remove built-in toolsets for a platform."""
    enabled = _get_platform_tools(config, platform, include_default_mcp_servers=False)
    if action == "disable":
        updated = enabled - set(toolset_names)
    else:
        updated = enabled | set(toolset_names)
    _save_platform_tools(config, platform, updated)


def _apply_mcp_change(config: dict, targets: List[str], action: str) -> Set[str]:
    """Add or remove specific MCP tools from a server's exclude list.

    Returns the set of server names that were not found in config.
    """
    failed_servers: Set[str] = set()
    mcp_servers = config.get("mcp_servers") or {}

    for target in targets:
        server_name, tool_name = target.split(":", 1)
        if server_name not in mcp_servers:
            failed_servers.add(server_name)
            continue
        tools_cfg = mcp_servers[server_name].setdefault("tools", {})
        exclude = list(tools_cfg.get("exclude") or [])
        if action == "disable":
            if tool_name not in exclude:
                exclude.append(tool_name)
        else:
            exclude = [t for t in exclude if t != tool_name]
        tools_cfg["exclude"] = exclude

    return failed_servers


def _print_tools_list(enabled_toolsets: set, mcp_servers: dict, platform: str = "cli"):
    """Print a summary of enabled/disabled toolsets and MCP tool filters."""
    effective_all = _get_effective_configurable_toolsets()
    effective = [
        (k, l, d) for (k, l, d) in effective_all
        if _toolset_allowed_for_platform(k, platform)
    ]
    builtin_keys = {ts_key for ts_key, _, _ in CONFIGURABLE_TOOLSETS}

    print(f"Built-in toolsets ({platform}):")
    for ts_key, label, _ in effective:
        if ts_key not in builtin_keys:
            continue
        status = (color("✓ enabled", Colors.GREEN) if ts_key in enabled_toolsets
                  else color("✗ disabled", Colors.RED))
        print(f"  {status}  {ts_key}  {color(label, Colors.DIM)}")

    # Plugin toolsets
    plugin_entries = [(k, l) for k, l, _ in effective if k not in builtin_keys]
    if plugin_entries:
        print()
        print(f"Plugin toolsets ({platform}):")
        for ts_key, label in plugin_entries:
            status = (color("✓ enabled", Colors.GREEN) if ts_key in enabled_toolsets
                      else color("✗ disabled", Colors.RED))
            print(f"  {status}  {ts_key}  {color(label, Colors.DIM)}")

    if mcp_servers:
        print()
        print("MCP servers:")
        for srv_name, srv_cfg in mcp_servers.items():
            tools_cfg = srv_cfg.get("tools") or {}
            exclude = tools_cfg.get("exclude") or []
            include = tools_cfg.get("include") or []
            if include:
                _print_info(f"{srv_name}  [include only: {', '.join(include)}]")
            elif exclude:
                _print_info(f"{srv_name}  [excluded: {color(', '.join(exclude), Colors.YELLOW)}]")
            else:
                _print_info(f"{srv_name}  {color('all tools enabled', Colors.DIM)}")


def _known_tool_platforms() -> set[str]:
    """Return built-in plus discovered plugin platform names.

    Plugin platforms are registered at runtime rather than in the static CLI
    display registry. Tool introspection/configuration must recognize those
    names too, otherwise an active plugin platform cannot audit its authority.
    """
    known = set(PLATFORMS)
    try:
        from son_of_anton_cli.plugins import discover_plugins
        from gateway.platform_registry import platform_registry

        discover_plugins()  # idempotent
        known.update(platform_registry.registered_names())
    except Exception:
        # Plugin discovery is optional. Preserve the built-in CLI path when a
        # third-party plugin is malformed or its dependencies are unavailable.
        pass
    return known


def tools_disable_enable_command(args):
    """Enable, disable, or list tools for a platform.

    Built-in toolsets use plain names (e.g. ``web``, ``memory``).
    MCP tools use ``server:tool`` notation (e.g. ``github:create_issue``).
    """
    action = args.tools_action
    platform = getattr(args, "platform", "cli")
    config = load_config()

    valid_platforms = _known_tool_platforms()
    if platform not in valid_platforms:
        _print_error(f"Unknown platform '{platform}'. Valid: {', '.join(sorted(valid_platforms))}")
        return

    if action == "list":
        _print_tools_list(_get_platform_tools(config, platform, include_default_mcp_servers=False),
                          config.get("mcp_servers") or {}, platform)
        return

    targets: List[str] = args.names
    toolset_targets = [t for t in targets if ":" not in t]
    mcp_targets = [t for t in targets if ":" in t]

    valid_toolsets = {ts_key for ts_key, _, _ in CONFIGURABLE_TOOLSETS} | _get_plugin_toolset_keys()
    unknown_toolsets = [t for t in toolset_targets if t not in valid_toolsets]
    if unknown_toolsets:
        for name in unknown_toolsets:
            _print_error(f"Unknown toolset '{name}'")
        toolset_targets = [t for t in toolset_targets if t in valid_toolsets]

    # Reject platform-scoped toolsets on platforms that don't allow them.
    restricted_targets = [
        t for t in toolset_targets
        if not _toolset_allowed_for_platform(t, platform)
    ]
    if restricted_targets:
        for name in restricted_targets:
            allowed = sorted(_TOOLSET_PLATFORM_RESTRICTIONS.get(name) or set())
            _print_error(
                f"Toolset '{name}' is not available on platform '{platform}' "
                f"(only: {', '.join(allowed)})"
            )
        toolset_targets = [t for t in toolset_targets if t not in restricted_targets]

    if toolset_targets:
        _apply_toolset_change(config, platform, toolset_targets, action)

    failed_servers: Set[str] = set()
    if mcp_targets:
        failed_servers = _apply_mcp_change(config, mcp_targets, action)
        for srv in failed_servers:
            _print_error(f"MCP server '{srv}' not found in config")

    save_config(config)

    successful = [
        t for t in targets
        if t not in unknown_toolsets
        and t not in restricted_targets
        and (":" not in t or t.split(":")[0] not in failed_servers)
    ]
    if successful:
        verb = "Disabled" if action == "disable" else "Enabled"
        _print_success(f"{verb}: {', '.join(successful)}")
