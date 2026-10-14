"""
MCP Server Management CLI — ``son-of-anton mcp`` subcommand.

Implements ``son-of-anton mcp list/test/login/reauth`` for MCP servers declared
in config.toml; servers are never installed or added from here.

Relies on tools/mcp_tool.py for connection/discovery and keeps
configuration in ~/.son-of-anton/config.toml under the ``mcp_servers`` key.
"""

import asyncio
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

from son_of_anton_cli.config import (
    load_config,
    get_son_of_anton_home,  # noqa: F401 — used by test mocks
)
from son_of_anton_cli.colors import Colors, color
from son_of_anton_cli.mcp_security import validate_mcp_server_entry
from tools.mcp_tool import _ENV_VAR_PATTERN, _env_ref_name

logger = logging.getLogger(__name__)


# ─── UI Helpers ───────────────────────────────────────────────────────────────

def _info(text: str):
    print(color(f"  {text}", Colors.DIM))

def _success(text: str):
    print(color(f"  ✓ {text}", Colors.GREEN))

def _warning(text: str):
    print(color(f"  ⚠ {text}", Colors.YELLOW))

def _error(text: str):
    print(color(f"  ✗ {text}", Colors.RED))


# ─── Config Helpers ───────────────────────────────────────────────────────────

def _get_mcp_servers(config: Optional[dict] = None) -> Dict[str, dict]:
    """Return the ``mcp_servers`` dict from config, or empty dict."""
    if config is None:
        config = load_config()
    servers = config.get("mcp_servers")
    if not servers or not isinstance(servers, dict):
        return {}
    return servers


# ─── Discovery (temporary connect) ───────────────────────────────────────────

def _resolve_mcp_server_config(config: dict) -> dict:
    """Resolve ``${ENV}`` placeholders in a server config before connecting.

    Mirrors ``_load_mcp_config()`` in ``tools/mcp_tool.py``: load
    ``~/.son-of-anton/.env`` into ``os.environ`` and recursively interpolate any
    ``${VAR}`` placeholders. The CLI builds header templates like
    ``Authorization: Bearer ${MCP_X_API_KEY}`` but the probe path never
    resolved them, so the discovery probe sent the literal placeholder and
    auth-requiring servers (e.g. n8n) returned 401 — while runtime tool
    loading worked because it interpolates. (#37792)
    """
    from tools.mcp_tool import _interpolate_env_vars

    try:
        from son_of_anton_cli.env_loader import load_son_of_anton_dotenv
        load_son_of_anton_dotenv()
    except Exception:  # pragma: no cover — defensive
        pass
    return _interpolate_env_vars(config)


