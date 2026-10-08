"""``son-of-anton plugins`` CLI subcommand — install, update, remove, and list plugins.

Plugins are installed from Git repositories into ``~/.son-of-anton/plugins/``.
Supports full URLs and ``owner/repo`` shorthand (resolves to GitHub).

After install, if the plugin ships an ``after-install.md`` file it is
rendered with Rich Markdown.  Otherwise a default confirmation is shown.
"""

from __future__ import annotations

import importlib.metadata
import logging
import sys
from pathlib import Path
from typing import Optional

from son_of_anton_constants import get_son_of_anton_home
from son_of_anton_cli.config import cfg_get

logger = logging.getLogger(__name__)


# Minimum manifest version this installer understands.
# Plugins may declare ``manifest_version: 1`` in plugin.toml;
# future breaking changes to the manifest schema bump this.


def _plugins_dir() -> Path:
    """Return the user plugins directory, creating it if needed."""
    plugins = get_son_of_anton_home() / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    return plugins


def _read_manifest(plugin_dir: Path) -> dict:
    """Read a native or portable manifest, preferring native TOML."""
    manifest_file = plugin_dir / "plugin.toml"
    if not manifest_file.exists():
        manifest_file = plugin_dir / "plugin.toml"
    if not manifest_file.exists():
        portable_file = plugin_dir / "plugin.json"
        if not portable_file.exists() and not portable_file.is_symlink():
            return {}
        try:
            from son_of_anton_cli.agent_plugins import read_agent_plugin_manifest

            manifest, _ = read_agent_plugin_manifest(plugin_dir)
            return manifest
        except Exception as e:
            logger.warning("Failed to read plugin.json in %s: %s", plugin_dir, e)
            return {}
    try:
        import tomllib

        with open(manifest_file, encoding="utf-8") as f:
            return tomllib.loads(f.read()) or {}
    except Exception as e:
        logger.warning("Failed to read plugin.toml in %s: %s", plugin_dir, e)
        return {}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _get_disabled_set() -> set:
    """Read the disabled plugins set from config.toml.

    An explicit deny-list. A plugin name here never loads, even if also
    listed in ``plugins.enabled``.
    """
    try:
        from son_of_anton_cli.config import load_config
        config = load_config()
        disabled = cfg_get(config, "plugins", "disabled", default=[])
        return set(disabled) if isinstance(disabled, list) else set()
    except Exception:
        return set()


def _save_disabled_set(disabled: set) -> None:
    """Write the disabled plugins list to config.toml."""
    from son_of_anton_cli.config import load_config, save_config
    config = load_config()
    if "plugins" not in config:
        config["plugins"] = {}
    config["plugins"]["disabled"] = sorted(disabled)
    save_config(config)


def _get_enabled_set() -> set:
    """Read the enabled plugins allow-list from config.toml.

    Plugins are opt-in: only names here are loaded. Returns ``set()`` if
    the key is missing (same behaviour as "nothing enabled yet").
    """
    try:
        from son_of_anton_cli.config import load_config
        config = load_config()
        plugins_cfg = config.get("plugins", {})
        if not isinstance(plugins_cfg, dict):
            return set()
        enabled = plugins_cfg.get("enabled", [])
        return set(enabled) if isinstance(enabled, list) else set()
    except Exception:
        return set()


def _save_enabled_set(enabled: set) -> None:
    """Write the enabled plugins list to config.toml."""
    from son_of_anton_cli.config import load_config, save_config
    config = load_config()
    if "plugins" not in config:
        config["plugins"] = {}
    config["plugins"]["enabled"] = sorted(enabled)
    save_config(config)


def _resolve_plugin_key(name: str) -> Optional[str]:
    """Resolve a user-supplied plugin identifier to its canonical registry key.

    Accepts either the bare manifest name (``langfuse``), the directory
    name, or the full path-derived key (``observability/langfuse``) and
    returns the canonical key the loader gates on (``manifest.key`` or, for a
    flat plugin, the bare name). Returns ``None`` when no plugin matches.

    This is the single normalization point so ``son-of-anton plugins enable`` /
    ``disable`` write the same key that ``PluginManager`` matches against —
    nested category plugins (e.g. ``observability/langfuse``) included.
    """
    entries = _discover_all_plugins()
    # 1. Exact match on canonical key or manifest name — always unambiguous.
    for entry in entries:
        # entry = (name, version, description, source, dir_path, key)
        if name == entry[5] or name == entry[0]:
            return entry[5]
    # 2. Fall back to a bare leaf-name match (e.g. "langfuse" ->
    #    "observability/langfuse"), but only when it resolves to exactly one
    #    plugin so we never silently pick the wrong same-named nested plugin.
    leaf_matches = [entry[5] for entry in entries if name == entry[5].split("/")[-1]]
    if len(leaf_matches) == 1:
        return leaf_matches[0]
    return None


