"""
Unified tool configuration for Son of Anton Agent.

`son-of-anton tools` and `son-of-anton setup tools` both enter this module.
Select a platform → toggle toolsets on/off → for newly enabled tools
that need API keys, run through provider-aware configuration.

Saves per-platform tool configuration to ~/.son-of-anton/config.toml under
the `platform_toolsets` key.
"""

import logging
from pathlib import Path
from typing import List, Set


from son_of_anton_cli.config import (
    cfg_get,
    load_config, save_config,
)
from son_of_anton_cli.colors import Colors, color

logger = logging.getLogger(__name__)


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
    ("todo",            "📋 Task Planning",             "todo"),
    ("memory",          "💾 Memory",                    "persistent memory across sessions"),
    ("session_search",  "🔎 Session Search",            "search past conversations"),
    ("clarify",         "❓ Clarifying Questions",      "clarify"),
    ("delegation",      "👥 Task Delegation",           "delegate_task"),
    ("cronjob",         "⏰ Cron Jobs",                 "create/list/update/pause/resume/run"),
]


# Toolsets that are OFF by default for new installs.
# They're still in _SON_OF_ANTON_CORE_TOOLS (available at runtime if enabled),
# but the setup checklist won't pre-select them for first-time users.
_DEFAULT_OFF_TOOLSETS: set = set()




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

