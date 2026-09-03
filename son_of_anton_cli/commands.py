"""Slash command definitions and autocomplete for the Son of Anton CLI.

Central registry for all slash commands. Every consumer -- CLI help, gateway
dispatch, Slack subcommand mapping, autocomplete --
derives its data from ``COMMAND_REGISTRY``.

To add a command: add a ``CommandDef`` entry to ``COMMAND_REGISTRY``.
To add an alias: set ``aliases=("short",)`` on the existing ``CommandDef``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from utils import is_truthy_value

# mtime-keyed memo of the /personality completion source. load_cli_config()
# does a full YAML parse + deep merge of the built-in defaults on every call,
# and the completer runs on every keystroke of /personality. The personalities
# list only changes when the config file changes on disk, so keying on
# path+mtime keeps the memo freshness-correct (same pattern as load_env and
# _nous_auth_status_cache). Falls back to a fresh load when the file cannot
# be stat'ed.
_personalities_memo: Optional[
    Tuple[Tuple[Optional[str], Optional[int], Optional[int]], Dict[str, Any]]
] = None


def _personalities_from_cli_config() -> Dict[str, Any]:
    """Return the available personalities map, memoised on config mtime.

    Wraps ``available_personalities(load_cli_config())`` — the single owner of
    built-ins + user overrides. Built-ins are static for the process lifetime,
    so keying on the config file's path+mtime+size keeps the memo
    freshness-correct.
    """
    global _personalities_memo
    from cli import load_cli_config
    from son_of_anton_cli.personality import available_personalities

    try:
        from son_of_anton_cli.config import get_config_path

        cfg_path = get_config_path()
        st = cfg_path.stat()
        sig = (str(cfg_path), st.st_mtime_ns, st.st_size)
    except Exception:
        sig = (None, None, None)

    if _personalities_memo is not None and _personalities_memo[0] == sig:
        return _personalities_memo[1]

    personalities = available_personalities(load_cli_config())
    _personalities_memo = (sig, personalities)
    return personalities

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
    # normal handler (e.g. /goal's control-verb whitelist, /queue's FIFO
    # enqueue, /model's custom busy-reject text).
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
VALID_BUSY_POLICIES: frozenset[str] = frozenset(
    {"dispatch", "reject", "interrupt_then_dispatch"}
)


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
    CommandDef("undo", "Back up N user turns and re-prompt (default 1)", "Session",
               args_hint="[N]"),
    CommandDef("title", "Set a title for the current session", "Session",
               args_hint="[name]"),
    CommandDef("worktree", "Show, list, create, or prune isolated git worktrees", "Session",
               cli_only=True, args_hint="[new [name]|list|prune [--dry-run]]",
               subcommands=("new", "list", "prune")),
    CommandDef("compress", "Compress conversation context (add 'here [N]' to keep recent N turns; --preview shows what would happen)", "Session",
               aliases=("compact",), args_hint="[here [N] | focus topic | --preview|--dry-run]"),
    CommandDef("rollback", "List or restore filesystem checkpoints (restores keep your hand-edits; --all overrides)", "Session",
               args_hint="[number] [--all]"),
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
    CommandDef("queue", "Queue a prompt for the next turn (doesn't interrupt)", "Session",
               args_hint="<prompt>",
               busy_policy="dispatch", busy_handler="queue"),
    CommandDef("steer", "Inject a message after the next tool call without interrupting", "Session",
               args_hint="<prompt>", busy_policy="dispatch", busy_handler="steer"),
    CommandDef("goal", "Set a standing goal Son of Anton works on across turns until achieved", "Session",
               args_hint="[text | draft <text> | show | gate add <cmd> | pause | resume | clear | status | wait <pid> | unwait]",
               busy_policy="dispatch", busy_handler="goal"),
    CommandDef("refine", "Review this conversation now and save lessons to memory/skills", "Session",
               args_hint="[focus instructions]"),
    CommandDef("status", "Show session, model, token, and context info", "Session",
               busy_policy="dispatch"),
    CommandDef("context", "Show detailed context window view with usage gauge, category breakdown, compression stats, and throughput", "Session",
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

    CommandDef("statusbar", "Toggle the context/model status bar", "Configuration",
               cli_only=True, aliases=("sb",)),
    CommandDef("timestamps", "Toggle [HH:MM] timestamps on messages and /history", "Configuration",
               cli_only=True, args_hint="[on|off|status]",
               subcommands=("on", "off", "status"), aliases=("ts",)),
    CommandDef("diff", "Show git changes in the working directory", "Info",
               args_hint="[staged|all|session] [--stat] [path...]",
               subcommands=("staged", "all", "session")),
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
    CommandDef("busy", "Control what Enter does while Son of Anton is working", "Configuration",
               cli_only=True, args_hint="[queue|steer|interrupt|status]",
               subcommands=("queue", "steer", "interrupt", "status")),

    # Tools & Skills
    CommandDef("tools", "Manage tools: /tools [list|disable|enable] [name...]", "Tools & Skills",
               args_hint="[list|disable|enable] [name...]", cli_only=True),
    CommandDef("toolsets", "List available toolsets", "Tools & Skills",
               cli_only=True),
    CommandDef("skills", "Search, install, inspect, or manage skills",
               "Tools & Skills", cli_only=True,
               gateway_config_gate="skills.write_approval",
               subcommands=("search", "browse", "inspect", "install", "audit",
                            "pending", "approve", "reject", "diff", "approval")),
    CommandDef("memory", "Review pending memory writes / toggle the approval gate",
               "Tools & Skills",
               args_hint="[pending|approve|reject|approval] [id|on|off]",
               subcommands=("pending", "approve", "reject", "approval")),
    CommandDef("bundles", "List skill bundles (aliases /<name> for multiple skills)",
               "Tools & Skills", execute="bundles"),
    CommandDef("learn", "Learn a reusable skill from anything you describe (dirs, URLs, this chat, notes)",
               "Tools & Skills", args_hint="<what to learn from>"),
    CommandDef("init", "Generate or update AGENTS.md project instructions from a repo scan",
               "Tools & Skills", args_hint="[notes]"),
    CommandDef("cron", "Manage scheduled tasks", "Tools & Skills",
               cli_only=True, args_hint="[subcommand]",
               subcommands=("list", "add", "create", "edit", "pause", "resume", "run", "remove")),
    # cli_only: the gateway runs the curator on its own hourly timer
    # (gateway/run.py maybe_run_curator) and has no slash dispatch for it, so
    # surfacing /curator in chat offered a command that could never run.
    CommandDef("curator", "Background skill maintenance (status, run, pin, archive, list-archived)",
               "Tools & Skills", cli_only=True, args_hint="[subcommand]",
               subcommands=("status", "run", "pause", "resume", "pin", "unpin", "restore", "list-archived")),
    CommandDef("reload", "Reload .env variables into the running session", "Tools & Skills",
               cli_only=True),
    CommandDef("reload-mcp", "Reload MCP servers from config", "Tools & Skills",
               aliases=("reload_mcp",)),
    CommandDef("reload-skills", "Re-scan ~/.son-of-anton/skills/ for newly installed or removed skills",
               "Tools & Skills", aliases=("reload_skills",)),
    CommandDef("plugins", "List installed plugins and their status",
               "Tools & Skills", cli_only=True),

    # Info
    CommandDef("commands", "Browse all commands and skills (paginated)", "Info",
               gateway_only=True, args_hint="[page]", busy_policy="dispatch",
               execute="gateway_commands"),
    CommandDef("help", "Show available commands (/help skills lists skill commands, /help <text> filters)", "Info", busy_policy="dispatch",
               execute="gateway_help", args_hint="[skills|<filter>]"),
    CommandDef("palette", "Open the fuzzy command palette (also Ctrl+P)", "Info",
               cli_only=True, busy_policy="dispatch"),
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
        "compress", "compact", "context", "ctx", "status",
    ),
    "Background & Automation": (
        "background", "bg", "btw", "agents", "tasks", "queue", "steer",
        "goal", "subgoal", "heartbeat", "hb", "refine", "loop", "proactive",
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
ACTIVE_SESSION_BYPASS_COMMANDS: frozenset[str] = frozenset(
    cmd.name for cmd in COMMAND_REGISTRY if cmd.busy_policy != "reject"
)


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
    /voice, /insights, /title, /resume, /retry, /undo, /compress,
    /usage, /reload-mcp, /sethome, /reset) would silently
    interrupt the agent AND get discarded, producing a zero-char
    response. See issue #5057 / PRs #6252, #10370, #4665.

    ACTIVE_SESSION_BYPASS_COMMANDS remains the subset of commands with
    explicit Level-2 handlers; the rest fall through to the catch-all.
    """
    return resolve_command(command_name) is not None if command_name else False