def _resolve_plugin_key_and_source(name: str) -> Optional[tuple]:
    """Resolve *name* to ``(canonical_key, source)`` or ``None`` if no match.

    Mirrors :func:`_resolve_plugin_key`'s normalization but also returns the
    plugin's source (``"bundled"``, ``"user"``, ``"project"``, ...) so the
    enable path can tell whether a built-in-override consent prompt is needed.
    """
    entries = _discover_all_plugins()
    for entry in entries:
        # entry = (name, version, description, source, dir_path, key)
        if name == entry[5] or name == entry[0]:
            return (entry[5], entry[3])
    leaf_matches = [
        (entry[5], entry[3]) for entry in entries
        if name == entry[5].split("/")[-1]
    ]
    if len(leaf_matches) == 1:
        return leaf_matches[0]
    return None


def _set_plugin_entry_flag(plugin_id: str, key: str, value: bool) -> None:
    """Write ``plugins.entries.<plugin_id>.<key> = value`` into config.toml."""
    from son_of_anton_cli.config import load_config, save_config
    config = load_config()
    plugins_cfg = config.setdefault("plugins", {})
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
        config["plugins"] = plugins_cfg
    entries = plugins_cfg.setdefault("entries", {})
    if not isinstance(entries, dict):
        entries = {}
        plugins_cfg["entries"] = entries
    entry = entries.setdefault(plugin_id, {})
    if not isinstance(entry, dict):
        entry = {}
        entries[plugin_id] = entry
    entry[key] = bool(value)
    save_config(config)


def cmd_enable(name: str, allow_tool_override: Optional[bool] = None) -> None:
    """Add a plugin to the enabled allow-list (and remove it from disabled).

    For non-bundled plugins, prompt the operator about granting the
    privileged ``allow_tool_override`` capability (replacing built-in tools
    like ``shell_exec`` / ``write_file``). ``allow_tool_override`` is a
    tri-state: ``True`` grants without prompting, ``False`` declines without
    prompting, ``None`` (default) asks interactively. Bundled plugins are
    trusted and never prompted.
    """
    from rich.console import Console
    from son_of_anton_cli.relay_plugin_cutover import (
        LEGACY_RELAY_PLUGIN_KEYS,
        RELAY_PLUGINS_CONFIG_ENV,
    )

    console = Console()
    if name in LEGACY_RELAY_PLUGIN_KEYS:
        console.print(
            f"[red]Plugin '{name}' was removed.[/red] Relay lifecycle is owned "
            f"by Son of Anton core; configure {RELAY_PLUGINS_CONFIG_ENV} instead."
        )
        sys.exit(1)

    # Discover the plugin — check installed (user) AND bundled, including
    # nested category plugins — and normalize to its canonical registry key.
    resolved = _resolve_plugin_key_and_source(name)
    if resolved is None:
        console.print(f"[red]Plugin '{name}' is not installed or bundled.[/red]")
        sys.exit(1)
    key, source = resolved

    if key in LEGACY_RELAY_PLUGIN_KEYS:
        console.print(
            f"[red]Plugin '{key}' was removed.[/red] Relay lifecycle is owned "
            f"by Son of Anton core; configure {RELAY_PLUGINS_CONFIG_ENV} instead."
        )
        sys.exit(1)

    enabled = _get_enabled_set()
    disabled = _get_disabled_set()

    already_enabled = key in enabled and key not in disabled

    if not already_enabled:
        enabled.add(key)
        disabled.discard(key)
        # Drop every alias of this plugin from the disabled list so an
        # explicit disable under a different form can't keep it off. The
        # loader's disable check matches on BOTH the canonical key
        # (``web/firecrawl``) AND the manifest name (``web-firecrawl``);
        # a stale entry under either form makes "explicit disable wins"
        # (plugins.py) silently veto this enable. Discard the key, its
        # bare leaf, and the manifest name. (#40190 follow-up.)
        bare = key.split("/")[-1]
        if bare != key:
            disabled.discard(bare)
        for entry in _discover_all_plugins():
            # entry = (name, version, description, source, dir_path, key)
            if entry[5] == key:
                disabled.discard(entry[0])
                break
        _save_enabled_set(enabled)
        _save_disabled_set(disabled)
        console.print(
            f"[green]✓[/green] Plugin [bold]{key}[/bold] enabled. "
            "Takes effect on next session."
        )
    else:
        console.print(f"[dim]Plugin '{key}' is already enabled.[/dim]")

    # Built-in tool override is a privileged grant. Bundled plugins ship with
    # Son of Anton core and are trusted; every other source needs operator opt-in.
    if source == "bundled":
        return

    # Capability consent (#64228): when the manifest declares capabilities,
    # the consent screen is the canonical grant path — it covers
    # tools.override too, so skip the legacy standalone prompt unless the
    # operator explicitly passed --allow-tool-override/--no-allow-tool-override.
    declared_caps = _declared_capabilities_for_key(key)
    if declared_caps:
        _run_capability_consent(console, key, declared_caps, context="enable")
        if allow_tool_override is not None:
            _resolve_tool_override_grant(console, key, allow_tool_override)
        return

    _resolve_tool_override_grant(console, key, allow_tool_override)