def _probe_single_server(
    name: str, config: dict, connect_timeout: Optional[float] = None, *, details: Optional[dict] = None
) -> List[Tuple[str, str]]:
    """Temporarily connect to one MCP server, list its tools, disconnect.

    Returns list of ``(tool_name, description)`` tuples.
    Raises on connection failure.

    ``details``: optional dict the probe fills with extra capability counts
    (``prompts``, ``resources``) — an out-param so the return shape stays
    stable for existing CLI callers.
    """
    issues = validate_mcp_server_entry(name, config)
    if issues:
        raise ValueError("; ".join(issues))

    from tools.mcp_tool import (
        _ensure_mcp_loop,
        _run_on_mcp_loop,
        _connect_server,
        _stop_mcp_loop_if_idle,
        _parse_boolish,
    )

    config = _resolve_mcp_server_config(config)
    if connect_timeout is None:
        raw_timeout = config.get("connect_timeout", 30)
        try:
            connect_timeout = max(1.0, float(raw_timeout))
        except (TypeError, ValueError):
            connect_timeout = 30.0

    _ensure_mcp_loop()
    tools_found: List[Tuple[str, str]] = []

    async def _probe():
        server = await asyncio.wait_for(
            _connect_server(name, config), timeout=connect_timeout
        )
        try:
            for t in server._tools:
                desc = getattr(t, "description", "") or ""
                # Truncate long descriptions for display
                if len(desc) > 80:
                    desc = desc[:77] + "..."
                tools_found.append((t.name, desc))
            if details is not None:
                # Per-tool registry-schema sizes so the desktop can estimate the
                # per-call token cost a server adds. Uses the SAME converted
                # schema the agent registers (name + description + normalized
                # parameters) — i.e. what actually rides on every model call.
                # Additive-optional wire field: best-effort, absent on failure.
                try:
                    import json as _json

                    from tools.mcp_tool import _convert_mcp_schema

                    details["schema_chars"] = {
                        t.name: len(
                            _json.dumps(
                                _convert_mcp_schema(name, t),
                                separators=(",", ":"),
                                default=str,
                            )
                        )
                        for t in server._tools
                    }
                except Exception:  # pragma: no cover — display-only extra
                    pass
            if details is not None:
                # Gate the capability probes exactly like runtime utility-tool
                # registration (tools.mcp_tool._select_utility_schemas):
                #   1. honour the user's tools.prompts / tools.resources config
                #   2. only call a family the server actually advertises.
                # Without this the "Test server" probe fired prompts/list and
                # resources/list at every server unconditionally — so a server
                # that rejects those methods (e.g. Unreal's MCP server, which
                # answers "Call to unknown method 'prompts/list'") logged a hard
                # error, and setting tools.prompts: false did NOT suppress it.
                tools_filter = config.get("tools") or {}
                prompts_enabled = _parse_boolish(
                    tools_filter.get("prompts"), default=True
                )
                resources_enabled = _parse_boolish(
                    tools_filter.get("resources"), default=True
                )
                advertised_caps = getattr(
                    getattr(server, "initialize_result", None),
                    "capabilities",
                    None,
                )

                def _advertises(cap_attr: str) -> bool:
                    # When no capability info was captured (legacy fixtures /
                    # older servers) preserve the old always-try behaviour.
                    if advertised_caps is None:
                        return True
                    return getattr(advertised_caps, cap_attr, None) is not None

                # Capability probes are best-effort: servers without the
                # capability raise, which just means "0".
                if prompts_enabled and _advertises("prompts"):
                    try:
                        result = await server.session.list_prompts()
                        details["prompts"] = len(result.prompts)
                    except Exception:
                        pass
                if resources_enabled and _advertises("resources"):
                    try:
                        result = await server.session.list_resources()
                        details["resources"] = len(result.resources)
                    except Exception:
                        pass
        finally:
            await server.shutdown()

    try:
        _run_on_mcp_loop(_probe(), timeout=connect_timeout + 10)
    except BaseException as exc:
        raise _unwrap_exception_group(exc) from None
    finally:
        _stop_mcp_loop_if_idle()

    return tools_found


def _oauth_tokens_present(name: str) -> bool:
    """Return True if an OAuth token file exists on disk for ``name``.

    Used after ``son-of-anton mcp login`` to distinguish a genuine authentication
    from a probe that succeeded only because the server allowed
    initialize/tools-list without auth (so no token was ever acquired).
    """
    try:
        from tools.mcp_oauth import SonOfAntonTokenStorage
        return SonOfAntonTokenStorage(name).has_cached_tokens()
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Could not check OAuth tokens for '%s': %s", name, exc)
        # Be permissive on unexpected errors: don't block a real success.
        return True