def _resolve_config_gates() -> set[str]:
    """Return canonical names of commands whose ``gateway_config_gate`` is truthy.

    Reads ``config.yaml`` and walks the dot-separated key path for each
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


def _requires_argument(args_hint: str) -> bool:
    """Return True when selecting a command without text would be incomplete."""
    return args_hint.strip().startswith("<")


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
    "status", "context", "compress",
    # Turn control while the agent is busy
    "queue", "steer",
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
    gateway command menu, in Slack's ``/son-of-anton`` subcommand mapping, and
    (via :func:`plugins.platforms.discord.adapter._register_slash_commands`) in
    Discord's native slash command picker.

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


def _clamp_command_names(
    entries: list[tuple[str, ...]],
    reserved: set[str],
) -> list[tuple[str, ...]]:
    """Enforce 32-char command name limit with collision avoidance.

    Platforms cap slash command names at 32 characters.
    Names exceeding the limit are truncated.  If truncation creates a duplicate
    (against *reserved* names or earlier entries in the same batch), the name is
    shortened to 31 chars and a digit ``0``-``9`` is appended to differentiate.
    If all 10 digit slots are taken the entry is silently dropped.

    Accepts tuples of any length >= 2.  Extra elements beyond ``(name, desc)``
    (e.g. ``cmd_key``) are passed through unchanged, so callers can attach
    metadata that survives the rename.
    """
    used: set[str] = set(reserved)
    result: list[tuple] = []
    for entry in entries:
        name, desc, *extra = entry
        if len(name) > _CMD_NAME_LIMIT:
            candidate = name[:_CMD_NAME_LIMIT]
            if candidate in used:
                prefix = name[:_CMD_NAME_LIMIT - 1]
                for digit in range(10):
                    candidate = f"{prefix}{digit}"
                    if candidate not in used:
                        break
                else:
                    # All 10 digit slots exhausted — skip entry
                    continue
            name = candidate
        if name in used:
            continue
        used.add(name)
        result.append((name, desc, *extra))
    return result


# Backward-compat alias.


# ---------------------------------------------------------------------------
# Shared skill/plugin collection for gateway platforms
# ---------------------------------------------------------------------------

def _collect_gateway_skill_entries(
    platform: str,
    max_slots: int,
    reserved_names: set[str],
    desc_limit: int = 100,
    sanitize_name: "Callable[[str], str] | None" = None,
) -> tuple[list[tuple[str, str, str]], int]:
    """Collect plugin + skill entries for a gateway platform.

    Priority order:
      1. Plugin slash commands (take precedence over skills)
      2. Built-in skill commands (fill remaining slots, alphabetical)

    Only skills are trimmed when the cap is reached.
    Hub-installed skills are excluded.  Per-platform disabled skills are
    excluded.

    Args:
        platform: Platform identifier for per-platform skill filtering
            (``"discord"``, ``"slack"``, etc.).
        max_slots: Maximum number of entries to return (remaining slots after
            built-in/core commands).
        reserved_names: Names already taken by built-in commands.  Mutated
            in-place as new names are added.
        desc_limit: Max description length (100 for Discord).
        sanitize_name: Optional name transform applied before clamping.  May
            return an empty string to signal "skip this entry".

    Returns:
        ``(entries, hidden_count)`` where *entries* is a list of
        ``(name, description, cmd_key)`` triples and *hidden_count* is the
        number of skill entries dropped due to the cap.  ``cmd_key`` is the
        original ``/skill-name`` key from :func:`get_skill_commands`.
    """
    all_entries: list[tuple[str, str, str]] = []

    # --- Tier 1: Plugin slash commands (never trimmed) ---------------------
    plugin_pairs: list[tuple[str, str]] = []
    try:
        from son_of_anton_cli.plugins import get_plugin_commands
        plugin_cmds = get_plugin_commands()
        for cmd_name in sorted(plugin_cmds):
            name = sanitize_name(cmd_name) if sanitize_name else cmd_name
            if not name:
                continue
            desc = plugin_cmds[cmd_name].get("description", "Plugin command")
            if len(desc) > desc_limit:
                desc = desc[:desc_limit - 3] + "..."
            plugin_pairs.append((name, desc))
    except Exception:
        pass

    plugin_pairs = _clamp_command_names(plugin_pairs, reserved_names)
    reserved_names.update(n for n, _ in plugin_pairs)
    # Plugins have no cmd_key — use empty string as placeholder
    for n, d in plugin_pairs:
        all_entries.append((n, d, ""))

    # --- Tier 2: Built-in skill commands (trimmed at cap) -----------------
    _platform_disabled: set[str] = set()
    try:
        from agent.skill_utils import get_disabled_skill_names
        _platform_disabled = get_disabled_skill_names(platform=platform)
    except Exception:
        pass

    skill_triples: list[tuple[str, str, str]] = []
    try:
        from agent.skill_commands import get_skill_commands
        from tools.skills_tool import SKILLS_DIR
        from agent.skill_utils import get_external_skills_dirs, get_project_skills_dirs
        _skills_dir = str(SKILLS_DIR.resolve())
        _hub_dir = str((SKILLS_DIR / ".hub").resolve()).rstrip("/") + "/"
        # Build set of allowed directory prefixes: local skills dir + any
        # user-configured ``skills.external_dirs`` + trusted project dirs.
        # Ensure each prefix ends
        # with ``/`` so ``/my-skills`` does not also match ``/my-skills-extra``.
        # Without this widening, external skills are visible in
        # ``son-of-anton skills list`` and the agent's ``/skill-name`` dispatch but
        # silently excluded from gateway slash menus (#8110).
        _allowed_prefixes = [_skills_dir.rstrip("/") + "/"]
        _allowed_prefixes.extend(
            str(d).rstrip("/") + "/" for d in get_external_skills_dirs()
        )
        _allowed_prefixes.extend(
            str(d).rstrip("/") + "/" for d in get_project_skills_dirs()
        )
        skill_cmds = get_skill_commands()
        for cmd_key in sorted(skill_cmds):
            info = skill_cmds[cmd_key]
            skill_path = info.get("skill_md_path", "")
            if not skill_path:
                continue
            if not any(skill_path.startswith(prefix) for prefix in _allowed_prefixes):
                continue
            if skill_path.startswith(_hub_dir):
                continue
            skill_name = info.get("name", "")
            if skill_name in _platform_disabled:
                continue
            raw_name = cmd_key.lstrip("/")
            name = sanitize_name(raw_name) if sanitize_name else raw_name
            if not name:
                continue
            desc = info.get("description", "")
            if len(desc) > desc_limit:
                desc = desc[:desc_limit - 3] + "..."
            skill_triples.append((name, desc, cmd_key))
    except Exception:
        pass

    # Clamp names; cmd_key is passed through as extra payload so it survives
    # any clamp-induced renames.
    skill_triples = _clamp_command_names(skill_triples, reserved_names)

    # Skills fill remaining slots — only tier that gets trimmed
    remaining = max(0, max_slots - len(all_entries))
    hidden_count = max(0, len(skill_triples) - remaining)
    for n, d, k in skill_triples[:remaining]:
        all_entries.append((n, d, k))

    return all_entries[:max_slots], hidden_count


# ---------------------------------------------------------------------------
# Platform-specific wrappers
# ---------------------------------------------------------------------------

def discord_skill_commands(
    max_slots: int,
    reserved_names: set[str],
) -> tuple[list[tuple[str, str, str]], int]:
    """Return skill entries for Discord slash command registration.

    Same priority and filtering logic as the gateway command menu
    (plugins > skills, hub excluded, per-platform disabled excluded), but
    adapted for Discord's constraints:

    - Hyphens are allowed in names (no ``-`` → ``_`` sanitization)
    - Descriptions capped at 100 chars (Discord's per-field max)

    Args:
        max_slots: Available command slots (100 minus existing built-in count).
        reserved_names: Names of already-registered built-in commands.

    Returns:
        ``(entries, hidden_count)`` where *entries* is a list of
        ``(discord_name, description, cmd_key)`` triples.  ``cmd_key`` is
        the original ``/skill-name`` key needed for the slash handler callback.
    """
    return _collect_gateway_skill_entries(
        platform="discord",
        max_slots=max_slots,
        reserved_names=set(reserved_names),  # copy — don't mutate caller's set
        desc_limit=100,
    )


def discord_skill_commands_by_category(
    reserved_names: set[str],
) -> tuple[dict[str, list[tuple[str, str, str]]], list[tuple[str, str, str]], int]:
    """Return skill entries organized by category for Discord ``/skill`` autocomplete.

    Skills whose directory is nested at least 2 levels under a scan root
    (e.g. ``creative/ascii-art/SKILL.md``) are grouped by their top-level
    category.  Root-level skills (e.g. ``some-skill/SKILL.md`` directly under a
    scan root) are returned as
    *uncategorized*.

    Scan roots include the local ``SKILLS_DIR`` **and** any configured
    ``skills.external_dirs`` — matching the widened filter applied to the
    flat ``discord_skill_commands()`` collector in #18741. Without this
    parity, external-dir skills are visible via ``son-of-anton skills list`` and
    the agent's ``/skill-name`` dispatch but silently absent from Discord's
    ``/skill`` autocomplete.

    Filtering mirrors :func:`discord_skill_commands`: hub skills excluded,
    per-platform disabled excluded, names clamped to 32 chars, descriptions
    clamped to 100 chars.

    The legacy 25-group × 25-subcommand caps (from the old nested
    ``/skill <cat> <name>`` layout) are **not** applied — the live caller
    (``_register_skill_group`` in ``gateway/platforms/discord.py``, refactored
    in PR #11580) flattens these results and feeds them into a single
    autocomplete callback, which scales to thousands of entries without any
    per-command payload concerns. ``hidden_count`` is retained in the return
    tuple for backward compatibility and still reports skills dropped for
    other reasons (32-char clamp collision vs a reserved name).

    Returns:
        ``(categories, uncategorized, hidden_count)``

        - *categories*: ``{category_name: [(name, description, cmd_key), ...]}``
        - *uncategorized*: ``[(name, description, cmd_key), ...]``
        - *hidden_count*: skills dropped due to name clamp collisions
          against already-registered command names.
    """
    from pathlib import Path as _P

    _platform_disabled: set[str] = set()
    try:
        from agent.skill_utils import get_disabled_skill_names
        _platform_disabled = get_disabled_skill_names(platform="discord")
    except Exception:
        pass

    # Collect raw skill data --------------------------------------------------
    categories: dict[str, list[tuple[str, str, str]]] = {}
    uncategorized: list[tuple[str, str, str]] = []
    # Map clamped-32-char-name → what it came from, so we can emit an
    # actionable warning on collision. Reserved (gateway-builtin) command
    # names are marked with a sentinel so the warning distinguishes
    # "skill collided with a reserved command" from "two skills collided
    # on the 32-char clamp" — the latter is the rename-worthy case.
    _names_used: dict[str, str] = dict.fromkeys(reserved_names, "<reserved>")
    hidden = 0

    try:
        from agent.skill_commands import get_skill_commands
        from agent.skill_utils import get_external_skills_dirs, get_project_skills_dirs
        from tools.skills_tool import SKILLS_DIR

        _skills_dir = SKILLS_DIR.resolve()
        _hub_dir = (SKILLS_DIR / ".hub").resolve()
        # Build list of (resolved_root, is_local) tuples. Each external dir
        # becomes its own scan root for category derivation — a skill at
        # ``<external>/mlops/foo/SKILL.md`` is still categorized as "mlops".
        _scan_roots: list[_P] = [_skills_dir]
        try:
            for ext in get_external_skills_dirs():
                try:
                    _scan_roots.append(_P(ext).resolve())
                except Exception:
                    continue
        except Exception:
            pass
        try:
            for proj in get_project_skills_dirs():
                try:
                    _scan_roots.append(_P(proj).resolve())
                except Exception:
                    continue
        except Exception:
            pass
        skill_cmds = get_skill_commands()

        for cmd_key in sorted(skill_cmds):
            info = skill_cmds[cmd_key]
            skill_path = info.get("skill_md_path", "")
            if not skill_path:
                continue
            sp = _P(skill_path).resolve()
            # Hub skills are loaded via the skill hub, not surfaced as
            # slash commands.
            if str(sp).startswith(str(_hub_dir)):
                continue
            # Accept skill if it lives under any scan root; record the
            # matching root so we can derive the category correctly.
            matched_root: _P | None = None
            for root in _scan_roots:
                try:
                    sp.relative_to(root)
                except ValueError:
                    continue
                matched_root = root
                break
            if matched_root is None:
                continue

            skill_name = info.get("name", "")
            if skill_name in _platform_disabled:
                continue

            raw_name = cmd_key.lstrip("/")
            # Clamp to 32 chars (Discord per-command name limit)
            discord_name = raw_name[:32]
            if discord_name in _names_used:
                # Two skills whose first 32 chars are identical. One wins
                # (the first one seen, which is alphabetical because the
                # caller iterates ``sorted(skill_cmds)``); the other is
                # dropped from Discord's /skill autocomplete.
                #
                # Silently counting this as ``hidden`` (the old behavior)
                # meant skill authors had no way to discover the drop —
                # their skill just didn't appear in the picker. Emit a
                # WARNING naming both sides so the author can rename the
                # losing skill's frontmatter name to something with a
                # distinct 32-char prefix.
                prior = _names_used[discord_name]
                if prior == "<reserved>":
                    logger.warning(
                        "Discord /skill: %r (from %r) collides on its 32-char "
                        "clamp with a reserved gateway command name %r — the "
                        "skill will not appear in the /skill autocomplete. "
                        "Rename the skill's frontmatter ``name:`` to differ "
                        "in its first 32 chars.",
                        discord_name, cmd_key, discord_name,
                    )
                else:
                    logger.warning(
                        "Discord /skill: %r and %r both clamp to %r on "
                        "Discord's 32-char command-name limit — only %r "
                        "will appear in the /skill autocomplete. Rename "
                        "one skill's frontmatter ``name:`` to differ in "
                        "its first 32 chars.",
                        prior, cmd_key, discord_name, prior,
                    )
                hidden += 1
                continue
            _names_used[discord_name] = cmd_key

            desc = info.get("description", "")
            if len(desc) > 100:
                desc = desc[:97] + "..."

            # Determine category from the relative path within the matched
            # scan root. e.g. creative/ascii-art/SKILL.md → ("creative", ...)
            rel = sp.parent.relative_to(matched_root)
            parts = rel.parts
            if len(parts) >= 2:
                cat = parts[0]
                categories.setdefault(cat, []).append((discord_name, desc, cmd_key))
            else:
                uncategorized.append((discord_name, desc, cmd_key))
    except Exception:
        pass

    return categories, uncategorized, hidden


# ---------------------------------------------------------------------------
# Slack native slash commands
# ---------------------------------------------------------------------------

# Slack slash command name constraints: lowercase a-z, 0-9, hyphens,
# underscores. Max 32 chars. Slack app manifest accepts up to 50 slash
# commands per app.
_SLACK_MAX_SLASH_COMMANDS = 50
_SLACK_NAME_LIMIT = 32
_SLACK_INVALID_CHARS = re.compile(r"[^a-z0-9_\-]")
_SLACK_RESERVED_COMMANDS = frozenset({
    # Built-in Slack slash commands that cannot be registered by apps.
    # https://slack.com/help/articles/201259356-Use-built-in-slash-commands
    "me", "status", "away", "dnd", "shrug", "remind", "msg", "feed",
    "who", "collapse", "expand", "leave", "join", "open", "search",
    "topic", "mute", "pro", "shortcuts",
})

# High-value aliases that must survive Slack's 50-slash cap even when the
# registry fills up. Without this, adding a new canonical command silently
# clamps off low-priority aliases (they're added in the second pass), so a
# long-standing native slash like /btw could disappear just because an
# unrelated command landed. These claim their slots right after /son-of-anton,
# ahead of both canonical names and the rest of the aliases. Anything not
# listed here still degrades gracefully (reachable via /son-of-anton <command>).
# Keep this list TIGHT: every pinned alias takes a slot a canonical command
# would otherwise get, and the Slack parity test fails when a canonical
# gets clamped ("reset" was unpinned for exactly that — /new keeps its
# native slot, the alias spelling stays reachable via /son-of-anton reset).
_SLACK_PRIORITY_ALIASES = ("btw", "bg")

# Canonical commands intentionally NOT given a native Slack slash slot. Slack
# caps apps at 50 slash commands and the registry is at that ceiling; rather
# than let the clamp silently drop whichever command sorts last, we explicitly
# route a few low-frequency commands through
# ``/son-of-anton <command>`` on Slack only. They remain native on every other
# surface (CLI, TUI, Discord). Keep this list TIGHT and intentional —
# an entry here is a deliberate
# "Slack-via-/son-of-anton" decision, not a silent clamp.
#   - topup: the billing/balance surface; reached via /son-of-anton topup on Slack.
#     (the rehaul folded the old /credits + /billing surfaces into /topup.)
#   - moa: high-cost slash mode, available through /son-of-anton moa to avoid
#     displacing existing native Slack slash commands at the 50-command cap.
#   - debug: the log/report upload surface; reached via /son-of-anton debug on Slack.
#   - egress: Docker-only proxy status; reachable as /son-of-anton egress on Slack.
#   - init: repo-scan AGENTS.md bootstrap — a cwd-centric dev command that is
#     rare from Slack; reachable as /son-of-anton init. Without this entry, adding
#     /init clamps /version off the native list and breaks Slack parity.
#   - version: low-frequency info command; reachable as /son-of-anton version on
#     Slack. Demoted when /context claimed a native slot (context is a
#     recurring inspection surface; version is a one-off lookup); the demotion
#     also absorbs the native slot /approvals now consumes at the 50-cap.
#   - diff: git working-tree diff; reached via /son-of-anton diff on Slack so it
#     doesn't displace an existing native slash at the 50-command cap.
#   - update: low-frequency self-update maintenance command; reached via
#     /son-of-anton update on Slack. Demoted to free the native slot /approvals now
#     claims — without this entry /approvals tips the registry past the 50-cap
#     and silently clamps /update off, breaking Slack parity.
#   - heartbeat: session heartbeat management; reached via /son-of-anton heartbeat
#     on Slack. Added at the 50-cap — a native slot would clamp /insights.
#   - refine: on-demand memory/skill review; reached via /son-of-anton refine on
#     Slack. Added at the 50-cap — a native slot would clamp an existing
#     native slash.
#   - pause: global emergency stop; reached via /son-of-anton pause [off] on
#     Slack. Added at the 50-cap — a native slot would clamp /platform.
#   - whoami: one-off identity lookup; reached via /son-of-anton whoami on Slack.
#     Demoted when /loop claimed a native slot (loop is a recurring
#     interactive surface; whoami is a rare debug lookup) — without this
#     entry /loop tips the registry past the 50-cap and silently clamps
#     /platform, breaking Slack parity.
#   - platform: informational platform/environment lookup; reached via
#     /son-of-anton platform on Slack. Demoted when /save became gateway-available
#     (session export is an interactive surface; platform is a rare
#     informational lookup) — without this entry /save tips the registry
#     past the 50-cap and silently clamps /platform, breaking parity.
_SLACK_VIA_SON_OF_ANTON_ONLY = frozenset({"debug", "egress", "init", "version", "diff", "update", "heartbeat", "refine", "pause", "whoami", "platform"})


def _sanitize_slack_name(raw: str) -> str:
    """Convert a command name to a valid Slack slash command name.

    Slack allows lowercase a-z, digits, hyphens, and underscores. Max 32
    chars. Uppercase is lowercased; invalid chars are stripped.
    """
    name = raw.lower()
    name = _SLACK_INVALID_CHARS.sub("", name)
    name = name.strip("-_")
    return name[:_SLACK_NAME_LIMIT]


def slack_native_slashes() -> list[tuple[str, str, str]]:
    """Return (slash_name, description, usage_hint) triples for Slack.

    Every gateway-available command in ``COMMAND_REGISTRY`` is surfaced as
    a standalone Slack slash command (e.g. ``/btw``, ``/stop``, ``/model``),
    matching Discord's model where every command is a
    first-class slash and not a ``/son-of-anton <verb>`` subcommand.

    Both canonical names and aliases are included so users can type any
    documented form (e.g. ``/background``, ``/bg``, and ``/btw`` all work).
    Plugin-registered slash commands are included too.

    Commands whose sanitized name collides with a Slack built-in
    (e.g. ``/status``, ``/me``, ``/join``) are silently skipped.  Users
    can still reach them via ``/son-of-anton <command>``.

    Results are clamped to Slack's 50-command limit with duplicate-name
    avoidance. ``/son-of-anton`` is always reserved as the first entry so the
    legacy ``/son-of-anton <subcommand>`` form keeps working for anything that
    gets dropped by the clamp or for free-form questions.
    """
    overrides = _resolve_config_gates()
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    # Reserve /son-of-anton as the catch-all top-level command.
    entries.append(("son-of-anton", "Talk to Son of Anton or run a subcommand", "[subcommand] [args]"))
    seen.add("son-of-anton")

    def _add(name: str, desc: str, hint: str) -> None:
        slack_name = _sanitize_slack_name(name)
        if not slack_name or slack_name in seen:
            return
        if slack_name in _SLACK_RESERVED_COMMANDS:
            return
        if slack_name in _SLACK_VIA_SON_OF_ANTON_ONLY:
            # Intentionally Slack-via-/son-of-anton only (see _SLACK_VIA_SON_OF_ANTON_ONLY).
            return
        if len(entries) >= _SLACK_MAX_SLASH_COMMANDS:
            return
        # Slack description cap is 2000 chars; keep it short.
        entries.append((slack_name, desc[:140], hint[:100]))
        seen.add(slack_name)

    # Priority pass: pin high-value aliases (e.g. /btw, /bg, /reset) ahead of
    # everything except /son-of-anton, so a new canonical command can never silently
    # clamp them off the 50-slash cap. Each alias borrows its parent command's
    # description and hint.
    _alias_to_cmd = {
        alias: cmd
        for cmd in COMMAND_REGISTRY
        if _is_gateway_available(cmd, overrides)
        for alias in cmd.aliases
    }
    for alias in _SLACK_PRIORITY_ALIASES:
        cmd = _alias_to_cmd.get(alias)
        if cmd is not None:
            _add(alias, f"Alias for /{cmd.name} — {cmd.description}", cmd.args_hint or "")

    # First pass: canonical names (so they win slots if we hit the cap).
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        _add(cmd.name, cmd.description, cmd.args_hint or "")

    # Second pass: aliases.
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        for alias in cmd.aliases:
            # Skip aliases that only differ from canonical by case/punctuation
            # normalization (already covered by _add dedup).
            _add(alias, f"Alias for /{cmd.name} — {cmd.description}", cmd.args_hint or "")

    # Third pass: plugin commands.
    for name, description, args_hint in _iter_plugin_command_entries():
        _add(name, description, args_hint or "")

    return entries


def slack_app_manifest(request_url: str = "https://son-of-anton.local/slack/commands") -> dict[str, Any]:
    """Generate a Slack app manifest with all gateway commands as slashes.

    ``request_url`` is required by Slack's manifest schema for every slash
    command, but in Socket Mode (which we use) Slack ignores it and routes
    the command event through the WebSocket. A placeholder URL is fine.

    The returned dict is the ``features.slash_commands`` portion only —
    callers compose it into a full manifest (or merge into an existing
    one). Keeping it narrow avoids coupling us to the rest of the manifest
    schema (display_information, oauth_config, settings, etc.) which users
    set up once in the Slack UI and rarely change.
    """
    slashes = []
    for name, desc, usage in slack_native_slashes():
        entry = {
            "command": f"/{name}",
            "description": desc or f"Run /{name}",
            "should_escape": False,
            "url": request_url,
        }
        if usage:
            entry["usage_hint"] = usage
        slashes.append(entry)
    return {"features": {"slash_commands": slashes}}


def slack_subcommand_map() -> dict[str, str]:
    """Return subcommand -> /command mapping for Slack /son-of-anton handler.

    Maps both canonical names and aliases so /son-of-anton bg do stuff works
    the same as /son-of-anton background do stuff.

    Plugin-registered slash commands are included so ``/son-of-anton <plugin-cmd>``
    routes through the plugin handler.
    """
    overrides = _resolve_config_gates()
    mapping: dict[str, str] = {}
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        mapping[cmd.name] = f"/{cmd.name}"
        for alias in cmd.aliases:
            mapping[alias] = f"/{alias}"
    for name, _description, _args_hint in _iter_plugin_command_entries():
        if name not in mapping:
            mapping[name] = f"/{name}"
    return mapping


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Inline auto-suggest (ghost text) for slash commands
# ---------------------------------------------------------------------------



def _file_size_label(path: str) -> str:
    """Return a compact human-readable file size, or '' on error."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}K"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}M"
    return f"{size / (1024 * 1024 * 1024):.1f}G"