# ── Capability consent flow (#64228) ─────────────────────────────────────────


def _declared_capabilities_from_manifest(manifest: dict, plugin_name: str = "?") -> list:
    """Extract + normalize the ``capabilities:`` declaration from a manifest."""
    from son_of_anton_cli.plugin_capabilities import parse_declared_capabilities

    return parse_declared_capabilities(
        (manifest or {}).get("capabilities"), plugin_name
    )


def _declared_capabilities_for_key(key: str) -> list:
    """Read the declared capabilities for an installed/bundled plugin by key."""
    for entry in _discover_all_plugins():
        # entry = (name, version, description, source, dir_path, key)
        if entry[5] == key or entry[0] == key:
            if entry[3] == "entrypoint":
                from son_of_anton_cli.plugins import discover_entrypoint_manifests

                for manifest in discover_entrypoint_manifests():
                    if key in (manifest.key, manifest.name):
                        return list(manifest.capabilities)
                return []
            dir_path = entry[4]
            if not dir_path:
                return []
            manifest = _read_manifest(Path(dir_path))
            return _declared_capabilities_from_manifest(manifest, entry[0])
    return []


def _print_capability_list(console, capabilities: list) -> None:
    """Render the consent screen body: one line per capability."""
    from son_of_anton_cli.plugin_capabilities import CAPABILITY_REGISTRY

    for cap in capabilities:
        spec = CAPABILITY_REGISTRY.get(cap)
        desc = spec.description if spec else ""
        console.print(f"    [bold]{cap}[/bold] — {desc}")