def _unwrap_exception_group(exc: BaseException) -> Exception:
    """Extract the root-cause exception from anyio TaskGroup wrappers.

    The MCP SDK uses anyio task groups, which wrap errors in
    ``BaseExceptionGroup`` / ``ExceptionGroup``.  This makes error
    messages opaque ("unhandled errors in a TaskGroup").  We unwrap
    to surface the real cause (e.g. "401 Unauthorized").
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    # Return a plain Exception so callers can catch normally
    if isinstance(exc, Exception):
        return exc
    return RuntimeError(str(exc))


# ─── son-of-anton mcp list ──────────────────────────────────────────────────────────

def cmd_mcp_list(args=None):
    """List all configured MCP servers."""
    servers = _get_mcp_servers()

    if not servers:
        print()
        _info("No MCP servers configured.")
        print()
        _info("Declare MCP servers under mcp_servers in config.toml (e.g. your Nix module's settings).")
        print()
        return

    print()
    print(color("  MCP Servers:", Colors.CYAN + Colors.BOLD))
    print()

    # Table header
    print(f"  {'Name':<16} {'Transport':<30} {'Tools':<12} {'Status':<10}")
    print(f"  {'─' * 16} {'─' * 30} {'─' * 12} {'─' * 10}")

    for name, cfg in servers.items():
        # Transport info
        if "url" in cfg:
            url = cfg["url"]
            # Truncate long URLs
            if len(url) > 28:
                url = url[:25] + "..."
            transport = url
        elif "command" in cfg:
            cmd = cfg["command"]
            cmd_args = cfg.get("args", [])
            if isinstance(cmd_args, list) and cmd_args:
                transport = f"{cmd} {' '.join(str(a) for a in cmd_args[:2])}"
            else:
                transport = cmd
            if len(transport) > 28:
                transport = transport[:25] + "..."
        else:
            transport = "?"

        # Tool count
        tools_cfg = cfg.get("tools", {})
        if isinstance(tools_cfg, dict):
            include = tools_cfg.get("include")
            exclude = tools_cfg.get("exclude")
            if include and isinstance(include, list):
                tools_str = f"{len(include)} selected"
            elif exclude and isinstance(exclude, list):
                tools_str = f"-{len(exclude)} excluded"
            else:
                tools_str = "all"
        else:
            tools_str = "all"

        # Enabled status
        enabled = cfg.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.lower() in {"true", "1", "yes"}
        status = color("✓ enabled", Colors.GREEN) if enabled else color("✗ disabled", Colors.DIM)

        print(f"  {name:<16} {transport:<30} {tools_str:<12} {status}")

    print()


# ─── son-of-anton mcp test ──────────────────────────────────────────────────────────

def cmd_mcp_test(args):
    """Test connection to an MCP server."""
    name = args.name
    servers = _get_mcp_servers()

    if name not in servers:
        _error(f"Server '{name}' not found in config.")
        available = list(servers.keys())
        if available:
            _info(f"Available: {', '.join(available)}")
        return

    cfg = servers[name]
    print()
    print(color(f"  Testing '{name}'...", Colors.CYAN))

    # Show transport info
    if "url" in cfg:
        _info(f"Transport: HTTP → {cfg['url']}")
    else:
        cmd = cfg.get("command", "?")
        _info(f"Transport: stdio → {cmd}")

    # Show auth info (masked)
    auth_type = cfg.get("auth", "")
    headers = cfg.get("headers", {})
    if auth_type == "oauth":
        _info("Auth: OAuth 2.1 PKCE")
    elif headers:
        for k, v in headers.items():
            if isinstance(v, str) and ("key" in k.lower() or "auth" in k.lower()):
                # Mask the value (accepts ${VAR} and Cursor-style ${env:VAR})
                resolved = _ENV_VAR_PATTERN.sub(lambda m: os.getenv(_env_ref_name(m.group(1)), ""), v)
                if len(resolved) > 8:
                    masked = resolved[:4] + "***" + resolved[-4:]
                else:
                    masked = "***"
                print(f"    {k}: {masked}")
    else:
        _info("Auth: none")

    # Attempt connection
    start = time.monotonic()
    try:
        tools = _probe_single_server(name, cfg)
        elapsed_ms = (time.monotonic() - start) * 1000
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        _error(f"Connection failed ({elapsed_ms:.0f}ms): {exc}")
        return

    _success(f"Connected ({elapsed_ms:.0f}ms)")
    _success(f"Tools discovered: {len(tools)}")

    if tools:
        print()
        for tool_name, desc in tools:
            short = desc[:55] + "..." if len(desc) > 55 else desc
            print(f"    {color(tool_name, Colors.GREEN):36s} {short}")
    print()


# ─── son-of-anton mcp login ────────────────────────────────────────────────────────

def _reauth_oauth_server(name: str, server_config: dict) -> bool:
    """Force a fresh OAuth flow for one server. Returns True on success.

    Wipes cached OAuth state (disk + in-process MCPOAuthManager cache),
    re-probes to trigger the browser flow, and verifies a token actually
    landed before reporting success. Shared by ``son-of-anton mcp login`` and
    ``son-of-anton mcp reauth`` so both behave identically for a single server.
    """
    url = server_config.get("url")
    if not url:
        _error(f"Server '{name}' has no URL — not an OAuth-capable server")
        return False
    if server_config.get("auth") != "oauth":
        _error(f"Server '{name}' is not configured for OAuth (auth={server_config.get('auth')})")
        _info("Set auth = \"oauth\" on its mcp_servers entry in config.toml.")
        return False

    # Wipe both disk and in-memory cache so the next probe forces a fresh
    # OAuth flow.
    try:
        from tools.mcp_oauth_manager import get_manager
        get_manager().remove(name)
    except Exception as exc:
        _warning(f"Could not clear existing OAuth state: {exc}")

    print()
    _info(f"Starting OAuth flow for '{name}'...")

    # Probe triggers the OAuth flow (browser redirect + callback capture).
    # Honor the server's configured connect_timeout so a human has enough
    # time to complete the browser sign-in; the 30s default is too tight for
    # an interactive OAuth round-trip. Floor at 315s — the OAuth callback
    # window (300s in mcp_oauth) plus headroom — matching the GUI re-auth
    # path in web_server.py so CLI and dashboard behave identically.
    #
    # force_interactive_oauth: `son-of-anton mcp login` is *explicitly* user-
    # initiated even when stdin isn't a TTY (Son of Anton desktop / agent-
    # spawned terminals). Without this, OAuth refuses before opening a
    # browser because _is_interactive() only checks sys.stdin.isatty().
    try:
        from tools.mcp_oauth import force_interactive_oauth

        _login_connect_timeout = server_config.get("connect_timeout")
        try:
            _login_connect_timeout = float(_login_connect_timeout)
        except (TypeError, ValueError):
            _login_connect_timeout = 0.0
        _login_connect_timeout = max(_login_connect_timeout, 315.0)
        with force_interactive_oauth():
            tools = _probe_single_server(
                name, server_config, connect_timeout=_login_connect_timeout
            )
        # A clean probe is NOT proof of authentication. Some MCP servers
        # (notably Google's official Drive server) serve initialize +
        # tools/list WITHOUT auth, so the probe lists tools even when the
        # OAuth flow never completed — e.g. dynamic client registration
        # 400'd because the provider doesn't support RFC 7591. Reporting
        # "Authenticated — N tools" in that case is a false success: every
        # real tool call later hangs until timeout because there's no token.
        # Verify a token actually landed on disk before claiming success.
        if not _oauth_tokens_present(name):
            _warning(
                "Server responded, but no OAuth token was obtained — "
                "authentication did not complete."
            )
            print()
            _info(
                "Some providers (e.g. Google Drive, Atlassian) do not support "
                "automatic client registration. For those you must create an "
                "OAuth client yourself and add its credentials to config.toml:"
            )
            print()
            print(color("    mcp_servers:", Colors.DIM))
            print(color(f"      {name}:", Colors.DIM))
            print(color(f"        url: {url}", Colors.DIM))
            print(color("        auth: oauth", Colors.DIM))
            print(color("        oauth:", Colors.DIM))
            print(color("          client_id: \"<your-oauth-client-id>\"", Colors.DIM))
            print(color("          client_secret: \"<your-oauth-client-secret>\"", Colors.DIM))
            print()
            _info("Then re-run `son-of-anton mcp login " + name + "`.")
            return False
        if tools:
            _success(f"Authenticated — {len(tools)} tool(s) available")
        else:
            _success("Authenticated (server reported no tools)")
        return True
    except Exception as exc:
        try:
            from tools.mcp_oauth import humanize_oauth_registration_error

            humanized = humanize_oauth_registration_error(
                name, exc, server_url=url
            )
        except Exception:
            humanized = None
        _error(f"Authentication failed: {humanized or exc}")
        return False


def cmd_mcp_login(args):
    """Force re-authentication for an OAuth-based MCP server.

    Deletes cached tokens (both on disk and in the running process's
    MCPOAuthManager cache) and triggers a fresh OAuth flow via the
    existing probe path.

    Use this when:
      - Tokens are stuck in a bad state (server revoked, refresh token
        consumed by an external process, etc.)
      - You want to re-authenticate to change scopes or account
      - A tool call returned ``needs_reauth: true``
    """
    name = args.name
    servers = _get_mcp_servers()

    if name not in servers:
        _error(f"Server '{name}' not found in config.")
        if servers:
            _info(f"Available servers: {', '.join(servers)}")
        return

    _reauth_oauth_server(name, servers[name])


def cmd_mcp_reauth(args):
    """Re-authenticate one OAuth MCP server, or all of them sequentially.

    ``son-of-anton mcp reauth <name>`` re-auths a single server (same as ``login``).
    ``son-of-anton mcp reauth --all`` discovers every ``auth: oauth`` server in
    config and re-auths them ONE AT A TIME.

    Serial-by-design: a human can only complete one browser OAuth flow at a
    time, so re-authing all servers concurrently would open N tabs at once
    and N-1 would time out. This is the self-service fix for the recurring
    stale-client ritual in GH#36767 (and avoids the startup popup storm when
    several servers go stale at once).
    """
    servers = _get_mcp_servers()
    do_all = getattr(args, "all", False)
    name = getattr(args, "name", None)

    if do_all:
        oauth_servers = [
            (n, c) for n, c in servers.items()
            if c.get("auth") == "oauth" and c.get("url")
        ]
        if not oauth_servers:
            _info("No OAuth-based MCP servers found in config.")
            return
        print()
        _info(f"Re-authenticating {len(oauth_servers)} OAuth server(s) one at a time...")
        succeeded = 0
        for n, c in oauth_servers:
            print()
            print(color(f"  ── {n} ──", Colors.CYAN + Colors.BOLD))
            if _reauth_oauth_server(n, c):
                succeeded += 1
        print()
        _success(f"Re-authenticated {succeeded}/{len(oauth_servers)} server(s)")
        return

    if not name:
        _error("Specify a server name, or use --all to re-auth every OAuth server.")
        _info("Usage: son-of-anton mcp reauth <name>   |   son-of-anton mcp reauth --all")
        return
    if name not in servers:
        _error(f"Server '{name}' not found in config.")
        if servers:
            _info(f"Available servers: {', '.join(servers)}")
        return

    _reauth_oauth_server(name, servers[name])


# ─── Dispatcher ───────────────────────────────────────────────────────────────

def mcp_command(args):
    """Main dispatcher for ``son-of-anton mcp`` subcommands.

    MCP servers are declared in config.toml (``mcp_servers``), e.g. by the
    Nix module's settings. Son of Anton never installs or adds one itself, so
    these commands only inspect, test and authenticate declared servers.
    """
    action = getattr(args, "mcp_action", None)

    if action == "serve":
        from mcp_serve import run_mcp_server
        run_mcp_server(verbose=getattr(args, "verbose", False))
        return

    handlers = {
        "list": cmd_mcp_list,
        "ls": cmd_mcp_list,
        "test": cmd_mcp_test,
        "login": cmd_mcp_login,
        "reauth": cmd_mcp_reauth,
    }

    handler = handlers.get(action)
    if handler:
        handler(args)
        return

    cmd_mcp_list(args)
    print(color("  Commands:", Colors.CYAN))
    _info("son-of-anton mcp serve                              Run as MCP server")
    _info("son-of-anton mcp list                               List declared servers")
    _info("son-of-anton mcp test <name>                        Test connection")
    _info("son-of-anton mcp login <name>                       Re-authenticate OAuth")
    _info("son-of-anton mcp reauth <name> | --all              Re-auth one or all OAuth servers")
    _info("Servers are declared under mcp_servers in config.toml (e.g. your Nix module).")
    print()