# ─── Platform / Toolset Helpers ───────────────────────────────────────────────

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
    """Names of MCP servers globally enabled in config.toml.

    Shared by the gateway/CLI platform resolver (``_get_platform_tools``) and
    the cron per-job toolset resolver (``cron.scheduler``) so every path agrees
    on MCP membership. A server is enabled unless its config sets an explicitly
    falsey ``enabled`` (per ``_parse_enabled_flag``: false/0/no/off) — a missing
    flag or an unrecognized value is treated as enabled.
    """
    mcp_servers = (config or {}).get("mcp_servers") or {}
    names = {
        str(name)
        for name, server_cfg in mcp_servers.items()
        if isinstance(server_cfg, dict)
        and _parse_enabled_flag(server_cfg.get("enabled", True), default=True)
    }
    return names


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

    if toolset_names is None or not isinstance(toolset_names, list):
        plat_info = PLATFORMS.get(platform)
        if plat_info:
            default_ts = plat_info["default_toolset"]
        else:
            # Plugin platform — derive toolset name from platform key
            default_ts = f"son-of-anton-{platform}"
        toolset_names = [default_ts]

    # TOML may parse bare numeric names (e.g. ``12306:``) as int.
    # Normalise to str so downstream sorted() never mixes types.
    toolset_names = [str(ts) for ts in toolset_names]

    configurable_keys = {ts_key for ts_key, _, _ in CONFIGURABLE_TOOLSETS}
    platform_default_keys = {p["default_toolset"] for p in PLATFORMS.values()}
    explicit_known_keys = configurable_keys

    # If the saved list contains any configurable keys directly, the user
    # has explicitly configured this platform — use direct membership.
    # This avoids the subset-inference bug where composite toolsets like
    # "son-of-anton-cli" (which include all _SON_OF_ANTON_CORE_TOOLS) cause disabled
    # toolsets to re-appear as enabled.
    has_explicit_config = any(ts in explicit_known_keys for ts in toolset_names)

    if has_explicit_config:
        enabled_toolsets = {
            ts for ts in toolset_names
            if ts in explicit_known_keys
        }
        # Mixed config: composite toolset alongside configurables. Without
        # expansion the composite name is silently dropped, leaving sessions
        # with only the configurable opt-ins and no native tools. Mirror the
        # else-branch's subset inference, but apply _DEFAULT_OFF_TOOLSETS only
        # to the implicit expansion — anything the user explicitly listed must
        # survive.
        composite_tools = set()
        for ts_name in toolset_names:
            if ts_name in configurable_keys:
                continue
            if ts_name not in TOOLSETS:
                continue
            composite_tools.update(resolve_toolset(ts_name))

        if composite_tools:
            expanded = set()
            for ts_key, _, _ in CONFIGURABLE_TOOLSETS:
                # Compare the toolset's STATIC membership: a tool registered
                # into a toolset (e.g. delegate_cli -> delegation, desktop-only
                # read_terminal -> terminal) that the composite never listed must
                # not drop the whole toolset. See issue #49622.
                ts_tools = set(resolve_toolset(ts_key, include_registry=False))
                if ts_tools and ts_tools.issubset(composite_tools):
                    expanded.add(ts_key)

            default_off = set(_DEFAULT_OFF_TOOLSETS)
            if platform in default_off:
                default_off.remove(platform)
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
        # toolset, keep that toolset enabled on first install.
        if platform in default_off:
            default_off.remove(platform)
        enabled_toolsets -= default_off

    # Recover non-configurable platform toolsets (e.g. relay).  These are
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
    skip = configurable_keys | platform_default_keys
    skip |= {k for k in TOOLSETS if k.startswith("son-of-anton-")}
    skip |= set(_DEFAULT_OFF_TOOLSETS) - {platform}
    for ts_key, ts_def in TOOLSETS.items():
        if ts_key in skip:
            continue
        if ts_def.get("includes"):
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

    # Preserve any explicit non-configurable toolset entries (for example,
    # custom toolsets or MCP server names saved in platform_toolsets).
    explicit_passthrough = {
        ts
        for ts in toolset_names
        if ts not in configurable_keys
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

    # Honor agent.disabled_toolsets from config.toml — allows users to
    # globally suppress specific toolsets (e.g. "memory") across all
    # platforms without per-platform toolset configuration.  This runs
    # last so it overrides everything above.  The value may arrive as a
    # JSON-array string (e.g. "['memory']") from `son-of-anton config set` or a
    # JSON-mode editor save; parse it so the list is not silently dead (#86661).
    agent_cfg = config.get("agent") or {}
    disabled_toolsets = agent_cfg.get("disabled_toolsets") or []
    if disabled_toolsets:
        from utils import parse_config_string_list

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

    configurable_keys = {ts_key for ts_key, _, _ in CONFIGURABLE_TOOLSETS}

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
            from utils import parse_config_string_list

            parsed_disabled = parse_config_string_list(disabled_toolsets)
            newly_enabled = enabled_toolset_keys - preserved_entries
            if newly_enabled:
                remaining = [
                    ts for ts in parsed_disabled if ts not in newly_enabled
                ]
                if remaining != parsed_disabled:
                    agent_cfg["disabled_toolsets"] = remaining

    save_config(config)


# ─── Menu Helpers ─────────────────────────────────────────────────────────────

# ─── Token Estimation ────────────────────────────────────────────────────────

# ─── Provider-Aware Configuration ────────────────────────────────────────────

# Python dependencies installed explicitly through ``son-of-anton tools`` are not
# part of the managed runtime's locked ``all`` sync. A runtime replacement
# therefore needs a small, static allowlist that can be snapshotted before the
# old site-packages disappears and restored afterward. Keep these install
# arguments in sync with the corresponding ``_run_post_setup`` branches.


# post_setup_key -> predicate(): True when the install side-effect is already
# satisfied. Used by ``provider_readiness_status`` to decide whether a keyless
# post_setup row (ddgs, langfuse) is honestly "ready" or still "needs_setup".
# Mirrors the installed-checks ``_run_post_setup`` itself performs before
# installing.


# ─── Main Entry Point ─────────────────────────────────────────────────────────

# ─── MCP Tools Interactive Configuration ─────────────────────────────────────


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
    print(f"Built-in toolsets ({platform}):")
    for ts_key, label, _ in CONFIGURABLE_TOOLSETS:
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


def tools_disable_enable_command(args):
    """Enable, disable, or list tools for a platform.

    Built-in toolsets use plain names (e.g. ``web``, ``memory``).
    MCP tools use ``server:tool`` notation (e.g. ``github:create_issue``).
    """
    action = args.tools_action
    platform = getattr(args, "platform", "cli")
    config = load_config()

    valid_platforms = set(PLATFORMS)
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

    valid_toolsets = {ts_key for ts_key, _, _ in CONFIGURABLE_TOOLSETS}
    unknown_toolsets = [t for t in toolset_targets if t not in valid_toolsets]
    if unknown_toolsets:
        for name in unknown_toolsets:
            _print_error(f"Unknown toolset '{name}'")
        toolset_targets = [t for t in toolset_targets if t in valid_toolsets]

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
        and (":" not in t or t.split(":")[0] not in failed_servers)
    ]
    if successful:
        verb = "Disabled" if action == "disable" else "Enabled"
        _print_success(f"{verb}: {', '.join(successful)}")