def _run_capability_consent(
    console,
    plugin_id: str,
    declared: list,
    *,
    context: str = "install",
) -> bool:
    """Show the capability consent screen and record the decision.

    Prints the declared capability list with one-line risk descriptions and
    asks a single Y/n. On consent, the *pending* capabilities are granted
    (recorded under ``plugins.entries.<plugin_id>.granted_capabilities`` with
    a consent hash of the declared set). On decline — or in ANY
    non-interactive context — capabilities stay ungranted (fail closed) and
    the plugin must degrade gracefully via ``ctx.has_capability()``.

    The consent wording deliberately does not imply a code audit: granting a
    capability trusts the plugin author. This is consent + audit, NOT a
    sandbox — an in-process plugin can run arbitrary Python regardless.

    Returns True when consent was granted.
    """
    from son_of_anton_cli.plugin_capabilities import (
        pending_capabilities,
        record_consent,
    )

    pending = pending_capabilities(plugin_id, declared)
    if not pending:
        # Everything declared is already granted — refresh the consent hash
        # so a later declaration change is detected against the current set.
        if declared:
            record_consent(plugin_id, [], declared)
        return True

    verb = "requests" if context == "install" else "now requests"
    console.print(
        f"\n  [yellow]Plugin [bold]{plugin_id}[/bold] {verb} the following "
        "capabilities:[/yellow]"
    )
    _print_capability_list(console, pending)
    console.print(
        "  [dim]Granting trusts the plugin author with these host surfaces. "
        "This is consent, not a sandbox — plugins run as regular Python "
        "in-process.[/dim]"
    )

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print(
            "  [yellow]Non-interactive session: capabilities NOT granted "
            "(fail closed).[/yellow] Run "
            f"`son-of-anton plugins capabilities {plugin_id}` to review and "
            f"`son-of-anton plugins enable {plugin_id}` to grant interactively."
        )
        return False

    try:
        answer = console.input("  Grant these capabilities? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in {"y", "yes"}:
        record_consent(plugin_id, pending, declared)
        console.print(
            f"  [green]✓[/green] Granted: {', '.join(pending)} "
            f"([dim]plugins.entries.{plugin_id}.granted_capabilities[/dim])"
        )
        return True

    console.print(
        f"  [dim]Declined. {plugin_id} stays enabled with these capabilities "
        "off; it should degrade gracefully (ctx.has_capability()). Re-run "
        f"`son-of-anton plugins enable {plugin_id}` to grant later.[/dim]"
    )
    return False


def _resolve_tool_override_grant(
    console, key: str, allow_tool_override: Optional[bool]
) -> None:
    """Resolve and persist the ``allow_tool_override`` grant for a plugin.

    ``allow_tool_override`` tri-state: True grants, False declines, None
    prompts interactively (defaulting to deny on a non-interactive stdin).
    """
    if allow_tool_override is None:
        # Interactive consent. Default to NO so a blind Enter doesn't grant
        # a privileged capability, and a non-interactive stdin denies safely.
        prompt = (
            "[yellow]Allow this plugin to replace built-in tools "
            "(e.g. shell_exec, write_file)?[/yellow]\n"
            "  This is a privileged capability: an override can intercept "
            "everything the agent routes through that tool.\n"
            "  Grant it? [y/N] "
        )
        try:
            answer = console.input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        allow_tool_override = answer in {"y", "yes"}

    plugin_id = key
    _set_plugin_entry_flag(plugin_id, "allow_tool_override", allow_tool_override)
    if allow_tool_override:
        console.print(
            f"[green]✓[/green] Granted [bold]{key}[/bold] permission to "
            "override built-in tools "
            f"([dim]plugins.entries.{plugin_id}.allow_tool_override: true[/dim])."
        )
    else:
        console.print(
            f"[dim]{key} may not override built-in tools. Re-run "
            f"`son-of-anton plugins enable {key} --allow-tool-override` to grant "
            "this later.[/dim]"
        )


def cmd_disable(name: str) -> None:
    """Remove a plugin from the enabled allow-list (and add to disabled)."""
    from rich.console import Console

    console = Console()
    key = _resolve_plugin_key(name)
    if key is None:
        console.print(f"[red]Plugin '{name}' is not installed or bundled.[/red]")
        sys.exit(1)

    enabled = _get_enabled_set()
    disabled = _get_disabled_set()

    if key not in enabled and key in disabled:
        console.print(f"[dim]Plugin '{key}' is already disabled.[/dim]")
        return

    enabled.discard(key)
    # Drop any legacy bare-name entry from the allow-list too, so a stale
    # bare name can't keep a nested plugin loading after an explicit disable.
    bare = key.split("/")[-1]
    if bare != key:
        enabled.discard(bare)
    disabled.add(key)
    _save_enabled_set(enabled)
    _save_disabled_set(disabled)
    console.print(
        f"[yellow]\u2298[/yellow] Plugin [bold]{key}[/bold] disabled. "
        "Takes effect on next session."
    )


def _read_manifest_info(d: Path, prefix: str):
    """Read a native or portable manifest and return display metadata.

    Returns None if no manifest file exists.
    """
    manifest_file = d / "plugin.toml"
    if not manifest_file.exists():
        manifest_file = d / "plugin.toml"
    if not manifest_file.exists():
        portable_file = d / "plugin.json"
        if not portable_file.exists() and not portable_file.is_symlink():
            return None
        try:
            from son_of_anton_cli.agent_plugins import read_agent_plugin_manifest

            manifest, _ = read_agent_plugin_manifest(d)
            name = manifest["name"]
            key = f"{prefix}/{d.name}" if prefix else name
            return (
                name,
                manifest.get("version", ""),
                manifest.get("description", ""),
                key,
            )
        except Exception:
            return None
    name = d.name
    version = ""
    description = ""
    try:
        import tomllib

        with open(manifest_file, encoding="utf-8") as f:
            manifest = tomllib.loads(f.read()) or {}
        name = manifest.get("name", d.name)
        version = manifest.get("version", "")
        description = manifest.get("description", "")
    except Exception:
        pass
    key = f"{prefix}/{d.name}" if prefix else name
    return name, version, description, key


# Manifest kinds that are active-by-default when bundled: backends auto-load,
# platforms register lazily but are available out of the box, model providers
# run through providers/ discovery (see PluginManager.discover_and_load).


def _scan_level(
    base: Path,
    source: str,
    skip_names: set,
    prefix: str,
    depth: int,
    seen: dict,
) -> None:
    """Recursive directory scan matching PluginManager._scan_directory_level.

    Populates *seen* with key -> (name, version, description, source, dir, key).
    """
    if not base.is_dir():
        return
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        if depth == 0 and skip_names and d.name in skip_names:
            continue
        info = _read_manifest_info(d, prefix)
        if info is not None:
            name, version, description, key = info
            if key in seen and source == "bundled":
                continue
            src_label = source
            if source == "user" and (d / ".git").exists():
                src_label = "git"
            seen[key] = (name, version, description, src_label, d, key)
            continue
        if depth >= 1:
            continue
        sub_prefix = f"{prefix}/{d.name}" if prefix else d.name
        _scan_level(d, source, set(), sub_prefix, depth + 1, seen)


def _discover_all_plugins() -> list:
    """Return a list of (name, version, description, source, dir_path, key) for
    every plugin the loader can see — user + bundled + project + entry point.

    Matches the ordering/dedup of ``PluginManager.discover_and_load``:
    bundled first, then user, then project, then entry points. Later sources
    override earlier ones on key collision.
    """
    seen: dict = {}  # key -> (name, version, description, source, path, key)

    # Bundled (<repo>/plugins/<name>/), excluding memory/, context_engine/
    # and model-providers/ — model providers load through the dedicated
    # provider registry (providers/__init__.py), not the general PluginManager
    # opt-in surface, so listing them as toggleable plugins is misleading.
    from son_of_anton_cli.plugins import get_bundled_plugins_dir
    repo_plugins = get_bundled_plugins_dir()
    for base, source, skip in (
        (repo_plugins, "bundled", {"memory", "context_engine", "model-providers"}),
        (_plugins_dir(), "user", set()),
    ):
        _scan_level(base, source, skip, "", 0, seen)

    # Entry-point plugins (installed as Python packages; no plugin directory).
    for name, version, description, path in _discover_entrypoint_plugins():
        seen[name] = (name, version, description, "entrypoint", path, name)
    return list(seen.values())


def _discover_entrypoint_plugins() -> list[tuple[str, str, str, str]]:
    """Return plugin entries advertised through ``son_of_anton_agent.plugins``.

    Entry-point plugins are installed as Python packages, so they do not have a
    plugin directory under ``~/.son-of-anton/plugins``. Include package metadata here
    so ``son-of-anton plugins list`` can show and enable them.
    """
    from son_of_anton_cli.plugins import ENTRY_POINTS_GROUP

    try:
        eps = importlib.metadata.entry_points()
        if hasattr(eps, "select"):
            group_eps = eps.select(group=ENTRY_POINTS_GROUP)
        elif isinstance(eps, dict):
            group_eps = eps.get(ENTRY_POINTS_GROUP, [])
        else:
            group_eps = [ep for ep in eps if ep.group == ENTRY_POINTS_GROUP]
    except Exception as exc:
        logger.debug("Entry-point plugin discovery failed: %s", exc)
        return []

    entries: list[tuple[str, str, str, str]] = []
    for ep in group_eps:
        version = ""
        description = ""
        dist = getattr(ep, "dist", None)
        metadata = getattr(dist, "metadata", None)
        if metadata is not None:
            version = str(getattr(dist, "version", "") or "")
            description = str(metadata.get("Summary", "") or "")
        entries.append((ep.name, version, description, ep.value))
    return entries


def _plugin_status(name: str, enabled: set, disabled: set, key: str = "") -> str:
    """Return the user-facing activation state for a plugin name or key."""
    if name in disabled or key in disabled:
        return "disabled"
    if name in enabled or key in enabled:
        return "enabled"
    return "not enabled"


# ---------------------------------------------------------------------------
# Provider plugin discovery helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Composite plugins UI
# ---------------------------------------------------------------------------


