"""Slash command definitions and autocomplete for the Son of Anton CLI.

Central registry for all slash commands. Every consumer -- CLI help, gateway
dispatch, autocomplete -- derives its data from ``COMMAND_REGISTRY``.

To add a command: add a ``CommandDef`` entry to ``COMMAND_REGISTRY``.
To add an alias: set ``aliases=("short",)`` on the existing ``CommandDef``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from utils import is_truthy_value

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CommandDef dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandDef:
    """Definition of a single slash command."""

    name: str                          # canonical name without slash: "background"
    description: str                   # human-readable description
    category: str                      # "Session", "Configuration", etc.
    aliases: tuple[str, ...] = ()      # alternative names: ("bg",)
    args_hint: str = ""                # argument placeholder: "<prompt>", "[name]"
    subcommands: tuple[str, ...] = ()  # tab-completable subcommands
    cli_only: bool = False             # only available in CLI
    gateway_only: bool = False         # only available in gateway/messaging
    gateway_config_gate: str | None = None  # config dotpath; when truthy, overrides cli_only for gateway
    # Mid-run (agent busy) gateway behavior.  Drives the Guard-2 dispatcher
    # in gateway/run.py (_dispatch_busy_slash_command) instead of a
    # hand-written per-command if-chain.  Values:
    #   "dispatch"                — run the command while the agent is busy
    #                               (via its normal handler, or the mid-run
    #                               variant named by ``busy_handler``).
    #   "reject"                  — refuse mid-run.  Without ``busy_handler``
    #                               the generic "Agent is running — `/<cmd>`
    #                               can't run mid-turn" catch-all is returned;
    #                               with ``busy_handler`` a command-specific
    #                               reject message is used.
    #   "interrupt_then_dispatch" — interrupt/kill the running agent first,
    #                               then dispatch (the /stop, /new, /reset
    #                               class).  Guard 1 (platforms/base.py)
    #                               routes these through the cancel-handoff
    #                               path via is_interrupt_then_dispatch().
    busy_policy: str = "reject"
    # Optional key of a special mid-run handler in the Guard-2 handler table
    # (gateway/run.py) for commands whose busy behavior differs from their
    # normal handler (e.g. /goal's control-verb whitelist, /sendnow's steer
    # of the live turn, /model's custom busy-reject text).
    busy_handler: str | None = None
    # Registry-owned shared execution (thin slice, informational commands).
    # Names a key in ``son_of_anton_cli.slash_exec.EXECUTORS`` — a pure formatter
    # producing the canonical, surface-independent core text.  Surfaces
    # resolve it via ``son_of_anton_cli.slash_exec.run_execute`` and apply only
    # their own decoration (Rich markup, emoji/markdown).  A
    # string key (not a callable) keeps this module import-light: the
    # gateway can import commands.py without prompt_toolkit and without
    # pulling in executor dependencies.
    execute: str | None = None


# Valid values for CommandDef.busy_policy (see field docs above).


# ---------------------------------------------------------------------------
# Central registry -- single source of truth
# ---------------------------------------------------------------------------

COMMAND_REGISTRY: list[CommandDef] = [
    # Session
    CommandDef("start", "Acknowledge platform start pings without a reply", "Session",
               gateway_only=True, busy_policy="dispatch", busy_handler="start"),
    CommandDef("new", "Start a new session (fresh session ID + history)", "Session",
               aliases=("reset",), args_hint="[name]",
               busy_policy="interrupt_then_dispatch", busy_handler="new"),
    CommandDef("clear", "Clear screen and start a new session", "Session",
               cli_only=True),
    CommandDef("redraw", "Force a full UI repaint (recovers from terminal drift)", "Session",
               cli_only=True),
    CommandDef("history", "Show conversation history", "Session",
               cli_only=True),
    CommandDef("save", "Export the current conversation (bare /save shows usage)", "Session",
               args_hint="<json|md|html> [filename] [redact]"),
    CommandDef("retry", "Retry the last message (resend to agent)", "Session"),
    CommandDef("commit", "Review uncommitted changes, write a message in the repo's style, and commit", "Session",
               cli_only=True, args_hint="[extra instructions]"),
    CommandDef("plan", "Write a plan to .son-of-anton/plans/ instead of doing the work", "Session",
               args_hint="[task]"),
    CommandDef("undo", "Back up N user turns and re-prompt (default 1)", "Session",
               args_hint="[N]"),
    CommandDef("title", "Set a title for the current session", "Session",
               args_hint="[name]"),
    CommandDef("worktree", "Show, list, create, or prune isolated git worktrees", "Session",
               cli_only=True, args_hint="[new [name]|list|prune [--dry-run]]",
               subcommands=("new", "list", "prune")),
    CommandDef("compact", "Compact conversation context (add 'here [N]' to keep recent N turns; --preview shows what would happen)", "Session",
               args_hint="[here [N] | focus topic | --preview|--dry-run]"),
    CommandDef("stop", "Kill all running background processes", "Session",
               busy_policy="interrupt_then_dispatch", busy_handler="stop"),
    CommandDef("pause", "Pause new work globally (emergency stop); '/pause off' resumes", "Session",
               gateway_only=True, args_hint="[reason | off]",
               busy_policy="dispatch"),
    CommandDef("approve", "Approve a pending dangerous command", "Session",
               gateway_only=True, args_hint="[session|always]", busy_policy="dispatch"),
    CommandDef("deny", "Deny a pending dangerous command (optionally with a reason)", "Session",
               gateway_only=True, args_hint="[all] [reason]", busy_policy="dispatch"),
    CommandDef("background", "Run a prompt in the background", "Session",
               aliases=("bg", "btw"), args_hint="<prompt>", busy_policy="dispatch"),
    CommandDef("agents", "Show active agents and running tasks", "Session",
               aliases=("tasks",), busy_policy="dispatch"),
    CommandDef("sendnow", "Steer the running turn onto the oldest queued message, or onto the text after it", "Session",
               args_hint="[text]",
               busy_policy="dispatch", busy_handler="sendnow"),
    CommandDef("sendall", "Steer the running turn onto every queued message at once", "Session",
               busy_policy="dispatch", busy_handler="sendall"),
    CommandDef("cancel", "Pick queued messages to drop before they are sent", "Session",
               cli_only=True),
    CommandDef("handoff", "Leaving? Your messaging chat relays this session while the TUI keeps running it", "Session",
               cli_only=True),
    CommandDef("pickup", "Back at the terminal: end /handoff, the chat stops relaying this session", "Session",
               cli_only=True),
    CommandDef("goal", "Set a standing goal Son of Anton works on across turns until achieved", "Session",
               args_hint="[text | draft <text> | show | gate add <cmd> | pause | resume | clear | status | wait <pid> | unwait]",
               busy_policy="dispatch", busy_handler="goal"),
    CommandDef("status", "Show session, model, token, and context info", "Session",
               busy_policy="dispatch"),
    CommandDef("context", "Show detailed context window view with usage gauge, category breakdown, compaction stats, and throughput", "Session",
               aliases=("ctx",), args_hint="[all]", subcommands=("all",),
               busy_policy="dispatch"),
    CommandDef("sethome", "Set this chat as the home channel", "Session",
               gateway_only=True, aliases=("set-home",)),
    CommandDef("resume", "Resume a previously-named session", "Session",
               args_hint="[name]"),

    # Configuration
    CommandDef("sessions", "Browse and resume previous sessions", "Session"),

    # Configuration
    CommandDef("config", "Show current configuration", "Configuration",
               cli_only=True),
    CommandDef("model", "Switch model (session-scoped; --global to persist)", "Configuration",
               args_hint="[model] [--provider name] [--global|--session] [--refresh]",
               busy_policy="reject", busy_handler="model"),
    CommandDef("perm", "Set the permission mode: default, ask, lockdown, or yolo", "Configuration",
               args_hint="[default|ask|lockdown|yolo]", busy_policy="reject", busy_handler="perm"),

    CommandDef("timestamps", "Toggle [HH:MM] timestamps on messages and /history", "Configuration",
               cli_only=True, args_hint="[on|off|status]",
               subcommands=("on", "off", "status"), aliases=("ts",)),
    CommandDef("diff", "Show git changes in the working directory", "Info",
               args_hint="[staged|all] [--stat] [path...]",
               subcommands=("staged", "all")),
    CommandDef("verbose", "Cycle tool progress display: off -> new -> all -> verbose",
               "Configuration", cli_only=True,
               gateway_config_gate="display.tool_progress_command",
               busy_policy="dispatch"),
    CommandDef("focus", "Toggle focus view — show only your prompt and the final response",
               "Configuration", cli_only=True, args_hint="[on|off|status]",
               subcommands=("on", "off", "status")),
    CommandDef("yolo", "Toggle YOLO mode (skip all dangerous command approvals)",
               "Configuration", busy_policy="dispatch"),
    CommandDef("approvals", "Show or set the persistent dangerous-command approval mode",
               "Configuration", args_hint="[manual|smart|off]",
               subcommands=("manual", "smart", "off")),
    CommandDef("reasoning", "Manage reasoning effort and display", "Configuration",
               args_hint="[level|show|hide|full|clamp] [--global]",
               subcommands=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "show", "hide", "on", "off", "full", "clamp", "--global")),
    CommandDef("skin", "Show or change the display skin/theme", "Configuration",
               cli_only=True, args_hint="[name]"),

    # Tools
    CommandDef("tools", "Manage tools: /tools [list|disable|enable] [name...]", "Tools",
               args_hint="[list|disable|enable] [name...]", cli_only=True),
    CommandDef("toolsets", "List available toolsets", "Tools",
               cli_only=True),
    CommandDef("memory", "Review pending memory writes / toggle the approval gate",
               "Tools",
               args_hint="[pending|approve|reject|approval] [id|on|off]",
               subcommands=("pending", "approve", "reject", "approval")),
    CommandDef("init", "Generate or update AGENTS.md project instructions from a repo scan",
               "Tools", args_hint="[notes]"),
    CommandDef("cron", "Manage scheduled tasks", "Tools",
               cli_only=True, args_hint="[subcommand]",
               subcommands=("list", "add", "create", "edit", "pause", "resume", "run", "remove")),
    CommandDef("reload", "Reload .env variables into the running session", "Tools",
               cli_only=True),
    CommandDef("reload-mcp", "Reload MCP servers from config", "Tools",
               aliases=("reload_mcp",)),
    CommandDef("plugins", "List installed plugins and their status",
               "Tools", cli_only=True),

    # Info
    CommandDef("commands", "Browse all commands (paginated)", "Info",
               gateway_only=True, args_hint="[page]", busy_policy="dispatch",
               execute="gateway_commands"),
    CommandDef("help", "Show available commands (/help <text> filters)", "Info", busy_policy="dispatch",
               execute="gateway_help", args_hint="[<filter>]"),
    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch"),
    CommandDef("usage", "Show token usage and rate limits for this session", "Info"),
    CommandDef("insights", "Show usage insights and analytics", "Info",
               args_hint="[days]"),
    CommandDef("platforms", "Show gateway/messaging platform status", "Info",
               cli_only=True, aliases=("gateway",)),
    CommandDef("platform", "Pause, resume, or list a failing gateway platform", "Info",
               gateway_only=True, args_hint="<pause|resume|list> [name]"),
    CommandDef("copy", "Copy the last assistant response to clipboard", "Info",
               cli_only=True, args_hint="[number]"),
    CommandDef("paste", "Attach clipboard image from your clipboard", "Info",
               cli_only=True),
    CommandDef("image", "Attach a local image file for your next prompt", "Info",
               cli_only=True, args_hint="<path>"),
    CommandDef("version", "Show Son of Anton Agent version", "Info", aliases=("v",),
               busy_policy="dispatch", execute="version"),

    # Exit
    CommandDef("quit", "Exit the CLI (use --delete to also remove session history)", "Exit",
               cli_only=True, aliases=("exit", "q"), args_hint="[--delete]"),
]


# ---------------------------------------------------------------------------
# Derived lookups -- rebuilt once at import time, refreshed by rebuild_lookups()
# ---------------------------------------------------------------------------

def _build_command_lookup() -> dict[str, CommandDef]:
    """Map every name and alias to its CommandDef."""
    lookup: dict[str, CommandDef] = {}
    for cmd in COMMAND_REGISTRY:
        lookup[cmd.name] = cmd
        for alias in cmd.aliases:
            lookup[alias] = cmd
    return lookup


_COMMAND_LOOKUP: dict[str, CommandDef] = _build_command_lookup()


def resolve_command(name: str) -> CommandDef | None:
    """Resolve a command name or alias to its CommandDef.

    Accepts names with or without the leading slash (``/help`` or ``help``),
    and the vi-style colon prefix (``:q``, ``:help``).
    """
    return _COMMAND_LOOKUP.get(name.lower().lstrip("/:"))


def _build_description(cmd: CommandDef) -> str:
    """Build a CLI-facing description string including usage hint."""
    if cmd.args_hint:
        return f"{cmd.description} (usage: /{cmd.name} {cmd.args_hint})"
    return cmd.description


# Backwards-compatible flat dict: "/command" -> description
COMMANDS: dict[str, str] = {}
for _cmd in COMMAND_REGISTRY:
    if not _cmd.gateway_only:
        COMMANDS[f"/{_cmd.name}"] = _build_description(_cmd)
        for _alias in _cmd.aliases:
            COMMANDS[f"/{_alias}"] = f"{_cmd.description} (alias for /{_cmd.name})"

# Backwards-compatible categorized dict
COMMANDS_BY_CATEGORY: dict[str, dict[str, str]] = {}
for _cmd in COMMAND_REGISTRY:
    if not _cmd.gateway_only:
        _cat = COMMANDS_BY_CATEGORY.setdefault(_cmd.category, {})
        _cat[f"/{_cmd.name}"] = COMMANDS[f"/{_cmd.name}"]
        for _alias in _cmd.aliases:
            _cat[f"/{_alias}"] = COMMANDS[f"/{_alias}"]


# Subcommands lookup: "/cmd" -> ["sub1", "sub2", ...]
SUBCOMMANDS: dict[str, list[str]] = {}
for _cmd in COMMAND_REGISTRY:
    if _cmd.subcommands:
        SUBCOMMANDS[f"/{_cmd.name}"] = list(_cmd.subcommands)


# Help renderer sub-grouping: the "Session" category accumulated ~46 commands
# spanning genuinely different concerns (lifecycle, context, background/async).
# Rather than re-tag every CommandDef (category is load-bearing for gateway
# help + other surfaces), the /help renderer splits Session into readable
# sub-headers using these command-name sets. Any Session command not listed
# here falls under the base "Session" header. Names are bare (no leading /).
HELP_SESSION_SUBGROUPS: dict[str, tuple[str, ...]] = {
    "Context": (
        "compact", "context", "ctx", "status",
    ),
    "Background & Automation": (
        "background", "bg", "btw", "agents", "tasks", "sendnow", "sendall", "cancel",
        "goal", "subgoal", "heartbeat", "hb", "loop", "proactive",
        "journey", "learning", "memory-graph",
    ),
}

# Also extract subcommands hinted in args_hint via pipe-separated patterns
# e.g. args_hint="[on|off|tts|status]" for commands that don't have explicit subcommands.
# NOTE: If a command already has explicit subcommands, this fallback is skipped.
# Use the `subcommands` field on CommandDef for intentional tab-completable args.
_PIPE_SUBS_RE = re.compile(r"[a-z]+(?:\|[a-z]+)+")
for _cmd in COMMAND_REGISTRY:
    key = f"/{_cmd.name}"
    if key in SUBCOMMANDS or not _cmd.args_hint:
        continue
    m = _PIPE_SUBS_RE.search(_cmd.args_hint)
    if m:
        SUBCOMMANDS[key] = m.group(0).split("|")


# ---------------------------------------------------------------------------
# Gateway helpers
# ---------------------------------------------------------------------------

# Set of all command names + aliases recognized by the gateway.
# Includes config-gated commands so the gateway can dispatch them
# (the handler checks the config gate at runtime).
GATEWAY_KNOWN_COMMANDS: frozenset[str] = frozenset(
    name
    for cmd in COMMAND_REGISTRY
    if not cmd.cli_only or cmd.gateway_config_gate
    for name in (cmd.name, *cmd.aliases)
)


def is_gateway_known_command(name: str | None) -> bool:
    """Return True if ``name`` resolves to a gateway-dispatchable slash command.

    This covers both built-in commands (``GATEWAY_KNOWN_COMMANDS`` derived
    from ``COMMAND_REGISTRY``) and plugin-registered commands, which are
    looked up lazily so importing this module never forces plugin
    discovery. Gateway code uses this to decide whether to emit
    ``command:<name>`` hooks — plugin commands get the same lifecycle
    events as built-ins.
    """
    if not name:
        return False
    if name in GATEWAY_KNOWN_COMMANDS:
        return True
    for plugin_name, _description, _args_hint in _iter_plugin_command_entries():
        if plugin_name == name:
            return True
    return False


# Commands with explicit mid-run (running-agent) behavior in gateway/run.py.
# DERIVED from the registry: every command whose ``busy_policy`` is not
# "reject" either dispatches while the agent is busy or interrupts it first.
# Kept under its historical public name for introspection / tests;
# semantically a subset of "all resolvable commands" — which is the real
# bypass set (see should_bypass_active_session below).


def is_interrupt_then_dispatch(command_name: str | None) -> bool:
    """Return True when *command_name* must interrupt a running agent first.

    Derived from the registry: commands whose ``busy_policy`` is
    "interrupt_then_dispatch" (the /stop, /new, /reset class).  Guard 1
    (gateway/platforms/base.py) routes these through the cancel-handoff
    path that serializes cancellation + runner response + pending drain.
    Accepts aliases (e.g. "reset" resolves to "new").
    """
    if not command_name:
        return False
    cmd = resolve_command(command_name)
    return cmd is not None and cmd.busy_policy == "interrupt_then_dispatch"


def should_bypass_active_session(command_name: str | None) -> bool:
    """Return True for any resolvable slash command.

    Rationale: every gateway-registered slash command either has a
    specific Level-2 handler in gateway/run.py (/stop, /new, /model,
    /approve, etc.) or reaches the running-agent catch-all that returns
    a "busy — wait or /stop first" response. In both paths the command
    is dispatched, not queued.

    Queueing is always wrong for a recognized slash command because the
    safety net in gateway.run discards any command text that reaches
    the pending queue — which meant a mid-run /model (or /reasoning,
    /voice, /insights, /title, /resume, /retry, /undo, /compact,
    /usage, /reload-mcp, /sethome, /reset) would silently
    interrupt the agent AND get discarded, producing a zero-char
    response. See issue #5057 / PRs #6252, #10370, #4665.

    ACTIVE_SESSION_BYPASS_COMMANDS remains the subset of commands with
    explicit Level-2 handlers; the rest fall through to the catch-all.
    """
    return resolve_command(command_name) is not None if command_name else False


def _resolve_config_gates() -> set[str]:
    """Return canonical names of commands whose ``gateway_config_gate`` is truthy.

    Reads ``config.toml`` and walks the dot-separated key path for each
    config-gated command.  Returns an empty set on any error so callers
    degrade gracefully.
    """
    gated = [c for c in COMMAND_REGISTRY if c.gateway_config_gate]
    if not gated:
        return set()
    try:
        from son_of_anton_cli.config import read_raw_config
        cfg = read_raw_config()
    except Exception:
        return set()
    result: set[str] = set()
    for cmd in gated:
        val: Any = cfg
        for key in cmd.gateway_config_gate.split("."):
            if isinstance(val, dict):
                val = val.get(key)
            else:
                val = None
                break
        if is_truthy_value(val, default=False):
            result.add(cmd.name)
    return result


def _is_gateway_available(cmd: CommandDef, config_overrides: set[str] | None = None) -> bool:
    """Check if *cmd* should appear in gateway surfaces (help, menus, mappings).

    Unconditionally available when ``cli_only`` is False.  When ``cli_only``
    is True but ``gateway_config_gate`` is set, the command is available only
    when the config value is truthy.  Pass *config_overrides* (from
    ``_resolve_config_gates()``) to avoid re-reading config for every command.
    """
    if not cmd.cli_only:
        return True
    if cmd.gateway_config_gate:
        overrides = config_overrides if config_overrides is not None else _resolve_config_gates()
        return cmd.name in overrides
    return False


def gateway_help_lines(only: Optional[frozenset[str]] = None) -> list[str]:
    """Generate gateway help text lines from the registry.

    ``only`` optionally restricts the listing to the named canonical
    commands — the gateway /help uses it to show the core set and points at
    ``/commands`` for the full paginated catalog instead of dumping every
    command into one message.
    """
    overrides = _resolve_config_gates()
    lines: list[str] = []
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        if only is not None and cmd.name not in only:
            continue
        args = f" {cmd.args_hint}" if cmd.args_hint else ""
        alias_parts: list[str] = []
        for a in cmd.aliases:
            # Skip internal aliases like reload_mcp (underscore variant)
            if a.replace("-", "_") == cmd.name.replace("-", "_") and a != cmd.name:
                continue
            alias_parts.append(f"`/{a}`")
        alias_note = f" (alias: {', '.join(alias_parts)})" if alias_parts else ""
        lines.append(f"`/{cmd.name}{args}` -- {cmd.description}{alias_note}")
    return lines


# The short default /help set on messaging platforms. The full catalog stays
# reachable via the paginated /commands — /help must fit a phone screen, not
# reproduce the registry. CLI-only or config-gated commands are omitted: the
# gateway availability filter applies first anyway, so a gated command that
# IS enabled on a deployment appears in the full /commands listing.
GATEWAY_HELP_CORE: frozenset[str] = frozenset({
    # Session lifecycle
    "new", "undo", "retry", "stop", "title", "sessions",
    # Model + permissions
    "model", "perm", "yolo",
    # Context
    "status", "context", "compact",
    # Turn control while the agent is busy
    "sendnow", "sendall",
    # Background + automation
    "agents", "goal",
    # Knowledge
    "memory",
    # Info
    "usage", "help", "commands",
})


def _iter_plugin_command_entries() -> list[tuple[str, str, str]]:
    """Yield (name, description, args_hint) tuples for all plugin slash commands.

    Plugin commands are registered via
    :func:`son_of_anton_cli.plugins.PluginContext.register_command`. They behave
    like ``CommandDef`` entries for gateway surfacing: they appear in the
    gateway command menu.

    Lookup is lazy so importing this module never forces plugin discovery
    (which can trigger filesystem scans and environment-dependent
    behavior).
    """
    try:
        from son_of_anton_cli.plugins import get_plugin_commands
    except Exception:
        return []
    try:
        commands = get_plugin_commands() or {}
    except Exception:
        return []
    entries: list[tuple[str, str, str]] = []
    for name, meta in commands.items():
        if not isinstance(name, str) or not isinstance(meta, dict):
            continue
        description = str(meta.get("description") or f"Run /{name}")
        args_hint = str(meta.get("args_hint") or "").strip()
        entries.append((name, description, args_hint))
    return entries


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inline auto-suggest (ghost text) for slash commands
# ---------------------------------------------------------------------------


