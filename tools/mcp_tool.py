#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Client Support

Connects to external MCP servers via stdio or Streamable HTTP transport,
discovers their tools, and registers them into the son-of-anton tool registry
so the agent can call them like any built-in tool.

Configuration is read from ``mcp_servers`` in config.toml. Each server has
either ``command``/``args``/``env`` (stdio) or ``url``/``headers`` (HTTP), plus
optional ``timeout`` (per tool call, default 300s), ``connect_timeout``
(default 60s), ``keepalive_interval`` (default 180s, floored at 5s),
``sampling`` and ``elicitation`` settings. ``${VAR}`` placeholders resolve from
the environment.

Architecture:
    A dedicated background event loop (_mcp_loop) runs in a daemon thread.
    Each MCP server runs as a long-lived asyncio Task on this loop, keeping
    its transport context alive. Tool call coroutines are scheduled onto the
    loop via ``run_coroutine_threadsafe()``.

    On shutdown, each server Task is signalled to exit its ``async with``
    block, ensuring the anyio cancel-scope cleanup happens in the *same*
    Task that opened the connection (required by anyio).

Thread safety:
    _servers and _mcp_loop/_mcp_thread are accessed from both the MCP
    background thread and caller threads.  All mutations are protected by
    _lock.
"""

import asyncio
import contextvars
import concurrent.futures
import errno
import fnmatch
import json
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
from types import SimpleNamespace
from typing import Callable
from datetime import datetime
from typing import Any, Coroutine, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from tools.registry import tool_error
from tools.ansi_strip import strip_unicode_tags

logger = logging.getLogger(__name__)


# Hard allocation ceiling for a single MCP text payload (chars): the first
# line of defense against a server returning multi-megabyte text. It sits far
# ABOVE the budget layer's 50K MCP spillover threshold (tools/budget_config.py)
# so ordinary large results reach spillover intact, while only pathological
# floods are lossy-truncated here (40% head / 60% tail).
_MCP_HARD_RESULT_CAP_CHARS = 2_000_000


def _truncate_mcp_text_result(text: str, max_chars: int = _MCP_HARD_RESULT_CAP_CHARS) -> str:
    """Bound pathological MCP text before it propagates."""
    if len(text) <= max_chars:
        return text
    head_chars = int(max_chars * 0.4)
    tail_chars = max_chars - head_chars
    omitted = len(text) - head_chars - tail_chars
    return (
        text[:head_chars]
        + f"\n\n... [MCP RESULT TRUNCATED - {omitted:,} chars omitted "
          f"out of {len(text):,} total] ...\n\n"
        + text[-tail_chars:]
    )

# ---------------------------------------------------------------------------
# Stdio subprocess stderr redirection
# ---------------------------------------------------------------------------
#
# The SDK's ``stdio_client`` defaults the subprocess stderr to our real
# stderr, i.e. the TTY the TUI is rendering on. Every stdio MCP server's
# stderr goes to a shared log file (~/.son-of-anton/logs/mcp-stderr.log)
# instead, with a header line per server start. Falls back to os.devnull.

_mcp_stderr_log_fh: Optional[Any] = None
_mcp_stderr_log_lock = threading.Lock()


def _get_mcp_stderr_log() -> Any:
    """Return a shared append-mode file handle for MCP subprocess stderr.

    Must have a real OS-level fd: asyncio's subprocess machinery wires the
    child's stderr directly to it.
    """
    global _mcp_stderr_log_fh
    with _mcp_stderr_log_lock:
        if _mcp_stderr_log_fh is not None:
            return _mcp_stderr_log_fh
        try:
            from son_of_anton_constants import get_son_of_anton_home
            log_dir = get_son_of_anton_home() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "mcp-stderr.log"
            fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
            fh.fileno()
            _mcp_stderr_log_fh = fh
        except Exception as exc:  # pragma: no cover — best-effort fallback
            logger.debug("Failed to open MCP stderr log, using devnull: %s", exc)
            _mcp_stderr_log_fh = open(os.devnull, "w", encoding="utf-8")
        return _mcp_stderr_log_fh


def _write_stderr_log_header(server_name: str) -> None:
    """Write a session marker so each server's output can be found in the log."""
    fh = _get_mcp_stderr_log()
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fh.write(f"\n===== [{ts}] starting MCP server '{server_name}' =====\n")
        fh.flush()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# MCP SDK -- imported lazily: ``import mcp`` costs ~260ms, which a config with
# no MCP servers should never pay at startup.
# ---------------------------------------------------------------------------

ClientSession: Any = None
_MCP_SDK_LOADED = False
_MCP_SDK_IMPORT_LOCK = threading.Lock()


def _ensure_mcp_sdk() -> None:
    """Bind the ``mcp`` SDK symbols this module uses (idempotent, thread-safe)."""
    global _MCP_SDK_LOADED, LATEST_HANDSHAKE_VERSION
    global ClientSession, StdioServerParameters, stdio_client, streamable_http_client
    global CreateMessageResult, CreateMessageResultWithTools, ErrorData
    global SamplingCapability, SamplingToolsCapability, TextContent, ToolUseContent
    global ElicitResult, PaginatedRequestParams, ToolListChangedNotification
    global PromptListChangedNotification, ResourceListChangedNotification

    if _MCP_SDK_LOADED:
        return
    with _MCP_SDK_IMPORT_LOCK:
        if _MCP_SDK_LOADED:
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.session import LATEST_HANDSHAKE_VERSION
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.types import (
            CreateMessageResult,
            CreateMessageResultWithTools,
            ElicitResult,
            ErrorData,
            PaginatedRequestParams,
            PromptListChangedNotification,
            ResourceListChangedNotification,
            SamplingCapability,
            SamplingToolsCapability,
            TextContent,
            ToolListChangedNotification,
            ToolUseContent,
        )
        _MCP_SDK_LOADED = True


# MCP logging levels (RFC 5424 syslog severities) -> Python logging levels.
_MCP_LOG_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.ERROR,
    "alert": logging.ERROR,
    "emergency": logging.ERROR,
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_TIMEOUT = 300      # seconds for tool calls
_DEFAULT_CONNECT_TIMEOUT = 60    # seconds for initial connection per server
_MAX_RECONNECT_RETRIES = 5
_MAX_INITIAL_CONNECT_RETRIES = 3 # retries for the very first connection attempt
_MAX_BACKOFF_SECONDS = 60
# While parked (reconnect budget exhausted, tools deregistered) the run task
# wakes on this cadence and attempts one revival probe. Without it a parked
# server is unrevivable: its tools are out of the registry, so no tool call
# can ever reach the circuit-breaker half-open probe or _signal_reconnect.
_PARKED_RETRY_INTERVAL = 300     # seconds between parked self-probes
# Jitter applied to reconnect backoff sleeps. Without it, every server that
# lost the same backend retries in lockstep (thundering herd) and log lines
# from N servers land in synchronized bursts.
_BACKOFF_JITTER = 0.2            # +/-20%


def _jittered(seconds: float) -> float:
    """Return ``seconds`` with +/-20% uniform jitter, floored at 0."""
    return max(0.0, seconds * random.uniform(1.0 - _BACKOFF_JITTER,
                                             1.0 + _BACKOFF_JITTER))

# Keepalive cadence for HTTP/SSE sessions. The MCP spec lets a server expire
# idle sessions on any TTL it chooses (Streamable HTTP "Session Management"),
# so a client that wants a session to survive idle periods MUST refresh faster
# than that TTL. The default suits long LB/NAT idle windows (commonly
# 300-600s); servers with short session TTLs (e.g. Unreal Engine's editor MCP,
# ~15s) need a smaller ``keepalive_interval`` in their config or every idle
# tool call lands on a dead session and pays the full reconnect path. The floor
# stops a misconfigured tiny interval from busy-looping the keepalive.
_DEFAULT_KEEPALIVE_INTERVAL = 180  # seconds between liveness pings
_MIN_KEEPALIVE_INTERVAL = 5        # clamp floor for configured intervals

# Final shutdown gives pending MCP-loop tasks one bounded cancellation cycle
# before closing their owning loop. Cooperative parked/reconnect waiters finish
# immediately; cancellation-resistant tasks must not hang process exit.
_MCP_LOOP_DRAIN_TIMEOUT = 3.0

# Environment variables that are safe to pass to stdio subprocesses
_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
})

# Regex for credential patterns to strip from error messages
_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{1,255}"           # GitHub PAT
    r"|sk-[A-Za-z0-9_]{1,255}"           # OpenAI-style key
    r"|Bearer\s+\S+"                      # Bearer token
    r"|token=[^\s&,;\"']{1,255}"         # token=...
    r"|key=[^\s&,;\"']{1,255}"           # key=...
    r"|API_KEY=[^\s&,;\"']{1,255}"       # API_KEY=...
    r"|password=[^\s&,;\"']{1,255}"      # password=...
    r"|secret=[^\s&,;\"']{1,255}"        # secret=...
    r")",
    re.IGNORECASE,
)

# Pre-compiled pattern for ${VAR_NAME} style env-var interpolation.
# Supports any non-} characters in the variable name (hyphens, dots, etc.)
# so providers like MY-VAR or my.var work correctly.
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _env_ref_name(ref: str) -> str:
    """Normalize a ``${...}`` reference body into an env-var name.

    Accepts ``${env:VAR}`` in addition to plain ``${VAR}`` by stripping a
    leading ``env:`` prefix.
    """
    ref = ref.strip()
    if ref.startswith("env:"):
        ref = ref[len("env:"):].strip()
    return ref


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def _build_safe_env(user_env: Optional[dict]) -> dict:
    """Build a filtered environment dict for stdio subprocesses.

    Only passes through safe baseline variables (PATH, HOME, etc.) and XDG_*
    variables from the current process environment, plus any variables
    explicitly specified by the user in the server config.

    This prevents accidentally leaking secrets like API keys, tokens, or
    credentials to MCP server subprocesses.
    """
    env = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV_KEYS or key.startswith("XDG_"):
            env[key] = value
    if user_env:
        env.update(user_env)
    return env


def _sanitize_error(text: str) -> str:
    """Strip credential-like patterns from error text before returning to LLM.

    Replaces tokens, keys, and other secrets with [REDACTED] to prevent
    accidental credential exposure in tool error responses.
    """
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", text)


def _exc_str(exc: BaseException) -> str:
    """Return a non-empty human-readable string for *exc*.

    Some exception classes (e.g. ``anyio.ClosedResourceError``) are raised
    without a message argument, so ``str(exc)`` is ``""``.  This helper
    falls back to ``repr(exc)`` so that error messages shown to the user
    and logged to disk always carry *some* diagnostic information.
    """
    text = str(exc).strip()
    return text if text else repr(exc)


# JSON-RPC "method not found" — the error a server returns when it does not
# implement a requested method (e.g. a tool-capable server that never wired up
# the optional ``ping`` utility).
_JSONRPC_METHOD_NOT_FOUND = -32601

# 2026-07-28 stateless servers answering a legacy ``initialize`` reject it
# with one of these: UnsupportedProtocolVersion (-32022, spec-reserved range)
# or plain method-not-found when the handshake methods are gone entirely.
# Structural codes only — checked via _handshake_rejected_as_modern().
_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION = -32022


def _handshake_rejected_as_modern(exc: BaseException) -> bool:
    """True when a failed ``initialize`` signals a 2026-07-28-only server.

    Mirrors :func:`_is_method_not_found_error`'s structural-then-substring
    shape (never ``isinstance`` on SDK exception types — the SDK wraps
    task-group errors in ``ExceptionGroup`` and symbols drift across
    generations; see references/sdk-exceptiongroup-wrapping.md).
    """
    err = getattr(exc, "error", None)
    code = getattr(err, "code", None) or getattr(exc, "code", None)
    if code in (_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION, _JSONRPC_METHOD_NOT_FOUND):
        return True
    msg = str(exc).lower()
    if not msg:
        return False
    return (
        "unsupported protocol version" in msg
        or str(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION) in msg
        or _is_method_not_found_error(exc)
    )


def _is_method_not_found_error(exc: BaseException) -> bool:
    """Return True if *exc* is a JSON-RPC ``method not found`` (-32601).

    ``ping`` is an *optional* MCP utility (spec: "optional ping mechanism").
    A server that doesn't implement it answers a ping with -32601 rather than
    an empty result. Structurally inspect ``MCPError.error.code`` first, then
    fall back to a substring match for servers that surface the condition as
    a plain message — many phrase it "Unknown method: <name>". Without that
    the ping→list_tools fallback never latches and the keepalive
    reconnect-loops.
    """
    # Structural: mcp.shared.exceptions.MCPError carries ErrorData.code.
    err = getattr(exc, "error", None)
    code = getattr(err, "code", None)
    if code == _JSONRPC_METHOD_NOT_FOUND:
        return True
    msg = str(exc).lower()
    if not msg:
        return False
    return (
        str(_JSONRPC_METHOD_NOT_FOUND) in msg
        or "method not found" in msg
        or "unknown method" in msg
        or "not found: ping" in msg
    )


# ---------------------------------------------------------------------------
# MCP tool description content scanning
# ---------------------------------------------------------------------------

# Patterns that indicate potential prompt injection in MCP tool descriptions.
# These are WARNING-level — we log but don't block, since false positives
# would break legitimate MCP servers.
_MCP_INJECTION_PATTERNS = [
    (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
     "prompt override attempt ('ignore previous instructions')"),
    (re.compile(r"you\s+are\s+now\s+a", re.I),
     "identity override attempt ('you are now a...')"),
    (re.compile(r"your\s+new\s+(task|role|instructions?)\s+(is|are)", re.I),
     "task override attempt"),
    (re.compile(r"system\s*:\s*", re.I),
     "system prompt injection attempt"),
    (re.compile(r"<\s*(system|human|assistant)\s*>", re.I),
     "role tag injection attempt"),
    (re.compile(r"do\s+not\s+(tell|inform|mention|reveal)", re.I),
     "concealment instruction"),
    (re.compile(r"(curl|wget|fetch)\s+https?://", re.I),
     "network command in description"),
    (re.compile(r"base64\.(b64decode|decodebytes)", re.I),
     "base64 decode reference"),
    (re.compile(r"exec\s*\(|eval\s*\(", re.I),
     "code execution reference"),
    (re.compile(r"import\s+(subprocess|os|shutil|socket)", re.I),
     "dangerous import reference"),
]


def _scan_mcp_description(server_name: str, tool_name: str, description: str) -> List[str]:
    """Scan an MCP tool description for prompt injection patterns.

    Returns a list of finding strings (empty = clean).
    """
    findings = []
    if not description:
        return findings
    for pattern, reason in _MCP_INJECTION_PATTERNS:
        if pattern.search(description):
            findings.append(reason)
    if findings:
        logger.warning(
            "MCP server '%s' tool '%s': suspicious description content — %s. "
            "Description: %.200s",
            server_name, tool_name, "; ".join(findings),
            description,
        )
    return findings


def _prepend_path(env: dict, directory: str) -> dict:
    """Prepend *directory* to env PATH if it is not already present."""
    updated = dict(env or {})
    if not directory:
        return updated

    existing = updated.get("PATH", "")
    parts = [part for part in existing.split(os.pathsep) if part]
    if directory not in parts:
        parts = [directory, *parts]
    updated["PATH"] = os.pathsep.join(parts) if parts else directory
    return updated


# Safety cap on nextCursor pagination loops so a misbehaving server that
# returns a cursor forever cannot spin discovery indefinitely. 50 pages at
# the common 50-100 items/page covers thousands of tools/resources/prompts.
_MCP_LIST_MAX_PAGES = 50


async def _paginate_full_list(list_method, items_attr: str, server_name: str):
    """Drain a paginated MCP ``list_*`` call by following ``nextCursor``.

    The MCP spec allows servers to paginate ``tools/list``,
    ``resources/list``, and ``prompts/list`` responses via an opaque
    ``nextCursor`` token. The Python SDK's ``ClientSession.list_*`` methods
    fetch exactly one page per call, so a client that never passes the
    cursor back silently sees only the first page — on a paginated server
    every tool/resource/prompt past page 1 would be invisible to the agent.

    Args:
        list_method: Bound ``session.list_tools`` / ``list_resources`` /
            ``list_prompts`` coroutine function.
        items_attr: Result attribute holding the page's items
            (``"tools"``, ``"resources"``, or ``"prompts"``).
        server_name: For log messages.

    Returns:
        Combined list of items across all pages. Callers must hold the
        server's ``_rpc_lock`` for the duration so pages come from a
        consistent snapshot.
    """
    items: list = []
    cursor = None
    for _ in range(_MCP_LIST_MAX_PAGES):
        if not cursor:
            result = await list_method()
        else:
            result = await list_method(params=PaginatedRequestParams(cursor=cursor))
        items.extend(getattr(result, items_attr, None) or [])
        cursor = getattr(result, "next_cursor", None)
        # Per the MCP spec the cursor is an opaque string; anything else
        # (including mock objects in tests) means "no more pages".
        if not isinstance(cursor, str) or not cursor:
            break
    else:
        logger.warning(
            "MCP server '%s': %s pagination exceeded %d pages; "
            "truncating at %d items",
            server_name, items_attr, _MCP_LIST_MAX_PAGES, len(items),
        )
    return items


def _resolve_stdio_command(command: str, env: dict) -> tuple[str, dict]:
    """Resolve a stdio MCP command against the exact subprocess environment."""
    resolved_command = os.path.expanduser(str(command).strip())
    resolved_env = dict(env or {})

    if os.sep not in resolved_command:
        path_arg = resolved_env["PATH"] if "PATH" in resolved_env else None
        which_hit = shutil.which(resolved_command, path=path_arg)
        if which_hit:
            resolved_command = which_hit

    command_dir = os.path.dirname(resolved_command)
    if command_dir:
        resolved_env = _prepend_path(resolved_env, command_dir)

    return resolved_command, resolved_env


def _wrap_command_with_watchdog(command: str, args: list) -> tuple[str, list]:
    """Wrap a stdio MCP server command in the parent-death watchdog supervisor.

    The watchdog records this process's PID and later detects parent death
    directly through ``getppid()``.
    """
    watchdog_args = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_stdio_watchdog.py"),
        "--ppid", str(os.getpid()),
        "--",
        command,
        *args,
    ]
    return sys.executable, watchdog_args


# ---------------------------------------------------------------------------
# MCP ImageContent block → Son of Anton MEDIA tag
# ---------------------------------------------------------------------------


def _is_reserved_mcp_meta_key(key: str) -> bool:
    """Return True if an MCP ``_meta`` key uses a protocol-reserved prefix.

    Per the MCP spec's key-name rules, a prefix is reserved when a
    ``modelcontextprotocol`` or ``mcp`` label is followed by at least one
    more label (``modelcontextprotocol.io/...``, ``tools.mcp.com/...``).
    A trailing reserved word (``com.example.mcp/...``) is a legitimate
    vendor namespace and passes through.
    """
    slash = key.find("/")
    if slash <= 0:
        return False
    labels = key[:slash].split(".")
    return any(
        label in ("modelcontextprotocol", "mcp") and i < len(labels) - 1
        for i, label in enumerate(labels)
    )


def _strip_reserved_meta_keys(meta) -> "Optional[Dict[str, Any]]":
    """Drop protocol-reserved keys from a tool result's ``_meta`` mapping.

    Returns the filtered dict, or ``None`` when there is nothing
    model-facing left (or the input wasn't a mapping).
    """
    if not isinstance(meta, dict):
        return None
    out = {k: v for k, v in meta.items()
           if isinstance(k, str) and not _is_reserved_mcp_meta_key(k)}
    return out or None


def _mcp_image_extension_for_mime_type(mime_type: str) -> str:
    """Return a reasonable file extension for an MCP image MIME type."""
    import mimetypes
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    return mimetypes.guess_extension(normalized) or ".png"


def _cache_mcp_image_block(block) -> str:
    """Cache an MCP ``ImageContent`` block to the shared image cache and
    return a ``MEDIA:<path>`` tag that Son of Anton gateways know how to render.

    Returns an empty string when *block* is not an image, when the base64
    payload is malformed, or when the cache helper rejects the bytes (e.g.
    non-image MIME masquerading as an image). Errors are logged, not raised:
    a single bad block shouldn't kill the tool result, and the caller will
    fall through to any text blocks that did parse.
    """
    import base64

    data = getattr(block, "data", None)
    mime_type = getattr(block, "mime_type", None)
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if data is None or not normalized_mime.startswith("image/"):
        return ""

    try:
        raw_bytes = base64.b64decode(data)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP image block decode failed (%s): %s", normalized_mime, exc)
        return ""

    try:
        from gateway.platforms.base import cache_image_from_bytes

        image_path = cache_image_from_bytes(
            raw_bytes,
            ext=_mcp_image_extension_for_mime_type(normalized_mime),
        )
    except Exception as exc:
        logger.warning("MCP image block cache failed: %s", exc)
        return ""

    return f"MEDIA:{image_path}"


# ---------------------------------------------------------------------------
# MCP resource blocks (ResourceLink / EmbeddedResource / AudioContent)
# ---------------------------------------------------------------------------

# Hard cap on decoded resource bytes materialized from an MCP tool result.
# Prevents a misbehaving server from filling the cache disk via one block.
_MCP_RESOURCE_MAX_BYTES = 50 * 1024 * 1024

# Base64 expands raw bytes by ~4/3; reject oversized payloads before decoding
# so a multi-GB blob string is never transiently doubled in memory.
_MCP_RESOURCE_MAX_B64_CHARS = _MCP_RESOURCE_MAX_BYTES * 4 // 3 + 4


def _mcp_resource_filename(uri: str, mime_type: str) -> str:
    """Derive a safe display filename for an MCP resource.

    Only the last path segment of the URI is considered, and only as a
    *name hint* — `cache_document_from_bytes` re-sanitizes and prefixes it,
    so remote path components can't influence the cache location.
    """
    import mimetypes
    import re as _re
    from pathlib import Path
    from urllib.parse import urlparse, unquote

    name = ""
    if uri:
        try:
            name = Path(unquote(urlparse(str(uri)).path or "")).name
        except (ValueError, TypeError):
            name = ""
    # Strip control characters (newlines/ANSI escapes from hostile URIs would
    # otherwise land in the filename and the transcript marker) and cap the
    # length, preserving the extension.
    name = _re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 12:
            name = stem[: 150 - len(ext) - 1] + "." + ext
        else:
            name = name[:150]
    if not name or name in {".", ".."}:
        normalized = (mime_type or "").split(";", 1)[0].strip().lower()
        ext = mimetypes.guess_extension(normalized) or ".bin"
        name = f"resource{ext}"
    return name


def _cache_mcp_audio_block(block) -> str:
    """Cache an MCP ``AudioContent`` block and return a ``MEDIA:`` tag.

    Returns an empty string when *block* is not audio or on any failure —
    same fail-open contract as ``_cache_mcp_image_block``.
    """
    import base64

    data = getattr(block, "data", None)
    mime_type = str(getattr(block, "mime_type", None) or "").split(";", 1)[0].strip().lower()
    if data is None or not mime_type.startswith("audio/"):
        return ""
    if len(data) > _MCP_RESOURCE_MAX_B64_CHARS:
        return f"[MCP audio resource too large to cache: ~{len(data) * 3 // 4} bytes]"
    try:
        raw_bytes = base64.b64decode(data)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP audio block decode failed (%s): %s", mime_type, exc)
        return ""
    if len(raw_bytes) > _MCP_RESOURCE_MAX_BYTES:
        return f"[MCP audio resource too large to cache: {len(raw_bytes)} bytes]"
    try:
        from gateway.platforms.base import cache_audio_from_bytes
        import mimetypes

        ext = (
            {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav"}.get(mime_type)
            or mimetypes.guess_extension(mime_type)
            or ".ogg"
        )
        audio_path = cache_audio_from_bytes(raw_bytes, ext=ext)
    except Exception as exc:
        logger.warning("MCP audio block cache failed: %s", exc)
        return ""
    return f"MEDIA:{audio_path}"


def _render_mcp_resource_block(block, server_name: str = "") -> str:
    """Render an MCP ``ResourceLink`` or ``EmbeddedResource`` block as text.

    - ``EmbeddedResource`` with text contents → the text itself.
    - ``EmbeddedResource`` with blob contents → bytes are decoded (size-capped)
      and materialized into the Son of Anton document cache; returns a marker with
      the local path so file/terminal tools can consume it.
    - ``ResourceLink`` → the URI plus a pointer at the server's read_resource
      tool. No network fetch happens here; the link is only readable through
      the originating MCP session.

    Returns an empty string for non-resource blocks. Failures are logged and
    reported inline rather than silently dropping the block.
    """
    block_type = getattr(block, "type", "")

    if block_type == "resource_link" or (
        hasattr(block, "uri") and not hasattr(block, "resource") and block_type != "text"
    ):
        uri = getattr(block, "uri", None)
        if not uri:
            return ""
        name = getattr(block, "name", "") or ""
        mime = getattr(block, "mime_type", "") or ""
        details = f"uri={uri}"
        if name:
            details += f", name={name}"
        if mime:
            details += f", mimeType={mime}"
        reader = (
            mcp_prefixed_tool_name(server_name, "read_resource")
            if server_name
            else "the MCP server's read_resource tool"
        )
        return f"[MCP resource link: {details} — fetch it with {reader}]"

    resource = getattr(block, "resource", None)
    if resource is None:
        return ""

    text = getattr(resource, "text", None)
    if text is not None:
        return strip_unicode_tags(str(text))

    blob = getattr(resource, "blob", None)
    if blob is None:
        return ""

    import base64

    uri = str(getattr(resource, "uri", "") or "")
    mime = str(getattr(resource, "mime_type", "") or "")
    if len(blob) > _MCP_RESOURCE_MAX_B64_CHARS:
        return f"[MCP embedded resource too large to cache: ~{len(blob) * 3 // 4} bytes, uri={uri}]"
    try:
        raw_bytes = base64.b64decode(blob)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP embedded resource decode failed (%s): %s", mime or uri, exc)
        return f"[MCP embedded resource could not be decoded: {mime or uri}]"
    if len(raw_bytes) > _MCP_RESOURCE_MAX_BYTES:
        return f"[MCP embedded resource too large to cache: {len(raw_bytes)} bytes, uri={uri}]"
    try:
        from gateway.platforms.base import cache_document_from_bytes

        path = cache_document_from_bytes(raw_bytes, _mcp_resource_filename(uri, mime))
    except Exception as exc:
        logger.warning("MCP embedded resource cache failed: %s", exc)
        return f"[MCP embedded resource could not be cached: {mime or uri}]"
    detail = mime or "unknown type"
    return f"[MCP resource saved to {path} ({detail}, {len(raw_bytes)} bytes) — read it with read_file or terminal tools]"


# ---------------------------------------------------------------------------
# Remote MCP URL validation
# ---------------------------------------------------------------------------


class InvalidMcpUrlError(ValueError):
    """Raised when a remote MCP server's ``url`` cannot be parsed as http(s)://.

    Validated once at startup so we fail fast with a clear message instead of
    burning through the reconnect-backoff loop on every attempt.
    """


class NonMcpEndpointError(ConnectionError):
    """Raised when an HTTP MCP URL serves a non-MCP response.

    A genuine MCP Streamable-HTTP endpoint answers with ``application/json``
    or ``text/event-stream``.  Anything else on a 2xx response (typically
    ``text/html`` from a web-app root) means the configured ``url`` points at
    the wrong place.  This is non-retryable: every attempt returns the same
    page, so the reconnect-backoff loop is skipped and the server is reported
    failed immediately with an actionable message.

    Subclasses :class:`ConnectionError` so callers that only catch the broad
    class still treat it as a connection problem.
    """


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Extract the root-cause exception from anyio TaskGroup wrappers.

    The MCP SDK uses anyio task groups, which wrap errors in
    ``BaseExceptionGroup`` / ``ExceptionGroup``. Their ``str()`` is opaque —
    "unhandled errors in a TaskGroup (1 sub-exception)" — so log sites must
    unwrap to surface the real cause (e.g. ``BrokenPipeError`` on a dead
    stdio pipe, "401 Unauthorized" on an auth failure).

    Adapted from :func:`son_of_anton_cli.mcp_config._unwrap_exception_group` with
    two extra behaviours needed on the runtime path:

    - **Fatal leaves re-raise.** A ``KeyboardInterrupt`` / ``SystemExit``
      anywhere in the (possibly nested) group must propagate to the
      interpreter, never be flattened into a loggable error.
    - **Prefer non-cancellation leaves.** When a group carries both a real
      error and the ``CancelledError``s that anyio cancellation sprays across
      sibling tasks, the real error is the root cause worth logging.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        fatal, _rest = exc.split((KeyboardInterrupt, SystemExit))
        if fatal is not None:
            # Surface the fatal signal itself, not the wrapper.
            leaf: BaseException = fatal
            while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
                leaf = leaf.exceptions[0]
            raise leaf
        # Prefer a non-cancellation leaf when one exists: cancellation
        # noise from sibling tasks should not mask the real error.
        chosen = exc.exceptions[0]
        for sub in exc.exceptions:
            if not _contains_only_cancellation(sub):
                chosen = sub
                break
        exc = chosen
    return exc


def _contains_only_cancellation(exc: BaseException) -> bool:
    """True if ``exc`` is (or a group containing only) CancelledError."""
    if isinstance(exc, BaseExceptionGroup):
        return all(_contains_only_cancellation(sub) for sub in exc.exceptions)
    return isinstance(exc, asyncio.CancelledError)


def _classify_mcp_failure(exc: BaseException) -> str:
    """Classify an MCP connection failure as ``'permanent'`` or ``'transient'``.

    Permanent failures are deterministic — every retry hits the same wall, so
    burning the retry ladder (and log lines) on them is pure noise; ``run()``
    parks them immediately:

    - auth failures (401/403) — need new credentials, not a retry;
    - :class:`NonMcpEndpointError` — the URL serves a web page, not MCP;
    - :class:`InvalidMcpUrlError` — unusable config;
    - ``FileNotFoundError`` / ``ENOENT`` — the stdio command doesn't exist.

    Everything else (network blips, EOF, ``ClosedResourceError``, transport
    TaskGroup drops, timeouts) is transient and keeps the normal
    retry-with-backoff ladder.
    """
    root = _unwrap_exception_group(exc)
    if isinstance(root, (NonMcpEndpointError, InvalidMcpUrlError)):
        return "permanent"
    # Stdio command missing: FileNotFoundError, or an OSError carrying ENOENT.
    if isinstance(root, FileNotFoundError):
        return "permanent"
    if isinstance(root, OSError) and getattr(root, "errno", None) == errno.ENOENT:
        return "permanent"
    # httpx.HTTPStatusError with 401/403 (either httpx flavour).
    status = getattr(getattr(root, "response", None), "status_code", None)
    if status in (401, 403):
        return "permanent"
    return "transient"


def _validate_remote_mcp_url(server_name: str, url: Any) -> str:
    """Return the URL as a string if it's a valid http(s) remote MCP URL.

    Raises :class:`InvalidMcpUrlError` otherwise with a message naming the
    offending server, so users can spot the bad entry in their config.

    Accepts:
    - ``http://host`` / ``https://host`` with optional port, path, query
    - IPv4, IPv6 (bracketed), DNS hostnames

    Rejects:
    - Non-string values (``None``, dicts, ints)
    - Missing scheme (``example.com/mcp``)
    - Non-http(s) schemes (``file://``, ``ws://``, ``stdio:`` — stdio servers
      use the ``command`` key, not ``url``)
    - Empty host (``http://``, ``https:///path``)
    """
    if not isinstance(url, str):
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': expected a string, got "
            f"{type(url).__name__}"
        )
    stripped = url.strip()
    if not stripped:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': empty url"
        )
    try:
        parsed = urlparse(stripped)
    except Exception as exc:  # urlparse is very permissive — belt and braces
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': {stripped!r} ({exc})"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': scheme must be http or "
            f"https, got {parsed.scheme!r} ({stripped!r})"
        )
    if not parsed.netloc:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': missing host ({stripped!r})"
        )
    # ``urlparse`` accepts ``http://:8080`` (empty host, explicit port).
    # Reject that — we need a real host.
    if not parsed.hostname:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': missing hostname "
            f"({stripped!r})"
        )
    return stripped


def _make_redirect_header_stripper(original_url):
    """Build an httpx response hook that strips ``Authorization`` when a
    redirect leaves the original origin."""

    async def _strip_on_cross_origin_redirect(response):
        if response.is_redirect and response.next_request:
            target = response.next_request.url
            if (target.scheme, target.host, target.port) != (
                original_url.scheme, original_url.host, original_url.port,
            ):
                response.next_request.headers.pop("authorization", None)
                response.next_request.headers.pop("Authorization", None)

    return _strip_on_cross_origin_redirect


def _format_connect_error(exc: BaseException) -> str:
    """Render nested MCP connection errors into an actionable short message."""

    def _find_missing(current: BaseException) -> Optional[str]:
        nested = getattr(current, "exceptions", None)
        if nested:
            for child in nested:
                missing = _find_missing(child)
                if missing:
                    return missing
            return None
        if isinstance(current, FileNotFoundError):
            if getattr(current, "filename", None):
                return str(current.filename)
            match = re.search(r"No such file or directory: '([^']+)'", str(current))
            if match:
                return match.group(1)
        for attr in ("__cause__", "__context__"):
            nested_exc = getattr(current, attr, None)
            if isinstance(nested_exc, BaseException):
                missing = _find_missing(nested_exc)
                if missing:
                    return missing
        return None

    def _flatten_messages(current: BaseException) -> List[str]:
        nested = getattr(current, "exceptions", None)
        if nested:
            flattened: List[str] = []
            for child in nested:
                flattened.extend(_flatten_messages(child))
            return flattened
        messages = []
        text = str(current).strip()
        if text:
            messages.append(text)
        for attr in ("__cause__", "__context__"):
            nested_exc = getattr(current, attr, None)
            if isinstance(nested_exc, BaseException):
                messages.extend(_flatten_messages(nested_exc))
        return messages or [current.__class__.__name__]

    missing = _find_missing(exc)
    if missing:
        return _sanitize_error(f"missing executable '{missing}'")

    deduped: List[str] = []
    for item in _flatten_messages(exc):
        if item not in deduped:
            deduped.append(item)
    return _sanitize_error("; ".join(deduped[:3]))


# ---------------------------------------------------------------------------
# Sampling -- server-initiated LLM requests (MCP sampling/createMessage)
# ---------------------------------------------------------------------------

_MISSING = object()


class SamplingHandler:
    """Handles sampling/createMessage requests for a single MCP server.

    MCP 2026-07-28 deprecates Sampling (SEP-2577); handshake-era servers still
    issue sampling/createMessage, so it stays — but do not grow it.

    Each MCPServerTask that has sampling enabled creates one SamplingHandler.
    The handler is callable and passed directly to ``ClientSession`` as
    the ``sampling_callback``.  All state (rate-limit timestamps, metrics,
    tool-loop counters) lives on the instance -- no module-level globals.

    The callback is async and runs on the MCP background event loop.  The
    sync LLM call is offloaded to a thread via ``asyncio.to_thread()`` so
    it doesn't block the event loop.
    """

    _STOP_REASON_MAP = {"stop": "endTurn", "length": "maxTokens", "tool_calls": "toolUse"}

    max_rpm = 10            # requests per minute
    timeout = 30.0          # LLM call timeout (seconds)
    max_tokens_cap = 4096   # max tokens per request
    max_tool_rounds = 5     # consecutive tool-use responses before refusing

    def __init__(self, server_name: str):
        self.server_name = server_name
        # Per-instance state
        self._rate_timestamps: List[float] = []
        self._tool_loop_count = 0
        self.metrics = {"requests": 0, "errors": 0, "tokens_used": 0, "tool_use_count": 0}

    # -- Rate limiting -------------------------------------------------------

    def _check_rate_limit(self) -> bool:
        """Sliding-window rate limiter.  Returns True if request is allowed."""
        now = time.time()
        window = now - 60
        self._rate_timestamps[:] = [t for t in self._rate_timestamps if t > window]
        if len(self._rate_timestamps) >= self.max_rpm:
            return False
        self._rate_timestamps.append(now)
        return True

    # -- Model resolution ----------------------------------------------------

    def _resolve_model(self, preferences) -> Optional[str]:
        """Server hint > None (use default)."""
        if preferences and hasattr(preferences, "hints") and preferences.hints:
            for hint in preferences.hints:
                if hasattr(hint, "name") and hint.name:
                    return hint.name
        return None

    # -- Message conversion --------------------------------------------------

    @staticmethod
    def _extract_tool_result_text(block) -> str:
        """Extract text from a ToolResultContent block."""
        if not hasattr(block, "content") or block.content is None:
            return ""
        items = block.content if isinstance(block.content, list) else [block.content]
        return "\n".join(item.text for item in items if hasattr(item, "text"))

    def _convert_messages(self, params) -> List[dict]:
        """Convert MCP SamplingMessages to OpenAI format.

        Uses ``msg.content_as_list`` (SDK helper) so single-block and
        list-of-blocks are handled uniformly. A tool-use id is the
        discriminator for a tool *result* block.
        """
        def _tool_use_id(block):
            return getattr(block, "tool_use_id", _MISSING)

        def _is_tool_use(block):
            return hasattr(block, "name") and hasattr(block, "input")

        messages: List[dict] = []
        for msg in params.messages:
            blocks = msg.content_as_list

            # Separate blocks by kind.
            tool_results = [b for b in blocks if _tool_use_id(b) is not _MISSING]
            tool_uses = [
                b for b in blocks
                if _is_tool_use(b) and _tool_use_id(b) is _MISSING
            ]
            content_blocks = [
                b for b in blocks
                if _tool_use_id(b) is _MISSING and not _is_tool_use(b)
            ]

            # Emit tool result messages (role: tool)
            for tr in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": _tool_use_id(tr),
                    "content": self._extract_tool_result_text(tr),
                })

            # Emit assistant tool_calls message
            if tool_uses:
                tc_list = []
                for tu in tool_uses:
                    tc_list.append({
                        "id": getattr(tu, "id", f"call_{len(tc_list)}"),
                        "type": "function",
                        "function": {
                            "name": tu.name,
                            "arguments": json.dumps(tu.input, ensure_ascii=False) if isinstance(tu.input, dict) else str(tu.input),
                        },
                    })
                msg_dict: dict = {"role": msg.role, "tool_calls": tc_list}
                # Include any accompanying text
                text_parts = [b.text for b in content_blocks if hasattr(b, "text")]
                if text_parts:
                    msg_dict["content"] = "\n".join(text_parts)
                messages.append(msg_dict)
            elif content_blocks:
                # Pure text/image content
                if len(content_blocks) == 1 and hasattr(content_blocks[0], "text"):
                    messages.append({"role": msg.role, "content": content_blocks[0].text})
                else:
                    parts = []
                    for block in content_blocks:
                        block_mime = getattr(block, "mime_type", _MISSING)
                        if hasattr(block, "text"):
                            parts.append({"type": "text", "text": block.text})
                        elif hasattr(block, "data") and block_mime is not _MISSING:
                            parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{block_mime};base64,{block.data}"},
                            })
                        else:
                            logger.warning(
                                "Unsupported sampling content block type: %s (skipped)",
                                type(block).__name__,
                            )
                    if parts:
                        messages.append({"role": msg.role, "content": parts})

        return messages

    # -- Error helper --------------------------------------------------------

    @staticmethod
    def _error(message: str, code: int = -1):
        return ErrorData(code=code, message=message)

    # -- Response building ---------------------------------------------------

    def _build_tool_use_result(self, choice, response):
        """Build a CreateMessageResultWithTools from an LLM tool_calls response."""
        self.metrics["tool_use_count"] += 1

        # Tool loop governance
        if self.max_tool_rounds == 0:
            self._tool_loop_count = 0
            return self._error(
                f"Tool loops disabled for server '{self.server_name}' (max_tool_rounds=0)"
            )

        self._tool_loop_count += 1
        if self._tool_loop_count > self.max_tool_rounds:
            self._tool_loop_count = 0
            return self._error(
                f"Tool loop limit exceeded for server '{self.server_name}' "
                f"(max {self.max_tool_rounds} rounds)"
            )

        content_blocks = []
        for tc in choice.message.tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "MCP server '%s': malformed tool_calls arguments "
                        "from LLM (wrapping as raw): %.100s",
                        self.server_name, args,
                    )
                    parsed = {"_raw": args}
            else:
                parsed = args if isinstance(args, dict) else {"_raw": str(args)}

            content_blocks.append(ToolUseContent(
                type="tool_use",
                id=tc.id,
                name=tc.function.name,
                input=parsed,
            ))

        logger.info(
            "MCP server '%s' sampling response: model=%s, tokens=%s, tool_calls=%d",
            self.server_name, response.model,
            getattr(getattr(response, "usage", None), "total_tokens", "?"),
            len(content_blocks),
        )

        return CreateMessageResultWithTools(
            role="assistant",
            content=content_blocks,
            model=response.model,
            stopReason="toolUse",
        )

    def _build_text_result(self, choice, response):
        """Build a CreateMessageResult from a normal text response."""
        self._tool_loop_count = 0  # reset on text response
        response_text = choice.message.content or ""

        logger.info(
            "MCP server '%s' sampling response: model=%s, tokens=%s",
            self.server_name, response.model,
            getattr(getattr(response, "usage", None), "total_tokens", "?"),
        )

        return CreateMessageResult(
            role="assistant",
            content=TextContent(type="text", text=_sanitize_error(response_text)),
            model=response.model,
            stopReason=self._STOP_REASON_MAP.get(choice.finish_reason, "endTurn"),
        )

    # -- Session kwargs helper -----------------------------------------------

    def session_kwargs(self) -> dict:
        """Return kwargs to pass to ClientSession for sampling support."""
        return {
            "sampling_callback": self,
            "sampling_capabilities": SamplingCapability(
                tools=SamplingToolsCapability(),
            ),
        }

    # -- Main callback -------------------------------------------------------

    async def __call__(self, context, params):
        """Sampling callback invoked by the MCP SDK.

        Conforms to ``SamplingFnT`` protocol.  Returns
        ``CreateMessageResult``, ``CreateMessageResultWithTools``, or
        ``ErrorData``.
        """
        # Rate limit
        if not self._check_rate_limit():
            logger.warning(
                "MCP server '%s' sampling rate limit exceeded (%d/min)",
                self.server_name, self.max_rpm,
            )
            self.metrics["errors"] += 1
            return self._error(
                f"Sampling rate limit exceeded for server '{self.server_name}' "
                f"({self.max_rpm} requests/minute)"
            )

        # Resolve model
        model = self._resolve_model(
            getattr(params, "model_preferences", None)
        )

        from agent.auxiliary_client import call_llm

        resolved_model = model or ""

        # Convert messages
        messages = self._convert_messages(params)
        system_prompt = getattr(params, "system_prompt", None)
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})

        # Build LLM call kwargs
        max_tokens = min(
            getattr(params, "max_tokens", self.max_tokens_cap),
            self.max_tokens_cap,
        )
        call_temperature = None
        if hasattr(params, "temperature") and params.temperature is not None:
            call_temperature = params.temperature

        # Forward server-provided tools
        call_tools = None
        server_tools = getattr(params, "tools", None)
        if server_tools:
            call_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": getattr(t, "name", ""),
                        "description": getattr(t, "description", "") or "",
                        "parameters": _normalize_mcp_input_schema(
                            getattr(t, "input_schema", None)
                        ),
                    },
                }
                for t in server_tools
            ]

        logger.info(
            "MCP server '%s' sampling request: model=%s, max_tokens=%d, messages=%d",
            self.server_name, resolved_model, max_tokens, len(messages),
        )

        # Offload sync LLM call to thread (non-blocking)
        def _sync_call():
            return call_llm(
                task="mcp",
                model=resolved_model or None,
                messages=messages,
                temperature=call_temperature,
                max_tokens=max_tokens,
                tools=call_tools,
                timeout=self.timeout,
            )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(_sync_call), timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            self.metrics["errors"] += 1
            return self._error(
                f"Sampling LLM call timed out after {self.timeout}s "
                f"for server '{self.server_name}'"
            )
        except Exception as exc:
            self.metrics["errors"] += 1
            return self._error(
                f"Sampling LLM call failed: {_sanitize_error(_exc_str(exc))}"
            )

        # Guard against empty choices (content filtering, provider errors)
        if not getattr(response, "choices", None):
            self.metrics["errors"] += 1
            return self._error(
                f"LLM returned empty response (no choices) for server "
                f"'{self.server_name}'"
            )

        # Track metrics
        choice = response.choices[0]
        self.metrics["requests"] += 1
        total_tokens = getattr(getattr(response, "usage", None), "total_tokens", 0)
        if isinstance(total_tokens, int):
            self.metrics["tokens_used"] += total_tokens

        # Dispatch based on response type
        if (
            choice.finish_reason == "tool_calls"
            and hasattr(choice.message, "tool_calls")
            and choice.message.tool_calls
        ):
            return self._build_tool_use_result(choice, response)

        return self._build_text_result(choice, response)


# ---------------------------------------------------------------------------
# Elicitation handler
# ---------------------------------------------------------------------------

def _format_elicitation_schema_summary(schema: dict, server_name: str) -> str:
    """Render a JSON-schema-ish requested_schema to a human-readable field list.

    Elicitation schemas are restricted to a flat object with named top-level
    properties. We surface field names, types, and descriptions so the user
    can tell what the server is asking for before approving.
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return f"Approval requested by MCP server '{server_name}'."

    lines = [f"Fields requested by MCP server '{server_name}':"]
    for field_name, field_spec in props.items():
        field_type = ""
        field_desc = ""
        if isinstance(field_spec, dict):
            field_type = str(field_spec.get("type", "") or "")
            field_desc = str(field_spec.get("description", "") or "")
        suffix = f" ({field_type})" if field_type else ""
        if field_desc:
            lines.append(f"  - {field_name}{suffix}: {field_desc}")
        else:
            lines.append(f"  - {field_name}{suffix}")
    return "\n".join(lines)


class ElicitationHandler:
    """Handles ``elicitation/create`` requests for a single MCP server.

    Each ``MCPServerTask`` that has elicitation enabled creates one handler.
    The handler is callable and passed directly to ``ClientSession`` as the
    ``elicitation_callback``.

    Elicitation lets a server ask the client to collect structured input from
    the user mid-tool-call (e.g. payment authorization, OAuth confirmation).
    Form-mode elicitations are routed through the approval system, which
    surfaces the prompt on whichever surface the active session uses (TUI or
    Signal). URL-mode elicitations are declined as unsupported.

    Failure modes are fail-closed: any timeout, exception, or unexpected
    state returns ``decline``/``cancel`` rather than silently accepting.
    The server treats this as the user not approving.
    """

    # Outer cap for the approval await. ``prompt_dangerous_approval`` runs
    # its own input() timeout via the approval-config value; this is an
    # asyncio-side safety net so the MCP event loop never blocks
    # indefinitely if the inner timeout machinery is bypassed.
    _OUTER_TIMEOUT_GRACE_SECONDS = 5

    # Per-elicitation timeout. 5 min mirrors the gateway approval default so
    # a user answering from Signal has time to respond.
    timeout = 300.0

    def __init__(self, server_name: str, owner: Optional["MCPServerTask"] = None):
        self.server_name = server_name
        # Back-reference to the MCPServerTask so we can read the agent's
        # captured contextvars snapshot at elicitation time. Optional so
        # the handler stays unit-testable in isolation.
        self.owner = owner
        self.metrics = {
            "requests": 0,
            "accepted": 0,
            "declined": 0,
            "errors": 0,
        }

    def session_kwargs(self) -> dict:
        """Return kwargs to pass to ClientSession for elicitation support."""
        return {"elicitation_callback": self}

    async def __call__(self, context, params):
        """Elicitation callback invoked by the MCP SDK.

        Conforms to ``ElicitationFnT`` protocol. Returns ``ElicitResult``
        or ``ErrorData``.
        """
        self.metrics["requests"] += 1

        # URL-mode elicitations point the user to an external URL for
        # sensitive out-of-band flows (OAuth, payment processing). Honouring
        # them requires opening a browser to that URL and waiting for the
        # server's notifications/elicitation/complete -- out of scope for
        # the initial implementation. Decline cleanly so the server does
        # not hang.
        mode = getattr(params, "mode", "form")
        if mode == "url":
            logger.info(
                "MCP server '%s' requested URL-mode elicitation; "
                "declining (URL-mode elicitation not implemented)",
                self.server_name,
            )
            self.metrics["declined"] += 1
            return ElicitResult(action="decline")

        message = getattr(params, "message", "") or (
            f"MCP server '{self.server_name}' is requesting your approval"
        )
        schema = getattr(params, "requested_schema", None) or {}
        description = _format_elicitation_schema_summary(schema, self.server_name)

        logger.info(
            "MCP server '%s' elicitation request: %s",
            self.server_name, _sanitize_error(message)[:200],
        )

        from tools.approval import request_elicitation_consent

        # Offload the sync consent flow to a worker thread. Running it
        # inline would freeze the MCP background event loop, blocking every
        # other RPC on this session. request_elicitation_consent() routes
        # itself to the right surface (gateway notify_cb, or
        # prompt_dangerous_approval for the TUI) and normalizes the answer
        # to one of accept / decline / cancel.
        #
        # The recv-loop task that fires this callback does NOT inherit
        # the agent's contextvars (SON_OF_ANTON_SESSION_PLATFORM etc.). When
        # the MCP tool wrapper captured the agent's context onto
        # owner._pending_call_context we replay it here via
        # contextvars.Context.run so the gateway-platform detection in
        # request_elicitation_consent picks up the right session.
        captured = getattr(self.owner, "_pending_call_context", None) if self.owner else None

        def _invoke_consent() -> str:
            if captured is None:
                return request_elicitation_consent(
                    message,
                    description,
                    timeout_seconds=int(self.timeout),
                    surface=f"mcp-elicitation/{self.server_name}",
                )
            # Context.run can only execute a context once — copy to allow
            # multiple elicitations within a single tool call.
            return captured.copy().run(
                request_elicitation_consent,
                message,
                description,
                timeout_seconds=int(self.timeout),
                surface=f"mcp-elicitation/{self.server_name}",
            )

        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(_invoke_consent),
                timeout=self.timeout + self._OUTER_TIMEOUT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "MCP server '%s' elicitation timed out after %ds",
                self.server_name, int(self.timeout),
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="cancel")
        except Exception as exc:
            logger.error(
                "MCP server '%s' elicitation failed: %s",
                self.server_name, exc, exc_info=True,
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="decline")

        if answer == "accept":
            self.metrics["accepted"] += 1
            return ElicitResult(action="accept", content={})
        if answer == "cancel":
            self.metrics["errors"] += 1
            return ElicitResult(action="cancel")
        self.metrics["declined"] += 1
        return ElicitResult(action="decline")


# ---------------------------------------------------------------------------
# Server task -- each MCP server lives in one long-lived asyncio Task
# ---------------------------------------------------------------------------

class MCPServerTask:
    """Manages a single MCP server connection in a dedicated asyncio Task.

    The entire connection lifecycle (connect, discover, serve, disconnect)
    runs inside one asyncio Task so that anyio cancel-scopes created by
    the transport client are entered and exited in the same Task context.

    Supports both stdio and HTTP/StreamableHTTP transports.
    """

    __slots__ = (
        "name", "session", "tool_timeout",
        "_task", "_ready", "_shutdown_event", "_reconnect_event",
        "_tools", "_error", "_config",
        "_sampling", "_elicitation",
        "_registered_tool_names", "_refresh_lock",
        "_rpc_lock", "_pending_refresh_tasks",
        "_pending_call_context",
        "initialize_result", "_ping_unsupported",
        "_reconnect_retries", "_session_proven", "_was_parked",
    )

    def __init__(self, name: str):
        self.name = name
        self.session: Optional[Any] = None
        self.tool_timeout: float = _DEFAULT_TOOL_TIMEOUT
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        # Set by tool handlers on a session-expired error (or a parked/dead
        # transport). When set, _run_http / _run_stdio exit their async-with
        # blocks cleanly (no exception), and the outer run() loop re-enters
        # the transport so the MCP session is rebuilt.
        self._reconnect_event = asyncio.Event()
        self._tools: list = []
        self._error: Optional[Exception] = None
        self._config: dict = {}
        self._sampling: Optional[SamplingHandler] = None
        self._elicitation: Optional[ElicitationHandler] = None
        self._registered_tool_names: list[str] = []
        self._reconnect_retries: int = 0
        # Rapid-drop budget: a freshly (re)established session is
        # UNPROVEN until it demonstrates real health — it survived at least
        # one full keepalive interval (keepalive success path) or served at
        # least one successful tool call. Only a proven session clears the
        # reconnect budget; a transport that flaps right after the handshake
        # keeps getting charged and still reaches the park instead of
        # hot-cycling respawns forever.
        self._session_proven: bool = False
        # True while parked (reconnect budget exhausted) or after a park,
        # until the session proves healthy again — used to log the
        # parked→revived transition exactly once.
        self._was_parked: bool = False
        self._refresh_lock = asyncio.Lock()
        # MCP stdio sessions are a single JSON-RPC stream. Some servers emit
        # list_changed notifications during startup; if the notification
        # handler calls list_tools while a normal tool call is in flight, the
        # stream can wedge and the user-visible tool call times out. Serialize
        # client-initiated RPCs per server. The lock is also applied to HTTP
        # transports for conservative per-server ordering.
        self._rpc_lock = asyncio.Lock()
        self._pending_refresh_tasks: set[asyncio.Task] = set()
        # contextvars snapshot of the agent task that's currently in
        # session.call_tool(). The MCP recv loop dispatches incoming
        # elicitation/create requests on a SEPARATE asyncio task whose
        # context doesn't inherit SON_OF_ANTON_SESSION_PLATFORM, so the
        # elicitation handler has no way to detect the gateway session
        # that triggered the call. Capturing the agent's context here
        # and replaying it inside the elicitation callback restores
        # gateway-platform attribution and routes the approval prompt
        # to the right surface.
        self._pending_call_context: Optional[contextvars.Context] = None
        # Captures the ``InitializeResult`` returned by
        # ``await session.initialize()`` so downstream code can inspect the
        # server's real advertised capabilities (``.capabilities.resources``,
        # ``.capabilities.prompts``) instead of assuming every ``ClientSession``
        # method attribute corresponds to a supported server method.
        self.initialize_result: Optional[Any] = None
        # Set True the first time a keepalive ``ping`` returns JSON-RPC
        # -32601 (method not found): the server is tool-capable but doesn't
        # implement the optional ``ping`` utility. Subsequent keepalives fall
        # back to ``list_tools`` (the pre-ping probe) so we neither spam pings
        # nor reconnect-loop. Reset on each fresh transport connection.
        self._ping_unsupported: bool = False

    def _is_http(self) -> bool:
        """Check if this server uses HTTP transport."""
        return "url" in self._config

    def _advertises_tools(self) -> bool:
        """Whether the server advertises the ``tools`` capability.

        Per the MCP spec, ``InitializeResult.capabilities.tools`` is non-None
        iff the server implements the ``tools/*`` request family. Prompt-only
        or resource-only servers omit it, and calling ``tools/list`` against
        them raises ``MCPError(-32601 Method not found)``, which would kill the
        connection during discovery and fail every keepalive.

        Returns True when no capability info was captured.
        """
        init_result = self.initialize_result
        caps = getattr(init_result, "capabilities", None) if init_result is not None else None
        if caps is None:
            return True
        return getattr(caps, "tools", None) is not None

    async def _negotiate_session(self, session, connect_timeout: float):
        """Negotiate the protocol era with the server and return its result.

        MCP 2026-07-28 replaced the ``initialize``/``initialized`` handshake
        with a stateless core: every request is self-describing and clients
        MAY probe ``server/discover`` up front (SEP-2575). The SDK exposes
        both paths on ``ClientSession`` (``initialize()`` / ``discover()``).

        Tries the legacy handshake FIRST and falls back to
        ``server/discover`` when the server signals it is modern-only
        (``UnsupportedProtocolVersion`` -32022, or ``initialize`` missing
        -32601) — the reverse of the SDK's own discover-first mode, so
        handshake-era servers pay zero extra round-trips.

        Both result types expose ``.capabilities``, so downstream gates
        (``_advertises_tools``, ``_select_utility_schemas``, the config
        probe) work unchanged on either.
        """
        try:
            return await asyncio.wait_for(
                session.initialize(), timeout=connect_timeout
            )
        except asyncio.TimeoutError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _handshake_rejected_as_modern(exc):
                raise
            logger.info(
                "MCP server '%s': legacy handshake rejected (%s) — "
                "retrying via server/discover (2026-07-28 stateless server)",
                self.name, exc,
            )
            return await asyncio.wait_for(
                session.discover(), timeout=connect_timeout
            )

    # ----- Dynamic tool discovery (notifications/tools/list_changed) -----

    async def _refresh_tools_task(self):
        """Run a dynamic tool refresh and log failures from background tasks."""
        try:
            await self._refresh_tools()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP server '%s': dynamic tool refresh failed", self.name)

    def _schedule_tools_refresh(self) -> asyncio.Task:
        """Schedule a background tool refresh and keep it strongly referenced."""
        task = asyncio.create_task(self._refresh_tools_task())
        self._pending_refresh_tasks.add(task)
        task.add_done_callback(self._pending_refresh_tasks.discard)
        return task

    def _make_logging_callback(self):
        """Build a ``logging_callback`` for ``ClientSession``.

        Routes MCP ``notifications/message`` log notifications from the
        server into agent.log, tagged with the server name.  Without this, the
        SDK's default callback silently discards them.
        """
        async def _on_log(params):
            try:
                level = _MCP_LOG_LEVEL_MAP.get(
                    str(getattr(params, "level", "info")).lower(), logging.INFO,
                )
                data = getattr(params, "data", None)
                if not isinstance(data, str):
                    try:
                        data = json.dumps(data, ensure_ascii=False, default=str)
                    except (TypeError, ValueError):
                        data = str(data)
                # Cap pathological payloads so a chatty/broken server can't
                # flood agent.log with megabyte lines.
                if len(data) > 2000:
                    data = data[:2000] + "... [truncated]"
                logger_name = getattr(params, "logger", None)
                origin = f"{self.name}/{logger_name}" if logger_name else self.name
                logger.log(level, "MCP server log [%s]: %s", origin, data)
            except Exception:
                logger.debug(
                    "Failed to handle MCP log notification from '%s'",
                    self.name, exc_info=True,
                )
        return _on_log

    def _make_message_handler(self):
        """Build a ``message_handler`` callback for ``ClientSession``.

        Dispatches on notification type.  Only ``ToolListChangedNotification``
        triggers a refresh; prompt and resource change notifications are
        logged as stubs for future work.
        """
        async def _handler(message):
            try:
                if isinstance(message, Exception):
                    logger.debug("MCP message handler (%s): exception: %s", self.name, message)
                    return
                match message:
                    case ToolListChangedNotification():
                        logger.info(
                            "MCP server '%s': received tools/list_changed notification",
                            self.name,
                        )
                        # Some servers emit tools/list_changed immediately
                        # after initialize, while the client may already be
                        # executing another request. Refreshing synchronously
                        # inside the SDK notification handler can race with
                        # that request and wedge the stdio JSON-RPC stream.
                        # Do the refresh in a separate task and return.
                        self._schedule_tools_refresh()
                        await asyncio.sleep(0)
                    case PromptListChangedNotification():
                        logger.debug("MCP server '%s': prompts/list_changed (ignored)", self.name)
                    case ResourceListChangedNotification():
                        logger.debug("MCP server '%s': resources/list_changed (ignored)", self.name)
            except Exception:
                logger.exception("Error in MCP message handler for '%s'", self.name)
        return _handler

    async def _refresh_tools(self):
        """Re-fetch tools from the server and update the registry.

        Called when the server sends ``notifications/tools/list_changed``.
        The lock prevents overlapping refreshes from rapid-fire notifications.
        After the initial ``await`` (list_tools), all mutations are synchronous
        — atomic from the event loop's perspective.
        """
        from tools.registry import registry

        if not self._advertises_tools():
            # A server that doesn't implement tools/* should never send
            # tools/list_changed, but guard anyway — calling tools/list
            # would raise MCPError(-32601).
            return

        async with self._refresh_lock:
            # Capture old tool names for change diff
            old_tool_names = set(self._registered_tool_names)

            # 1. Fetch current tool list from server (follow nextCursor)
            async with self._rpc_lock:
                new_mcp_tools = await _paginate_full_list(
                    self.session.list_tools, "tools", self.name
                )

            # 2. Re-register with fresh tool list. Avoid nuke-and-repave for
            # all names: live agent turns may already have tool-call IDs
            # pointing at existing handler functions. Replacing entries
            # in-place is enough for unchanged names and avoids transient
            # "tool not connected" / stale-handler races during startup
            # notifications. Tools absent from the fresh list are no longer
            # callable, so remove only those stale registry entries first.
            toolset_name = f"mcp-{self.name}"
            stale_tool_names = old_tool_names - {
                mcp_prefixed_tool_name(self.name, tool.name)
                for tool in new_mcp_tools
            }
            for tool_name in stale_tool_names:
                # Never let one server's refresh remove a colliding name that
                # is currently owned by another server.
                if registry.get_toolset_for_tool(tool_name) != toolset_name:
                    continue
                registry.deregister(tool_name)
                _forget_mcp_tool_server(tool_name)

            # 3. Re-register with the fresh list. The helper may skip names that
            # are ambiguous after normalization.
            self._tools = new_mcp_tools
            registered_names = _register_server_tools(
                self.name, self, self._config
            )

            # A previously unique raw name can become ambiguous without changing
            # its normalized registry name. In that case the pre-pass above does
            # not consider it stale, so remove any old entry that the final,
            # collision-checked registration set no longer owns.
            registered_name_set = set(registered_names)
            for tool_name in old_tool_names - registered_name_set:
                if registry.get_toolset_for_tool(tool_name) != toolset_name:
                    continue
                registry.deregister(tool_name)
                _forget_mcp_tool_server(tool_name)
            self._registered_tool_names = registered_names

            # 4. Log what changed (user-visible notification)
            new_tool_names = set(self._registered_tool_names)
            added = new_tool_names - old_tool_names
            removed = old_tool_names - new_tool_names
            changes = []
            if added:
                changes.append(f"added: {', '.join(sorted(added))}")
            if removed:
                changes.append(f"removed: {', '.join(sorted(removed))}")
            if changes:
                logger.warning(
                    "MCP server '%s': tools changed dynamically — %s. "
                    "Verify these changes are expected.",
                    self.name, "; ".join(changes),
                )
            else:
                logger.info(
                    "MCP server '%s': dynamically refreshed %d tool(s) (no changes)",
                    self.name, len(self._registered_tool_names),
                )

    async def _keepalive_probe(self) -> None:
        """Exercise the session to detect a stale/expired connection.

        Uses ``ping`` (cheap, transport-agnostic liveness) by default. ``ping``
        is an OPTIONAL MCP utility: a server that doesn't implement it answers
        JSON-RPC -32601. The first time that happens we latch
        ``_ping_unsupported`` and fall back to the pre-ping probe — capability
        permitting, ``list_tools``; otherwise ``ping`` is the only option and
        the -32601 propagates (a server advertising neither a working ping nor
        tools has no liveness primitive left). The latch resets on each fresh
        transport connection so a server that gains ping support after a
        reconnect is re-probed with the cheap path.

        Raises on a genuine connection failure so the caller triggers a
        reconnect; returns normally when the session is alive.
        """
        if not self._ping_unsupported:
            try:
                await asyncio.wait_for(self.session.send_ping(), timeout=30.0)
                return
            except Exception as exc:
                # Only a "method not found" means ping is unsupported. Any
                # other error (timeout, closed transport, session expired) is
                # a real liveness failure — propagate so we reconnect.
                if not _is_method_not_found_error(exc):
                    raise
                if not self._advertises_tools():
                    # No ping, no tools → no cheaper probe to fall back to.
                    raise
                self._ping_unsupported = True
                logger.info(
                    "MCP server '%s': does not implement the optional 'ping' "
                    "utility (-32601); using 'list_tools' for keepalive on "
                    "this connection.",
                    self.name,
                )

        # Fallback probe for servers without ping support.
        await asyncio.wait_for(self.session.list_tools(), timeout=30.0)

    def _mark_session_proven(self) -> None:
        """Record that the current session demonstrated real health.

        Called from the keepalive success path (session survived at least one
        full keepalive interval) and the tool-call success path. Only then is
        the reconnect budget cleared: a handshake that completes but drops
        moments later must keep consuming ``_reconnect_retries`` so a flapping
        transport still reaches the park instead of respawning forever.
        """
        if not self._session_proven:
            self._session_proven = True
            self._reconnect_retries = 0
            if self._was_parked:
                self._was_parked = False
                logger.warning(
                    "MCP server '%s': revived — session healthy again after "
                    "parking (state: parked → connected)",
                    self.name,
                )

    async def _wait_for_lifecycle_event(self) -> str:
        """Block until either _shutdown_event or _reconnect_event fires.

        Returns:
            "shutdown"  if the server should exit the run loop entirely.
            "reconnect" if the server should tear down the current MCP
                        session and re-enter the transport (new session
                        ID, etc.). The reconnect event is cleared before
                        return so the next cycle starts with a fresh signal.

        Shutdown takes precedence if both events are set simultaneously.

        Periodically sends a lightweight keepalive (``ping``, with a
        ``list_tools`` fallback for servers that don't implement the optional
        ping utility — see :meth:`_keepalive_probe`) to prevent TCP/session
        state from going stale during idle periods. If the keepalive fails,
        triggers a reconnect.

        The cadence is ``keepalive_interval`` from server config (default
        :data:`_DEFAULT_KEEPALIVE_INTERVAL`, floored at
        :data:`_MIN_KEEPALIVE_INTERVAL`); it must be below the server's
        session TTL or every idle tool call lands on an expired session.
        """
        # ``ping`` rather than ``list_tools`` so the probe stays a few bytes
        # regardless of how many tools the server exposes.
        keepalive_interval = max(
            _MIN_KEEPALIVE_INTERVAL,
            float(self._config.get("keepalive_interval", _DEFAULT_KEEPALIVE_INTERVAL)),
        )

        shutdown_task = asyncio.create_task(self._shutdown_event.wait())
        reconnect_task = asyncio.create_task(self._reconnect_event.wait())
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {shutdown_task, reconnect_task},
                    timeout=keepalive_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break

                # Timeout — no lifecycle event fired.  Probe the connection
                # to detect stale/expired sessions.
                if self.session:
                    try:
                        await self._keepalive_probe()
                    except Exception as exc:
                        root = _unwrap_exception_group(exc)
                        logger.warning(
                            "MCP server '%s' keepalive failed, triggering "
                            "reconnect (state: connected → degraded): %s: %s",
                            self.name, type(root).__name__, root,
                        )
                        self._reconnect_event.set()
                        break
                    # Keepalive succeeded — the session survived a full
                    # keepalive interval, which is real proof of health.
                    self._mark_session_proven()
        finally:
            for t in (shutdown_task, reconnect_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

        if self._shutdown_event.is_set():
            return "shutdown"
        self._reconnect_event.clear()
        return "reconnect"

    async def _wait_for_reconnect_or_shutdown(
        self, timeout: Optional[float] = None
    ) -> str:
        """Block until a reconnect or shutdown is requested while parked.

        Used by :meth:`run` after the reconnect budget is exhausted. The
        task stays alive (so ``_reconnect_event`` always has a listener) but
        does no work until something explicitly asks it to come back —
        a manual ``/mcp`` refresh — or, when ``timeout`` is
        given, until the timeout elapses (a periodic self-probe). The timed
        wake matters because parking deregisters this server's tools, so
        no tool call can ever reach the circuit-breaker's half-open probe
        or ``_signal_reconnect`` — without a self-probe a parked server
        would be unrevivable short of a full reload.

        Returns:
            ``"shutdown"`` if the server should exit the run loop entirely,
            ``"reconnect"`` if it should rebuild the transport (explicit
            request or self-probe timeout). The reconnect event is cleared
            before returning so the next park cycle starts from a fresh
            signal. Shutdown takes precedence.
        """
        shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())
        reconnect_task = asyncio.ensure_future(self._reconnect_event.wait())
        try:
            await asyncio.wait(
                {shutdown_task, reconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=timeout,
            )
        finally:
            for t in (shutdown_task, reconnect_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
        if self._shutdown_event.is_set():
            return "shutdown"
        self._reconnect_event.clear()
        return "reconnect"

    async def _run_stdio(self, config: dict):
        """Run the server using stdio transport."""
        command = config.get("command")
        args = config.get("args", [])
        user_env = config.get("env")

        if not command:
            raise ValueError(
                f"MCP server '{self.name}' has no 'command' in config"
            )

        safe_env = _build_safe_env(user_env)
        command, safe_env = _resolve_stdio_command(command, safe_env)

        # Wrap the real command in a parent-death watchdog supervisor so an
        # ungraceful exit of this process (kill -9, crash) can't leave the
        # stdio MCP child and its descendants running forever. On a clean
        # exit, MCPServerTask.shutdown() / _kill_orphaned_mcp_children() do
        # the reaping.
        command, args = _wrap_command_with_watchdog(command, args)

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=safe_env if safe_env else None,
            cwd=config.get("cwd"),
            # Chunk-boundary bytes may not form valid UTF-8; use "replace" to
            # substitute undecodable bytes with U+FFFD instead of crashing
            # with UnicodeDecodeError.
            encoding_error_handler="replace",
        )

        sampling_kwargs = self._sampling.session_kwargs() if self._sampling else {}
        if self._elicitation:
            sampling_kwargs.update(self._elicitation.session_kwargs())
        sampling_kwargs["message_handler"] = self._make_message_handler()
        sampling_kwargs["logging_callback"] = self._make_logging_callback()

        # Reap any orphaned subprocesses from prior failed connection
        # attempts before spawning a new one.  Without this, each retry in
        # the run() reconnect loop spawns a fresh process pair while the
        # previous failed pair lingers.  The unscoped sweep also
        # opportunistically reaps orphans left by *other* servers that
        # never reconnect; per-server filtering via ``server_name`` remains
        # available for scoped call sites.  Run in a worker thread: the
        # reaper blocks up to 2s (SIGTERM → wait → SIGKILL) when orphans
        # exist, which would otherwise stall the shared MCP event loop.
        await asyncio.to_thread(_kill_orphaned_mcp_children)

        # Snapshot child PIDs before spawning so we can track the new one.
        pids_before = _snapshot_child_pids()
        new_pids: set = set()
        _write_stderr_log_header(self.name)
        _errlog = _get_mcp_stderr_log()
        try:
            async with stdio_client(server_params, errlog=_errlog) as (
                read_stream,
                write_stream,
            ):
                # Capture the newly spawned subprocess PID for force-kill
                # cleanup, filtering out non-MCP children (LSP servers) that
                # race into the snapshot window.
                new_pids = _filter_mcp_children(
                    _snapshot_child_pids() - pids_before
                )
                if new_pids:
                    # Capture pgid while the child is alive — once it exits we
                    # can no longer call ``os.getpgid`` on it, and the cleanup
                    # sweep needs the pgid to reach any reparented descendants.
                    new_pgids: Dict[int, int] = {}
                    for _pid in new_pids:
                        try:
                            new_pgids[_pid] = os.getpgid(_pid)
                        except (ProcessLookupError, OSError):
                            # ProcessLookupError: child raced and already exited
                            pass
                    with _lock:
                        for _pid in new_pids:
                            _stdio_pids[_pid] = self.name
                        _stdio_pgids.update(new_pgids)
                async with ClientSession(
                    read_stream, write_stream, **sampling_kwargs
                ) as session:
                    # Bound the MCP handshake. A stdio server that never
                    # completes ``initialize`` otherwise hangs this coroutine
                    # forever on the background loop, the cleanup ``finally``
                    # below never runs, and the child plus its pipes leak on
                    # every discovery retry. Timing out here lets the
                    # ``finally`` reap the child.
                    connect_timeout = float(
                        config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
                    )
                    self.initialize_result = await self._negotiate_session(
                        session, connect_timeout
                    )
                    self.session = session
                    await self._discover_tools()
                    self._ready.set()
                    # Session is live again: clear any breaker state from a
                    # prior outage so the first call after recovery isn't
                    # gated on a stale consecutive-failure count.
                    _reset_server_error(self.name)
                    # A completed handshake alone is NOT proof of health: a
                    # flapping transport can handshake fine and drop moments
                    # later. The session must prove itself (keepalive success
                    # or a successful tool call) before the reconnect budget
                    # is cleared — see _mark_session_proven.
                    self._session_proven = False
                    return await self._wait_for_lifecycle_event()
        finally:
            # Runs on clean exit, exceptions, AND asyncio cancellation.
            # If any of the spawned PIDs are still alive, the SDK's
            # teardown failed (common when the task is cancelled mid-way
            # on Linux, where setsid() children escape the parent cgroup).
            # Mark them as orphans so the next cleanup sweep can reap them.
            if new_pids:
                from gateway.status import _pid_exists
                with _lock:
                    for _pid in new_pids:
                        _stdio_pids.pop(_pid, None)
                    for pid in new_pids:
                        # Use the shared existence check rather than
                        # ``os.kill(pid, 0)`` so identity semantics stay
                        # uniform across every host surface.
                        pid_alive = _pid_exists(pid)
                        pgroup_alive = False
                        pgid = _stdio_pgids.get(pid)
                        if not pid_alive and pgid is not None:
                            # Direct child exited but descendants may still be
                            # in its pgroup.  Probe with signal 0 — succeeds
                            # iff any pgroup member is alive.
                            try:
                                os.killpg(pgid, 0)
                                pgroup_alive = True
                            except (ProcessLookupError, PermissionError, OSError):
                                pgroup_alive = False
                        if pid_alive or pgroup_alive:
                            _orphan_stdio_pids.add(pid)
                            _orphan_stdio_pid_servers[pid] = self.name
                        else:
                            # Nothing left to reap — drop the pgid entry so
                            # PID-reuse can't surface stale pgroup state later.
                            _stdio_pgids.pop(pid, None)

    # Content types a real MCP Streamable-HTTP endpoint may return on the
    # initial POST/GET. Anything else on a 2xx response means the URL is not
    # an MCP endpoint.
    _MCP_CONTENT_TYPES = ("application/json", "text/event-stream")

    async def _preflight_content_type(
        self,
        url: str,
        *,
        headers: Optional[dict] = None,
        timeout: float = 5.0,
    ) -> None:
        """Probe *url* for an MCP-shaped response before the SDK connects.

        A misconfigured ``mcp_servers.<name>.url`` pointed at a plain web app
        returns HTML (or some other non-MCP body). The MCP SDK then sits on
        the connection for the full ``connect_timeout`` (default 60 s) before
        surfacing an opaque ``CancelledError``. A cheap, short-timeout probe
        here catches that in ≤ ``timeout`` seconds and raises
        :class:`NonMcpEndpointError` with an actionable message.

        Detection is allow-list based: a 2xx response is rejected only when it
        carries a definite content type that is NOT one an MCP endpoint uses
        (``application/json`` / ``text/event-stream``).  When HEAD/GET returns
        a non-MCP content type (e.g. ``text/html``), a lightweight JSON-RPC
        ``initialize`` POST is attempted before giving up — some servers
        serve a web UI on GET but speak Streamable HTTP only via POST.

        A missing or empty content type, non-2xx status, or any
        network/transport error passes through silently — the probe is
        strictly best-effort, and the real handshake remains the source of
        truth for everything except the unambiguous "this is a web page,
        not MCP" case.

        Runs on its own httpx client OUTSIDE the SDK's anyio task group, so the
        raised error propagates as itself rather than being wrapped in an
        ``ExceptionGroup``.
        """
        import httpx as _httpx

        client_kwargs: dict = {
            "follow_redirects": True,
            "timeout": _httpx.Timeout(timeout),
        }

        probe_headers = dict(headers) if headers else {}
        try:
            async with _httpx.AsyncClient(**client_kwargs) as client:
                # HEAD is cheapest; fall back to GET if the server doesn't
                # implement it (405 Method Not Allowed / 501 Not Implemented).
                resp = await client.head(url, headers=probe_headers)
                if resp.status_code in (405, 501):
                    resp = await client.get(url, headers=probe_headers)

                # Before rejecting a non-MCP content type, try a lightweight
                # JSON-RPC POST probe so POST-only servers pass.
                ct = (
                    resp.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if (
                    ct
                    and ct not in self._MCP_CONTENT_TYPES
                    and 200 <= resp.status_code < 300
                ):
                    post_resp = await client.post(
                        url,
                        headers={
                            **probe_headers,
                            "Content-Type": "application/json",
                            "Accept": "application/json, text/event-stream",
                        },
                        content=(
                            '{"jsonrpc":"2.0","id":"_probe",'
                            '"method":"initialize",'
                            '"params":{"protocolVersion":"2025-03-26",'
                            '"capabilities":{},'
                            '"clientInfo":{"name":"son-of-anton-probe",'
                            '"version":"0.1"}}}'
                        ),
                    )
                    if 200 <= post_resp.status_code < 300:
                        post_ct = (
                            post_resp.headers.get("content-type", "")
                            .split(";")[0]
                            .strip()
                            .lower()
                        )
                        if post_ct in self._MCP_CONTENT_TYPES:
                            resp = post_resp
        except _httpx.HTTPError:
            return  # DNS/connect/timeout/transport error — let the SDK try.

        # Only judge successful responses. A 4xx/5xx may be an auth challenge
        # or a transient error the real handshake handles correctly.
        if not (200 <= resp.status_code < 300):
            return

        ct_base = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not ct_base:
            return  # No content type advertised — don't second-guess the SDK.
        if ct_base in self._MCP_CONTENT_TYPES:
            return  # Looks like a real MCP endpoint.

        raise NonMcpEndpointError(
            f"MCP server '{self.name}' at {url} returned Content-Type "
            f"'{ct_base}', not an MCP response (expected one of: "
            f"{', '.join(self._MCP_CONTENT_TYPES)}). The URL most likely "
            "points at a web page rather than an MCP endpoint — check it "
            "resolves to a Streamable HTTP endpoint "
            "(e.g. https://host/mcp, not https://host/)."
        )

    def _reconnect_or_reraise_group(self, eg: BaseExceptionGroup) -> str:
        """Map an SDK transport TaskGroup failure to a clean ``"reconnect"``.

        The Streamable-HTTP transport runs its stream pump inside an anyio
        TaskGroup. A transient stream drop (idle timeout, brief backend blip,
        server-side TCP close) surfaces as a ``BaseExceptionGroup`` escaping the
        transport context manager. Left unwrapped it reaches ``run()``'s error
        path, which applies exponential backoff and eventually *parks* the
        server for 300s and deregisters its tools — a multi-minute tool outage
        for what is usually a sub-second glitch while the POST path stays
        healthy.

        Returning ``"reconnect"`` instead lets ``run()`` rebuild the session
        immediately with no backoff, no park, and no tool deregistration.

        Re-raise (rather than mask) when the failure is not a transient drop:
        - shutdown is in progress (``shutdown()`` sets ``_shutdown_event``
          before it ever cancels the task);
        - the group carries a ``KeyboardInterrupt`` / ``SystemExit`` — fatal
          signals must propagate to the interpreter, never be converted into
          a reconnect;
        - the group carries a real ``CancelledError`` (task cancellation must
          propagate to asyncio, mirroring the ``run()`` guard);
        - we never reached a live session this attempt (``_ready`` unset) — a
          connect/handshake failure SHOULD fall through to ``run()``'s backoff
          rather than hot-loop reconnects against a broken endpoint.
        """
        if self._shutdown_event.is_set():
            raise eg
        fatal, _rest = eg.split((KeyboardInterrupt, SystemExit))
        if fatal is not None:
            raise eg
        cancelled, _rest = eg.split(asyncio.CancelledError)
        if cancelled is not None:
            raise eg
        if not self._ready.is_set():
            raise eg
        logger.debug(
            "MCP server '%s': transport TaskGroup exited after a live session "
            "(%r) — reconnecting immediately instead of backing off",
            self.name, eg,
        )
        return "reconnect"

    async def _run_http(self, config: dict):
        """Run the server using Streamable HTTP transport."""
        url = config["url"]
        headers = dict(config.get("headers") or {})
        # Some MCP servers require MCP-Protocol-Version on the initial
        # initialize request and reject session-less POSTs otherwise.
        # Seed it as a client-level default, but treat user overrides as
        # case-insensitive so conventional casing is preserved.
        #
        # Seeded from the HANDSHAKE version, not the latest one: this transport
        # connects via `ClientSession.initialize()`, which sends
        # LATEST_HANDSHAKE_VERSION (2025-11-25) in the body. Advertising
        # 2026-07-28 in the header routes the request onto the server's
        # per-request-envelope ladder, which then rejects the legacy body for
        # missing its required `params._meta` envelope keys. The header has to
        # agree with what the body actually speaks.
        if not any(key.lower() == "mcp-protocol-version" for key in headers):
            headers["mcp-protocol-version"] = LATEST_HANDSHAKE_VERSION
        connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)

        sampling_kwargs = self._sampling.session_kwargs() if self._sampling else {}
        if self._elicitation:
            sampling_kwargs.update(self._elicitation.session_kwargs())
        sampling_kwargs["message_handler"] = self._make_message_handler()
        sampling_kwargs["logging_callback"] = self._make_logging_callback()

        # Build an explicit AsyncClient matching the SDK's own
        # create_mcp_http_client defaults. It has to be httpx2 — the module
        # the SDK's transport is built on — because the SDK sends its own
        # Request objects through this client.
        import httpx2 as httpx

        client_kwargs: dict = {
            "follow_redirects": True,
            "timeout": httpx.Timeout(float(connect_timeout), read=300.0),
            "event_hooks": {"response": [_make_redirect_header_stripper(httpx.URL(url))]},
        }
        if headers:
            client_kwargs["headers"] = headers

        # Caller owns the client lifecycle — the SDK skips cleanup when
        # http_client is provided, so we wrap in async-with.
        try:
            async with httpx.AsyncClient(**client_kwargs) as http_client:
                async with streamable_http_client(url, http_client=http_client) as (
                    read_stream,
                    write_stream,
                ):
                    async with ClientSession(read_stream, write_stream, **sampling_kwargs) as session:
                        # Bound the handshake — see stdio path.
                        self.initialize_result = await self._negotiate_session(
                            session, float(connect_timeout)
                        )
                        self.session = session
                        await self._discover_tools()
                        self._ready.set()
                        # Session is live again: clear any breaker state from
                        # a prior outage.
                        _reset_server_error(self.name)
                        # Unproven until keepalive/tool-call success.
                        self._session_proven = False
                        reason = await self._wait_for_lifecycle_event()
                        if reason == "reconnect":
                            logger.info(
                                "MCP server '%s': reconnect requested — "
                                "tearing down HTTP session", self.name,
                            )
        except BaseExceptionGroup as _eg:
            # Streamable-HTTP transport TaskGroup dropped: reconnect
            # immediately instead of backoff/park.
            reason = self._reconnect_or_reraise_group(_eg)
        return reason

    async def _discover_tools(self):
        """Discover tools from the connected session.

        Capability-gated: prompt-only / resource-only MCP servers don't
        implement ``tools/list``, and calling it raises ``MCPError(-32601)``,
        which would abort the connection. Skip the call when the server
        doesn't advertise the ``tools`` capability.
        """
        # Fresh transport connection → re-probe with the cheap ``ping`` path.
        # Clears any latch from a prior connection in case the server gained
        # ping support across the reconnect.
        self._ping_unsupported = False
        if self.session is None:
            return
        if not self._advertises_tools():
            logger.info(
                "MCP server '%s': does not advertise 'tools' capability — "
                "skipping tools/list (prompts/resources remain available)",
                self.name,
            )
            self._tools = []
            self._register_discovered_tools_if_needed()
            return
        async with self._rpc_lock:
            self._tools = await _paginate_full_list(
                self.session.list_tools, "tools", self.name,
            )
        self._register_discovered_tools_if_needed()

    def _register_discovered_tools_if_needed(self) -> None:
        """Re-register tools after an owned server reconnects if needed.

        Initial registration is performed by ``_discover_and_register_server``
        after ``start()`` completes. During a later reconnect, outage handling
        may clear ``_ready`` before discovery and may deregister stale tools.
        A managed server can still be identified by its entry in ``_servers``;
        publish its freshly discovered tools before transport readiness is
        restored so a successful revival cannot come back with zero tools.
        A server retained after a recoverable initial failure is likewise
        registry-owned before its first successful session, so ownership also
        authorizes its first publication.
        """
        if self._registered_tool_names:
            return
        if not self._ready.is_set():
            with _lock:
                if _servers.get(self.name) is not self:
                    return
        self._registered_tool_names = _register_server_tools(
            self.name, self, self._config
        )
        # A retained initial-failure server that just published tools has
        # recovered: drop its stale connect error so status surfaces stop
        # reporting it as failed.
        with _lock:
            if _servers.get(self.name) is self:
                _server_connect_errors.pop(self.name, None)

    async def _park(self) -> bool:
        """Drop this server's tools and wait while parked.

        Parking deregisters the tools, so no tool call can reach the
        circuit-breaker half-open probe or ``_signal_reconnect`` — the wait is
        TIMED: every ``_PARKED_RETRY_INTERVAL`` the task wakes and attempts one
        reconnect itself. An explicit ``_reconnect_event.set()`` (manual /mcp
        refresh) wakes it immediately. The task never exits here: exiting
        would leave nothing listening for ``_reconnect_event``.

        Returns True when shutdown was requested.
        """
        self._was_parked = True
        self._deregister_tools()
        self._reconnect_event.clear()
        parked = await self._wait_for_reconnect_or_shutdown(
            timeout=_PARKED_RETRY_INTERVAL
        )
        if parked == "shutdown":
            return True
        logger.debug(
            "MCP server '%s': attempting revival from parked state "
            "(self-probe or explicit reconnect request); rebuilding transport.",
            self.name,
        )
        return False

    async def run(self, config: dict):
        """Long-lived coroutine: connect, discover tools, wait, disconnect.

        Includes automatic reconnection with exponential backoff if the
        connection drops unexpectedly (unless shutdown was requested).
        """
        self._config = config
        self.tool_timeout = config.get("timeout", _DEFAULT_TOOL_TIMEOUT)
        _ensure_mcp_sdk()

        # Sampling lets the server request LLM completions; elicitation lets
        # it ask the user for structured input mid-tool-call (routed through
        # the approval system). Both default on.
        if config.get("sampling", {}).get("enabled", True):
            self._sampling = SamplingHandler(self.name)
        else:
            self._sampling = None
        if config.get("elicitation", {}).get("enabled", True):
            self._elicitation = ElicitationHandler(self.name, owner=self)
        else:
            self._elicitation = None

        # Validate: warn if both url and command are present
        if "url" in config and "command" in config:
            logger.warning(
                "MCP server '%s' has both 'url' and 'command' in config. "
                "Using HTTP transport ('url'). Remove 'command' to silence "
                "this warning.",
                self.name,
            )

        # Validate remote URL once, up front, so a typo in config.toml fails
        # fast with a clear error instead of burning the reconnect backoff.
        if self._is_http():
            try:
                _validate_remote_mcp_url(self.name, config.get("url"))
            except InvalidMcpUrlError as exc:
                logger.warning("%s", exc)
                self._error = exc
                self._ready.set()
                return

            # Pre-flight content-type probe. A URL pointed at a web-app root
            # returns HTML, which makes the SDK hang for the full
            # connect_timeout before surfacing an opaque CancelledError.
            # Probing once, outside the SDK task group, fails fast and
            # non-retryably. Skipped on reconnects (_ready already set).
            if not self._ready.is_set():
                try:
                    _probe_headers = dict(config.get("headers") or {})
                    await self._preflight_content_type(
                        config["url"],
                        headers=_probe_headers,
                    )
                except NonMcpEndpointError as exc:
                    logger.warning("%s", exc)
                    self._error = exc
                    self._ready.set()
                    return

        self._reconnect_retries = 0
        initial_retries = 0
        backoff = 1.0

        while True:
            try:
                if self._is_http():
                    await self._run_http(config)
                else:
                    await self._run_stdio(config)
                # Transport returned cleanly. Two cases:
                #  - _shutdown_event was set: exit the run loop entirely.
                #  - _reconnect_event was set: loop back and rebuild the MCP
                #    session. Do NOT touch the retry counters — this is not a
                #    failure.
                if self._shutdown_event.is_set():
                    break
                logger.debug(
                    "MCP server '%s': reconnecting (session expiry or "
                    "manual refresh)",
                    self.name,
                )
                # A clean transport return means a session was established and
                # then asked to rebuild. That alone is NOT proof of health: a
                # flapping transport handshakes fine and drops moments later.
                # Only clear the consecutive-failure budget once the session
                # PROVED healthy (_mark_session_proven).
                if self._session_proven:
                    self._reconnect_retries = 0
                    backoff = 1.0
                else:
                    # Unproven session: charge the rapid-drop budget so a
                    # flapping transport still reaches the park.
                    self._reconnect_retries += 1
                    if self._reconnect_retries > _MAX_RECONNECT_RETRIES:
                        logger.warning(
                            "MCP server '%s': %d consecutive reconnects "
                            "without a healthy session (rapid-drop budget "
                            "exhausted), parking; will self-probe every %ds "
                            "until it recovers (state: degraded → parked)",
                            self.name, _MAX_RECONNECT_RETRIES,
                            _PARKED_RETRY_INTERVAL,
                        )
                        if await self._park():
                            break
                        # One probe attempt per wake.
                        self._reconnect_retries = _MAX_RECONNECT_RETRIES
                        backoff = 1.0
                # Reset the session reference and readiness; _run_http/_run_stdio
                # will repopulate both on successful re-entry.  Leaving
                # _ready set here lets handler-side recovery mistake the stale
                # pre-reconnect session for a fresh one and retry too early.
                self._ready.clear()
                self.session = None
                continue
            except asyncio.CancelledError:
                # Task was cancelled (shutdown, gateway restart, explicit
                # task.cancel()). Not a connection failure: re-raise so the
                # cancellation propagates and ``shutdown()``'s
                # ``await self._task`` completes.
                self.session = None
                raise
            except Exception as exc:
                self.session = None
                # Unwrap anyio TaskGroup wrappers first: str(exc) on a
                # BaseExceptionGroup is useless in logs, and it hides the
                # root cause from the permanence classification below.
                root = _unwrap_exception_group(exc)
                failure_class = _classify_mcp_failure(root)

                # First connection attempt: retry with backoff before giving
                # up, so a transient blip at startup doesn't kill the server.
                # A deterministic failure (bad command, non-MCP URL, 401/403)
                # parks immediately. Parking (not returning) keeps the task
                # alive so the self-probe and /mcp refresh can revive it.
                if not self._ready.is_set():
                    if failure_class != "permanent":
                        initial_retries += 1
                    if (
                        failure_class == "permanent"
                        or initial_retries > _MAX_INITIAL_CONNECT_RETRIES
                    ):
                        if failure_class == "permanent":
                            logger.warning(
                                "MCP server '%s' failed initial connection with a "
                                "permanent error, parking without retries "
                                "(state: connecting → parked): %s: %s",
                                self.name, type(root).__name__, root,
                            )
                        else:
                            logger.warning(
                                "MCP server '%s' failed initial connection after "
                                "%d attempts, parking until a reconnect is "
                                "requested (state: connecting → parked): %s: %s",
                                self.name, _MAX_INITIAL_CONNECT_RETRIES,
                                type(root).__name__, root,
                            )
                        self._error = exc
                        self._ready.set()
                        if await self._park():
                            return
                        initial_retries = 0
                        self._reconnect_retries = 0
                        backoff = 1.0
                        self._error = None
                        self._ready.clear()
                        continue

                    logger.debug(
                        "MCP server '%s' initial connection failed "
                        "(attempt %d/%d), retrying in %.0fs: %s: %s",
                        self.name, initial_retries,
                        _MAX_INITIAL_CONNECT_RETRIES, backoff,
                        type(root).__name__, root,
                    )
                    await asyncio.sleep(_jittered(backoff))
                    backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                    # Check if shutdown was requested during the sleep
                    if self._shutdown_event.is_set():
                        self._error = exc
                        self._ready.set()
                        return
                    continue

                # If shutdown was requested, don't reconnect
                if self._shutdown_event.is_set():
                    logger.debug(
                        "MCP server '%s' disconnected during shutdown: %s: %s",
                        self.name, type(root).__name__, root,
                    )
                    return

                # A previously-working server that now fails deterministically
                # (revoked credentials, URL serving a web page, binary gone)
                # parks immediately; otherwise retry until the budget runs out.
                if failure_class != "permanent":
                    self._reconnect_retries += 1
                if (
                    failure_class == "permanent"
                    or self._reconnect_retries > _MAX_RECONNECT_RETRIES
                ):
                    if failure_class == "permanent":
                        logger.warning(
                            "MCP server '%s' hit a permanent error, parking "
                            "without retries; will self-probe every %ds "
                            "(state: connected → parked): %s: %s",
                            self.name, _PARKED_RETRY_INTERVAL,
                            type(root).__name__, root,
                        )
                    else:
                        logger.warning(
                            "MCP server '%s' failed after %d reconnection attempts, "
                            "parking; will self-probe every %ds until it recovers "
                            "(state: degraded → parked): %s: %s",
                            self.name, _MAX_RECONNECT_RETRIES,
                            _PARKED_RETRY_INTERVAL,
                            type(root).__name__, root,
                        )
                    if await self._park():
                        return
                    # One probe attempt per wake: a still-dead server parks
                    # again for another interval instead of burning 5 rapid
                    # retries each cycle.
                    self._reconnect_retries = _MAX_RECONNECT_RETRIES
                    backoff = 1.0
                    continue

                # Per-attempt retry chatter stays at DEBUG; state transitions
                # carry the WARNINGs — one line per transition, not per try.
                logger.debug(
                    "MCP server '%s' connection lost (attempt %d/%d), "
                    "reconnecting in %.0fs: %s: %s",
                    self.name, self._reconnect_retries, _MAX_RECONNECT_RETRIES,
                    backoff, type(root).__name__, root,
                )
                await asyncio.sleep(_jittered(backoff))
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                # Check again after sleeping
                if self._shutdown_event.is_set():
                    return
            finally:
                self.session = None

    async def start(self, config: dict):
        """Create the background Task and wait until ready (or failed)."""
        self._task = asyncio.ensure_future(self.run(config))
        try:
            await self._ready.wait()
        except asyncio.CancelledError:
            # The caller's connect timeout (discover_mcp_tools wraps start()
            # in asyncio.wait_for) cancels *this* coroutine, but the
            # ensure_future'd run() task is independent and would otherwise
            # keep running detached — parked on a hung transport with no
            # owner to reap it. Propagate the cancellation so the
            # transport context managers unwind and their finally blocks
            # release the child process / FDs.
            if self._task and not self._task.done():
                self._task.cancel()
            raise
        if self._error:
            raise self._error

    async def shutdown(self):
        """Signal the Task to exit and wait for clean resource teardown."""
        self._shutdown_event.set()
        # Defensive: if _wait_for_lifecycle_event is blocking, we need ANY
        # event to unblock it. _shutdown_event alone is sufficient (the
        # helper checks shutdown first), but setting reconnect too ensures
        # there's no race where the helper misses the shutdown flag after
        # returning "reconnect".
        self._reconnect_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP server '%s' shutdown timed out, cancelling task",
                    self.name,
                )
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        if self._pending_refresh_tasks:
            for task in list(self._pending_refresh_tasks):
                task.cancel()
            await asyncio.gather(*self._pending_refresh_tasks, return_exceptions=True)
            self._pending_refresh_tasks.clear()
        self._deregister_tools("disconnected")
        self.session = None

    def _deregister_tools(self, reason: str = "reconnecting") -> None:
        """Drop this server's tools from the global registry (idempotent).

        Pulls the server's tool schemas out of the registry so NEW tool
        snapshots (a new session, /reload-mcp, an agent still inside its
        opening-turns refresh window) don't advertise them. Called on shutdown
        (``reason="disconnected"``) AND when the reconnect budget is exhausted
        (``reason="reconnecting"``: the server parks and self-probes).

        An agent whose snapshot is already frozen keeps these tools in its
        schema — removing them would bust the conversation's prompt cache.
        Its calls are answered by ``departed_mcp_tool_error`` (via *reason*)
        until revival re-registers the same names.
        """
        from tools.registry import registry

        for tool_name in list(getattr(self, "_registered_tool_names", [])):
            registry.deregister(tool_name)
            _forget_mcp_tool_server(tool_name, reason)
        self._registered_tool_names = []


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_servers: Dict[str, MCPServerTask] = {}
_server_connecting: set[str] = set()
_server_connect_errors: Dict[str, str] = {}
# Discovery installs a task-local claim before calling ``_connect_server`` so
# it can retain a recoverable parked task without making standalone probe calls
# publish failed servers into module-global ownership.
_connect_server_claim: contextvars.ContextVar[
    Optional[Callable[[MCPServerTask], None]]
] = contextvars.ContextVar("mcp_connect_server_claim", default=None)

# Connection-retry cooldown (per-server isolation against restart storms).
#
# A stdio MCP server that fails to spawn is never recorded in ``_servers``,
# so without a cooldown EVERY subsequent ``discover_mcp_tools()`` (one per
# agent worker session) would re-spawn it from scratch — a restart storm that
# piles up subprocesses and destabilises the healthy co-located servers.
# After a failed connection attempt, stamp a monotonic ``retry_after``
# deadline with exponential backoff; ``register_mcp_servers`` skips a server
# whose cooldown has not elapsed. A successful connection clears the state.
_server_connect_retry_after: Dict[str, float] = {}   # name -> monotonic deadline
_server_connect_failures: Dict[str, int] = {}        # name -> consecutive failures
_CONNECT_RETRY_BASE_BACKOFF_SEC = 30.0
_CONNECT_RETRY_MAX_BACKOFF_SEC = 600.0


def _record_connect_failure(server_name: str) -> None:
    """Stamp an exponential-backoff cooldown after a failed connect.

    Called (under ``_lock``) when a server fails its discovery/connect
    attempt. The cooldown grows geometrically with the consecutive
    failure count and is capped at :data:`_CONNECT_RETRY_MAX_BACKOFF_SEC`,
    so a permanently-broken server settles into infrequent retries
    rather than a tight respawn loop.
    """
    n = _server_connect_failures.get(server_name, 0) + 1
    _server_connect_failures[server_name] = n
    backoff = min(
        _CONNECT_RETRY_BASE_BACKOFF_SEC * (2 ** (n - 1)),
        _CONNECT_RETRY_MAX_BACKOFF_SEC,
    )
    _server_connect_retry_after[server_name] = time.monotonic() + backoff


def _clear_connect_failure(server_name: str) -> None:
    """Clear the connect-cooldown state after a successful connection."""
    _server_connect_failures.pop(server_name, None)
    _server_connect_retry_after.pop(server_name, None)


def _connect_cooldown_active(server_name: str) -> bool:
    """Return True if ``server_name`` is still within its retry cooldown."""
    deadline = _server_connect_retry_after.get(server_name)
    return deadline is not None and time.monotonic() < deadline

# Circuit breaker: consecutive error counts per server.  After
# _CIRCUIT_BREAKER_THRESHOLD consecutive failures, the handler returns
# a "server unreachable" message that tells the model to stop retrying
# instead of burning iterations on a dead server.
#
# State machine:
#   closed    — error count below threshold; all calls go through.
#   open      — threshold reached; calls short-circuit until the
#               cooldown elapses.
#   half-open — cooldown elapsed; the next call is a probe that
#               actually hits the session. Probe success → closed.
#               Probe failure → reopens (cooldown re-armed).
#
# ``_server_breaker_opened_at`` records the monotonic timestamp when
# the breaker most recently transitioned into the open state. Use the
# ``_bump_server_error`` / ``_reset_server_error`` helpers to mutate
# this state — they keep the count and timestamp in sync.
_server_error_counts: Dict[str, int] = {}
_server_breaker_opened_at: Dict[str, float] = {}
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN_SEC = 60.0


def _bump_server_error(server_name: str) -> None:
    """Increment the consecutive-failure count for ``server_name``.

    When the count crosses :data:`_CIRCUIT_BREAKER_THRESHOLD`, stamp the
    breaker-open timestamp so the cooldown clock starts (or re-starts,
    for probe failures in the half-open state).
    """
    n = _server_error_counts.get(server_name, 0) + 1
    _server_error_counts[server_name] = n
    if n >= _CIRCUIT_BREAKER_THRESHOLD:
        _server_breaker_opened_at[server_name] = time.monotonic()


def _reset_server_error(server_name: str) -> None:
    """Fully close the breaker for ``server_name``.

    Clears both the failure count and the breaker-open timestamp. Call
    this on any unambiguous success signal (successful tool call,
    successful reconnect, manual /mcp refresh).
    """
    _server_error_counts[server_name] = 0
    _server_breaker_opened_at.pop(server_name, None)


def _signal_reconnect(server: Any) -> bool:
    """Ask a server task to rebuild its transport, thread-safely.

    The tool handlers run on caller threads, while the server task and its
    ``_reconnect_event`` live on the background MCP loop. Setting an
    asyncio.Event from another thread must go through
    ``loop.call_soon_threadsafe``; non-async adapters and tests without a
    running loop can use a direct ``.set()``.

    Returns True if a reconnect signal was delivered, False if the server
    has no reconnect machinery (nothing to revive).
    """
    event = getattr(server, "_reconnect_event", None)
    if event is None:
        return False
    loop = _mcp_loop
    if (
        isinstance(event, asyncio.Event)
        and loop is not None
        and loop.is_running()
    ):
        loop.call_soon_threadsafe(event.set)
    else:
        event.set()
    return True


def _wait_for_server_session_ready(
    srv: "MCPServerTask",
    *,
    old_session: Any = None,
    timeout: float = 15.0,
) -> bool:
    """Wait for an MCP server to expose a usable session.

    Tool handlers run in normal worker threads while the MCP transport lives on
    the module's background asyncio loop. During a reconnect there is a short
    window where ``srv.session`` is ``None`` (or still points at the stale
    session until the lifecycle coroutine has left the transport context). A
    handler that blindly retries in that window can burn circuit-breaker strikes
    and return ``not connected`` even though the reconnect is already in
    progress.

    When ``old_session`` is supplied, require the observed session object to be
    different so callers do not mistake the pre-reconnect, stale session for a
    fresh one.
    """
    # Iteration-bounded rather than deadline-bounded: several tests (and the
    # circuit-breaker cooldown logic) monkeypatch time.monotonic to a frozen
    # clock, which would make a monotonic-deadline loop spin forever.
    poll_interval = 0.25
    iterations = max(1, int(max(float(timeout), 0.0) / poll_interval))
    for i in range(iterations):
        session = getattr(srv, "session", None)
        ready = getattr(srv, "_ready", None)
        is_ready = True
        if ready is not None and hasattr(ready, "is_set"):
            try:
                is_ready = bool(ready.is_set())
            except Exception:
                is_ready = True
        if session is not None and session is not old_session and is_ready:
            return True
        if i < iterations - 1:
            time.sleep(poll_interval)
    return False


def _signal_reconnect_and_wait(
    server_name: str,
    srv: "MCPServerTask",
    *,
    op_description: str,
    timeout: float = 15.0,
) -> bool:
    """Ask a live MCP server task to rebuild its transport session.

    The important detail is clearing ``_ready`` on the MCP event loop before
    setting ``_reconnect_event``. Older code left ``_ready`` set across
    reconnects, so the caller's readiness poll could return immediately and
    retry against the same dead HTTP/stream session. That was observed as
    repeated ``Session terminated`` / ``not connected`` / circuit-breaker
    failures in long-lived gateway sessions even though a fresh CLI process
    could connect successfully.
    """
    loop = _mcp_loop
    if loop is None or not loop.is_running():
        return False

    old_session = getattr(srv, "session", None)

    def _request_reconnect() -> None:
        ready = getattr(srv, "_ready", None)
        if ready is not None and hasattr(ready, "clear"):
            ready.clear()
        reconnect_event = getattr(srv, "_reconnect_event", None)
        if reconnect_event is not None and hasattr(reconnect_event, "set"):
            reconnect_event.set()

    logger.info(
        "MCP server '%s': %s requesting transport reconnect",
        server_name, op_description,
    )
    loop.call_soon_threadsafe(_request_reconnect)
    return _wait_for_server_session_ready(
        srv,
        old_session=old_session,
        timeout=timeout,
    )

# Substrings (lower-cased match) that indicate the MCP server rejected
# the request because its server-side transport session expired /
# was garbage-collected; only the transport-layer session state needs
# rebuilding.
_SESSION_EXPIRED_MARKERS: tuple = (
    "invalid or expired session",
    "expired session",
    "session expired",
    "session not found",
    "unknown session",
    "session terminated",
    "closedresourceerror",
    "closed resource",
    "transport is closed",
    "connection closed",
    "broken pipe",
    "end of file",
)

# Upper bound on exception-graph nodes inspected by
# ``_is_session_expired_error``. The visited-identity set already breaks
# cycles across ``exceptions`` / ``__cause__`` / ``__context__``; the
# budget additionally bounds pathological acyclic graphs (e.g. deeply
# chained retries) so classification always terminates promptly. Kept
# comfortably above ``sys.getrecursionlimit()`` so legitimately deep
# wrapper stacks (task-group nesting) are still fully scanned.
_EXC_TRAVERSAL_MAX_NODES = 10_000


def _is_session_expired_error(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like an MCP transport session expiry.

    Streamable HTTP MCP servers may garbage-collect server-side session
    state — idle TTL, server restart, horizontal-scaling pod rotation,
    etc.  The SDK surfaces this as a JSON-RPC error whose message contains
    phrases like ``"Invalid or expired session"``.  What's needed is a
    transport reconnect — tear down and rebuild the transport +
    ``ClientSession`` pair, which is exactly what
    ``MCPServerTask._reconnect_event`` triggers.
    """
    # AnyIO's stream exceptions are commonly message-less. In particular,
    # ``str(ClosedResourceError()) == ""``, so marker matching alone misses the
    # exact failure emitted by both MCP stdio and HTTP transports.
    from anyio import BrokenResourceError, ClosedResourceError, EndOfStream

    transport_error_types = (BrokenResourceError, ClosedResourceError, EndOfStream)

    # ExceptionGroup trees can be arbitrarily deep or even cyclic when custom
    # exceptions expose ``exceptions``, and chained exceptions
    # (``raise X from Y`` / implicit ``__context__``) can likewise form
    # cycles when handlers re-raise previously seen exceptions. Traverse
    # once, iteratively, with an identity-visited set AND a bounded node
    # budget so classification can never spin, and inspect every reachable
    # node so user interruption always overrides transport markers or types
    # found elsewhere in the graph. The chain traversal matters for real
    # failures: SDK wrappers often raise a generic RuntimeError *from* the
    # message-less ClosedResourceError, leaving the transport signal only
    # reachable via ``__cause__``.
    stack: "list[BaseException | None]" = [exc]
    seen: set[int] = set()
    transport_error_found = False
    budget = _EXC_TRAVERSAL_MAX_NODES
    while stack and budget > 0:
        current = stack.pop()
        if current is None:
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        budget -= 1

        if isinstance(current, InterruptedError):
            return False
        if isinstance(current, transport_error_types):
            transport_error_found = True

        # Exception messages vary across SDK versions + server
        # implementations, so match on a small allow-list of stable
        # substrings rather than exception type. Kept narrow to avoid
        # false positives on unrelated server errors.
        msg = str(current).lower()
        if msg and any(marker in msg for marker in _SESSION_EXPIRED_MARKERS):
            transport_error_found = True

        stack.extend(getattr(current, "exceptions", ()))
        stack.append(getattr(current, "__cause__", None))
        stack.append(getattr(current, "__context__", None))

    return transport_error_found


def _handle_session_expired_and_retry(
    server_name: str,
    exc: BaseException,
    retry_call,
    op_description: str,
):
    """Trigger a transport reconnect and retry once on session expiry.

    Setting ``_reconnect_event`` causes the server task's lifecycle loop to
    tear down the current transport + ``ClientSession`` and rebuild them.

    Args:
        server_name: Name of the MCP server that raised.
        exc: The exception from the failed call.
        retry_call: Zero-arg callable that re-runs the operation,
            returning the same JSON string format as the handler.
        op_description: Human-readable name of the operation (logs).

    Returns:
        A JSON string if reconnect + retry was attempted and produced
        a response, or ``None`` to fall through to the caller's
        generic error path (not a session-expired error, no server
        record, reconnect didn't ready in time, or retry also failed).
    """
    if not _is_session_expired_error(exc):
        return None

    with _lock:
        srv = _servers.get(server_name)
    if srv is None or not hasattr(srv, "_reconnect_event"):
        return None

    loop = _mcp_loop
    if loop is None or not loop.is_running():
        return None

    logger.info(
        "MCP server '%s': %s failed with session-expired error (%s); "
        "signalling transport reconnect and retrying once.",
        server_name, op_description, exc,
    )

    # Trigger a transport reconnect, then wait briefly for the new
    # session to come back ready.
    if not _signal_reconnect_and_wait(
        server_name,
        srv,
        op_description=op_description,
        timeout=15,
    ):
        logger.warning(
            "MCP server '%s': reconnect did not ready within 15s after "
            "session-expired error; falling through to error response.",
            server_name,
        )
        return None

    try:
        result = retry_call()
        try:
            parsed = json.loads(result)
            if "error" not in parsed:
                _reset_server_error(server_name)
                return result
        except (json.JSONDecodeError, TypeError):
            _reset_server_error(server_name)
            return result
    except Exception as retry_exc:
        logger.warning(
            "MCP %s/%s retry after session reconnect failed: %s",
            server_name, op_description, retry_exc,
        )
    return None


# Exact MCP tool-name provenance. The generated registry name is lossy because
# provider-safe normalization maps punctuation to ``_``. Keep the raw server
# name captured at registration time so policy and capability checks never rely
# on parsing or re-sanitizing the generated name.
_mcp_tool_server_names: Dict[str, str] = {}

# Tools an MCP server registered and later took back, keyed by registry name,
# valued ``(raw_server_name, reason)``. An agent's tool snapshot is frozen after
# its opening turns (see ``agent/turn_context.py``), so a server that parks or
# disconnects leaves its tools in the model's schema; a call to one must then
# say *why* it can't run rather than "Unknown tool". Reasons:
#   ``reconnecting`` — parked/degraded; the self-probe may bring it back.
#   ``disconnected`` — the server task was shut down.
#   ``removed``      — the server no longer advertises this tool.
# Re-registering the name (revival, /reload-mcp) clears the entry.
_mcp_departed_tools: Dict[str, Tuple[str, str]] = {}

# Dedicated event loop running in a background daemon thread.
_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_thread: Optional[threading.Thread] = None

# Protects _mcp_loop, _mcp_thread, _servers, MCP connection status maps,
# _mcp_tool_server_names, _mcp_departed_tools, and _stdio_pids.
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Cross-process MCP discovery guard
# ---------------------------------------------------------------------------
# Advisory file lock that prevents N concurrent son-of-anton processes (e.g.
# gateway + TUI) from all running MCP discovery simultaneously.
_LOCK_UNAVAILABLE: Any = object()  # sentinel: locking broken/unavailable
_MCP_DISCOVERY_LOCK_PATH: Optional[str] = None  # resolved lazily

# Retry constants for the bounded wait when another process holds the lock.
_MCP_DISCOVERY_LOCK_MAX_RETRIES: int = 240
_MCP_DISCOVERY_LOCK_RETRY_DELAY_S: float = 0.5


class _LockCookie:
    """Holds a cross-process file lock; release() drops it.

    The fcntl lockdown is tied to the file-descriptor lifetime.  We keep the
    file object in ``_fh`` and close it on release.
    """

    def __init__(self, fh: Any) -> None:
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                fd = self._fh.fileno()
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def _acquire_lock_on_fh(fh: Any) -> bool:
    """Acquire a non-blocking exclusive lock on an open file handle.

    Uses ``fcntl.flock``.

    Returns ``True`` if the lock was acquired, ``False`` if another process
    holds it (non-blocking refusal).  Raises ``RuntimeError`` on unexpected
    errors so the caller can treat lock acquisition as unavailable.
    """
    fd = fh.fileno()
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            return False
        raise


def _try_acquire_mcp_discovery_lock() -> Any:
    """Try to acquire an exclusive cross-process lock for MCP discovery.

    Returns
    -------
    _LockCookie
        Lock acquired successfully.
    None
        Another process holds the lock (non-blocking refusal).
    _LOCK_UNAVAILABLE
        Locking mechanism is broken or unavailable -- caller should run
        discovery unguarded.
    """
    global _MCP_DISCOVERY_LOCK_PATH
    try:
        from son_of_anton_constants import get_son_of_anton_home
        if _MCP_DISCOVERY_LOCK_PATH is None:
            _MCP_DISCOVERY_LOCK_PATH = str(
                get_son_of_anton_home() / ".mcp-discovery.lock"
            )
        lock_path = _MCP_DISCOVERY_LOCK_PATH
    except Exception:
        return _LOCK_UNAVAILABLE

    try:
        fh = open(lock_path, "w", encoding="utf-8")
    except Exception:
        return _LOCK_UNAVAILABLE

    try:
        acquired = _acquire_lock_on_fh(fh)
    except Exception:
        fh.close()
        return _LOCK_UNAVAILABLE

    if acquired:
        return _LockCookie(fh)
    else:
        fh.close()
        return None


# PIDs of stdio MCP server subprocesses.  Tracked so we can force-kill
# them on shutdown if the graceful cleanup (SDK context-manager teardown)
# fails or times out.  PIDs are added after connection and removed on
# normal server shutdown.
_stdio_pids: Dict[int, str] = {}  # pid -> server_name

# PIDs that survived their session context exit (SDK teardown failed to
# terminate them).  These are detected in _run_stdio's finally block and
# can be cleaned up asynchronously by _kill_orphaned_mcp_children().
# Separate from _stdio_pids so cleanup sweeps never race with active
# sessions (e.g. concurrent cron jobs or live user chats).
_orphan_stdio_pids: set = set()
_orphan_stdio_pid_servers: Dict[int, str] = {}

# Process-group IDs of stdio MCP subprocesses, captured at spawn time.
# The MCP SDK spawns stdio children with ``start_new_session=True`` so each
# direct child becomes its own session/pgroup leader (PGID == its own PID).
# Grandchildren spawned by that child (e.g. a wrapper MCP server that itself
# launches helper subprocesses like ``claude mcp serve``) inherit that PGID
# unless they call ``setsid`` themselves.  When the direct child exits, those
# grandchildren reparent to init/systemd-user but keep the original PGID, so
# ``killpg(pgid, sig)`` still reaches them.  Tracked separately from
# ``_stdio_pids`` so we retain the PGID even after the direct child has
# exited and been removed from the active map.
_stdio_pgids: Dict[int, int] = {}  # pid -> pgid


def _snapshot_child_pids() -> set:
    """Return the set of current child process PIDs (from /proc).

    Used by _run_stdio to identify the subprocess spawned by stdio_client.
    """
    my_pid = os.getpid()
    try:
        children_path = f"/proc/{my_pid}/task/{my_pid}/children"
        with open(children_path, encoding="utf-8") as f:
            return {int(p) for p in f.read().split() if p.strip()}
    except (OSError, ValueError):
        return set()


# Non-MCP children that can race into the _snapshot_child_pids() delta
# during stdio MCP server spawn (LSP servers). Match argv markers instead of
# argv[0] because Java children begin with the binary path.
_NON_MCP_CHILD_CMDLINE_MARKERS: tuple[str, ...] = (
    "-dorg.eclipse.equinox.launcher",  # jdtls (legacy arg style)
    "eclipse.jdt.ls",
    "org.eclipse.equinox.launcher_",
)


def _filter_mcp_children(pids: set) -> set:
    """Remove non-MCP children from a PID snapshot delta.

    _snapshot_child_pids() returns *all* direct children. When a stdio MCP
    server spawns concurrently with an LSP server, the delta can include PIDs
    that are NOT the MCP server; tracking those in _stdio_pgids would let the
    shutdown sweep's killpg() reach them.
    """
    if not pids:
        return pids
    import psutil

    filtered: set = set()
    for pid in pids:
        try:
            argv = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            # Process raced away or is a zombie — skip it; it cannot be the
            # MCP server we just spawned and is not safe to track.
            continue
        if any(
            marker in arg
            for arg in argv[1:]
            for marker in _NON_MCP_CHILD_CMDLINE_MARKERS
        ):
            continue
        filtered.add(pid)
    return filtered


def _mcp_loop_exception_handler(loop, context):
    """Suppress benign 'Event loop is closed' noise during shutdown.

    When the MCP event loop is stopped and closed, httpx/httpcore async
    transports may fire __del__ finalizers that call call_soon() on the
    dead loop.  asyncio catches that RuntimeError and routes it here.
    We silence it because the connection is being torn down anyway; all
    other exceptions are forwarded to the default handler.
    """
    exc = context.get("exception")
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        return  # benign shutdown race — suppress
    loop.default_exception_handler(context)


def _ensure_mcp_loop():
    """Start the background event loop thread if not already running."""
    global _mcp_loop, _mcp_thread
    with _lock:
        if _mcp_loop is not None and _mcp_loop.is_running():
            return
        _mcp_loop = asyncio.new_event_loop()
        _mcp_loop.set_exception_handler(_mcp_loop_exception_handler)
        _mcp_thread = threading.Thread(
            target=_mcp_loop.run_forever,
            name="mcp-event-loop",
            daemon=True,
        )
        _mcp_thread.start()


def _wrap_with_home_override(coro: "Coroutine") -> "Coroutine":
    """Carry the caller's context-local SON_OF_ANTON_HOME override into ``coro``.

    Returns ``coro`` unchanged when no override is active. Otherwise wraps
    it so the override is set inside the coroutine's own (task-local)
    context on the MCP loop and reset when it completes — concurrent calls
    carrying different scopes don't interfere.
    """
    try:
        from son_of_anton_constants import (
            get_son_of_anton_home_override,
            reset_son_of_anton_home_override,
            set_son_of_anton_home_override,
        )

        home_override = get_son_of_anton_home_override()
    except Exception:
        return coro
    if not home_override:
        return coro

    async def _scoped():
        token = set_son_of_anton_home_override(home_override)
        try:
            return await coro
        finally:
            reset_son_of_anton_home_override(token)

    return _scoped()


def _run_on_mcp_loop(coro_or_factory, timeout: float = 30):
    """Schedule a coroutine on the MCP event loop and block until done.

    Accepts either a coroutine object or a zero-arg callable that returns one.
    Callers can pass a factory to avoid constructing coroutine objects when
    the MCP loop is unavailable (which would otherwise leak the coroutine
    frame and emit ``"coroutine was never awaited"`` warnings).

    Poll in short intervals so the calling agent thread can honor user
    interrupts while the MCP work is still running on the background loop.
    """
    from tools.interrupt import is_interrupted
    from agent.async_utils import safe_schedule_threadsafe

    with _lock:
        loop = _mcp_loop
    if loop is None or not loop.is_running():
        if asyncio.iscoroutine(coro_or_factory):
            coro_or_factory.close()
        raise RuntimeError("MCP event loop is not running")

    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory

    # Propagate the context-local SON_OF_ANTON_HOME override onto the MCP loop.
    # Tasks scheduled via run_coroutine_threadsafe are created INSIDE the
    # loop thread, so they copy the loop thread's context — not the
    # scheduling thread's, so any get_son_of_anton_home() resolution inside
    # the coroutine would read the process home instead of the scoped one.
    # Re-establish the override inside the task's own context (task-local —
    # concurrent calls carrying different scopes don't interfere). No-op when
    # no override is active.
    coro = _wrap_with_home_override(coro)

    future = safe_schedule_threadsafe(
        coro, loop,
        logger=logger,
        log_message="MCP scheduling failed",
    )
    if future is None:
        raise RuntimeError("MCP event loop unavailable (failed to schedule)")
    start_time = time.monotonic()
    deadline = None if timeout is None else start_time + timeout

    while True:
        if is_interrupted():
            future.cancel()
            raise InterruptedError("User sent a new message")

        wait_timeout = 0.1
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                elapsed = time.monotonic() - start_time
                raise TimeoutError(
                    f"MCP call timed out after {elapsed:.1f}s "
                    f"(configured timeout: {float(timeout):.1f}s)"
                )
            wait_timeout = min(wait_timeout, remaining)

        try:
            return future.result(timeout=wait_timeout)
        except concurrent.futures.TimeoutError:
            # On supported Python versions, concurrent.futures.TimeoutError
            # aliases the built-in TimeoutError, so result(timeout=...) also
            # raises it for a coroutine's own timeout.
            # Resolve a done future without a timeout to propagate its stored
            # outcome, including completion racing with this polling timeout.
            if future.done():
                return future.result()
            continue


def _interrupted_call_result() -> str:
    """Standardized JSON error for a user-interrupted MCP tool call."""
    return tool_error("MCP call interrupted: user sent a new message")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _interpolate_env_vars(value):
    """Recursively resolve ``${VAR}`` / ``${env:VAR}`` placeholders from the
    environment. Unset vars keep the literal placeholder.
    """
    from agent.secret_scope import get_secret as _get_secret

    if isinstance(value, str):
        def _replace(m):
            name = _env_ref_name(m.group(1))
            return _get_secret(name, m.group(0)) or m.group(0)
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env_vars(v) for v in value]
    return value


# (server_name, dotted key path) pairs already warned about — see
# _warn_hidden_whitespace(); config loads happen on every discovery pass.
_whitespace_warned: Set[Tuple[str, str]] = set()


def _warn_hidden_whitespace(server_name: str, config: dict) -> List[str]:
    """Warn about MCP config string values with hidden leading/trailing whitespace.

    A token pasted with a trailing newline or a URL copied with a leading
    space produces opaque auth/connect failures (the server rejects the
    credential, TLS/DNS fails on ``"example.com "``), and the whitespace is
    invisible when eyeballing config.toml.

    Advisory only — values are never mutated (whitespace could theoretically
    be intentional in an arg). Returns the list of dotted key paths flagged,
    for testability. Values themselves are never logged (they are often
    secrets); only the key path is named. Each (server, key path) is warned
    about once per process — ``_load_mcp_config()`` runs on every discovery/
    status call and repeating the warning would be noise.
    """
    flagged: List[str] = []

    def _walk(value: Any, path: str) -> None:
        if isinstance(value, str):
            if value != value.strip():
                flagged.append(path)
        elif isinstance(value, dict):
            for k, v in value.items():
                _walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _walk(v, f"{path}[{i}]")

    _walk(config, "")
    for key_path in flagged:
        dedupe_key = (server_name, key_path)
        if dedupe_key in _whitespace_warned:
            continue
        _whitespace_warned.add(dedupe_key)
        logger.warning(
            "MCP server '%s': config value '%s' has hidden leading or "
            "trailing whitespace — this often causes authentication or "
            "connection failures. Check for stray spaces/newlines in "
            "config.toml (or the referenced env var).",
            server_name,
            key_path,
        )
    return flagged


def _filter_suspicious_mcp_servers(servers: Dict[str, dict]) -> Dict[str, dict]:
    """Drop exfiltration-shaped MCP configs before any stdio spawn path."""
    from son_of_anton_cli.mcp_security import validate_mcp_server_entry as _validate_mcp_server_entry

    safe_servers = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            safe_servers[name] = cfg
            continue
        issues = _validate_mcp_server_entry(name, cfg)
        if issues:
            logger.warning(
                "Skipping suspicious MCP server '%s': %s",
                name,
                "; ".join(issues),
            )
            continue
        safe_servers[name] = cfg
    return safe_servers


def _load_mcp_config() -> Dict[str, dict]:
    """Read ``mcp_servers`` from the Son of Anton config file.

    Returns a dict of ``{server_name: server_config}`` or empty dict.
    Server config can contain either ``command``/``args``/``env`` for stdio
    transport or ``url``/``headers`` for HTTP transport, plus optional
    ``timeout`` and ``connect_timeout`` overrides.

    ``${ENV_VAR}`` placeholders in string values are resolved from
    ``os.environ`` (which includes ``~/.son-of-anton/.env`` loaded at startup).
    """
    try:
        from son_of_anton_cli.config import load_config
        from utils import env_var_enabled as _env_enabled

        if _env_enabled("SON_OF_ANTON_SAFE_MODE"):
            return {}
        config = load_config()
        servers = config.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = {}
        # Ensure .env vars are available for interpolation
        try:
            from son_of_anton_cli.env_loader import load_son_of_anton_dotenv
            load_son_of_anton_dotenv()
        except Exception:
            pass
        safe_servers: Dict[str, dict] = {}
        for name, cfg in _filter_suspicious_mcp_servers(servers).items():
            interpolated = _interpolate_env_vars(cfg)
            if isinstance(interpolated, dict):
                _warn_hidden_whitespace(name, interpolated)
                safe_servers[name] = interpolated
        return safe_servers
    except Exception as exc:
        logger.debug("Failed to load MCP config: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Server connection helper
# ---------------------------------------------------------------------------

async def _connect_server(name: str, config: dict) -> MCPServerTask:
    """Create an MCPServerTask, start it, and return when ready.

    The server Task keeps the connection alive in the background.
    Call ``server.shutdown()`` (on the same event loop) to tear it down.

    Raises:
        ValueError: if required config keys are missing.
        Exception: on connection or initialization failure.
    """
    server = MCPServerTask(name)
    claim = _connect_server_claim.get()
    claim_token = None
    if claim is not None:
        claim(server)
        # ``start()`` creates the long-lived run task by copying this context.
        # The ownership callback is only for this connection attempt; do not
        # retain its discovery closure for the server's lifetime.
        claim_token = _connect_server_claim.set(None)
    try:
        await server.start(config)
    except asyncio.CancelledError:
        # start() already cancels/reaps server._task on external cancellation
        # (see the comment there) -- awaiting a redundant shutdown() inside a
        # cancelled context would only risk swallowing the cancellation.
        raise
    except BaseException:
        # Discovery owns claimed tasks and decides whether a failed start is a
        # live recoverable park or a terminal failure. Standalone probes have
        # no revival owner, so they must reap their failed task locally.
        if claim is None:
            try:
                await server.shutdown()
            except Exception as shutdown_exc:  # noqa: BLE001 -- best-effort reap, don't mask the real error
                logger.debug(
                    "MCP server '%s' shutdown during orphan-reap failed: %s",
                    name, shutdown_exc,
                )
        raise
    finally:
        if claim_token is not None:
            _connect_server_claim.reset(claim_token)
    return server


# ---------------------------------------------------------------------------
# Handler / check-fn factories
# ---------------------------------------------------------------------------

def _get_connected_server_for_call(server_name: str) -> Optional[MCPServerTask]:
    """Return the live server task for *server_name*, if any."""
    with _lock:
        return _servers.get(server_name)


def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float):
    """Return a sync handler that calls an MCP tool via the background loop.

    The handler conforms to the registry's dispatch interface:
    ``handler(args_dict, **kwargs) -> str``
    """

    def _handler(args: dict, **kwargs) -> str:
        # Circuit breaker: if this server has failed too many times
        # consecutively, short-circuit with a clear message so the model
        # stops retrying and uses alternative approaches.
        #
        # Once the cooldown elapses, the breaker transitions to
        # half-open: we let the *next* call through as a probe. On
        # success the success-path below resets the breaker; on
        # failure the error paths below bump the count again, which
        # re-stamps the open-time via _bump_server_error (re-arming
        # the cooldown).
        if _server_error_counts.get(server_name, 0) >= _CIRCUIT_BREAKER_THRESHOLD:
            opened_at = _server_breaker_opened_at.get(server_name, 0.0)
            age = time.monotonic() - opened_at
            if age < _CIRCUIT_BREAKER_COOLDOWN_SEC:
                remaining = max(1, int(_CIRCUIT_BREAKER_COOLDOWN_SEC - age))
                return tool_error(
                    f"MCP server '{server_name}' is unreachable after "
                    f"{_server_error_counts[server_name]} consecutive "
                    f"failures. Auto-retry available in ~{remaining}s. "
                    f"Do NOT retry this tool yet — use alternative "
                    f"approaches or ask the user to check the MCP server."
                )
            # Cooldown elapsed → fall through as a half-open probe.

        server = _get_connected_server_for_call(server_name)
        if not server:
            _bump_server_error(server_name)
            return tool_error(f"MCP server '{server_name}' is not connected")

        if not server.session:
            # No live session. A reconnect may already be completing (the
            # transport swaps in a fresh session object asynchronously) —
            # wait briefly before treating this as a failure, so a
            # transient reconnect window doesn't burn a circuit-breaker
            # strike.
            if _wait_for_server_session_ready(
                server, timeout=min(5.0, float(tool_timeout or 5.0)),
            ):
                pass  # Fresh session arrived; proceed below.
            else:
                # Still down — the server task is reconnecting, or it has
                # exhausted its retry budget and parked (e.g. a dead stdio
                # subprocess). Probing here would write into a dead/absent
                # transport and re-arm the breaker forever. Instead,
                # ask the (always-present) server task to rebuild the
                # transport — which respawns a dead stdio subprocess — and
                # return a clean "reconnecting" error so the model backs off
                # without burning iterations. The breaker resets once the
                # fresh session initializes (_run_stdio/_run_http call
                # _reset_server_error).
                _bump_server_error(server_name)
                if _signal_reconnect(server):
                    return tool_error(
                        f"MCP server '{server_name}' transport is down; "
                        f"reconnect requested. Do NOT retry this tool "
                        f"immediately — give it a few seconds to come back."
                    )
                return tool_error(f"MCP server '{server_name}' is not connected")

        async def _call():
            async with server._rpc_lock:
                # Snapshot the agent's context so an elicitation callback
                # triggered during this call (fired on the MCP recv loop
                # task, which doesn't inherit our contextvars) can replay
                # it and detect the gateway platform / session for routing.
                server._pending_call_context = contextvars.copy_context()
                try:
                    result = await server.session.call_tool(tool_name, arguments=args)
                finally:
                    server._pending_call_context = None
            # The RPC round-trip completed — the session is demonstrably
            # healthy at the transport level (even if the tool itself
            # returned is_error). Clear the rapid-drop budget.
            server._mark_session_proven()
            if getattr(result, "is_error", False):
                error_text = ""
                for block in (result.content or []):
                    if getattr(block, "text", None):
                        error_text += block.text
                        continue
                    # EmbeddedResource blocks inside error payloads carry
                    # their text under .resource.text.
                    res_text = getattr(getattr(block, "resource", None), "text", None)
                    if res_text:
                        error_text += str(res_text)
                return tool_error(_sanitize_error(
                    _truncate_mcp_text_result(
                        error_text or "MCP tool returned an error"
                    )
                ))

            # Collect text from content blocks. Image/audio blocks are cached
            # via the gateway's cache helpers so they flow through the MEDIA:
            # tag convention and out to Signal.
            parts: List[str] = []
            for block in (result.content or []):
                if hasattr(block, "text") and block.text:
                    parts.append(strip_unicode_tags(block.text))
                    continue
                image_tag = _cache_mcp_image_block(block)
                if image_tag:
                    parts.append(image_tag)
                    continue
                audio_tag = _cache_mcp_audio_block(block)
                if audio_tag:
                    parts.append(audio_tag)
                    continue
                # ResourceLink / EmbeddedResource blocks (PDFs, archives,
                # office docs, ...).
                resource_text = _render_mcp_resource_block(block, server_name)
                if resource_text:
                    parts.append(resource_text)
                    continue
                # Benign empty renders (empty text blocks, empty text
                # resources, audio in a process without the gateway cache)
                # aren't data loss — log at debug. Warn only for genuinely
                # unrecognized block shapes.
                block_type = getattr(block, "type", None) or type(block).__name__
                if block_type in {"text", "resource", "audio", "image"}:
                    logger.debug(
                        "MCP %s: content block type %r rendered empty",
                        server_name, block_type,
                    )
                else:
                    logger.warning(
                        "MCP %s: dropping unsupported content block type %r",
                        server_name, block_type,
                    )
            text_result = "\n".join(parts) if parts else ""

            # Hard-cap pathological payloads before they propagate;
            # ordinary large results pass untouched to the spillover layer.
            text_result = _truncate_mcp_text_result(text_result)

            # Combine content + structuredContent when both are present.
            # MCP spec: content is model-oriented (text), structuredContent
            # is machine-oriented (JSON metadata).  For an AI agent, content
            # is the primary payload; structuredContent supplements it.
            #
            # Server-level `_meta` is also surfaced: servers return namespaced
            # metadata there (validated contracts, handoff payloads, ...).
            # Protocol-reserved keys (host/protocol plumbing, not model-facing
            # data) are dropped first — see _is_reserved_mcp_meta_key.
            structured = getattr(result, "structured_content", None)
            # Cap structuredContent too — a malicious server could flood
            # context via a multi-MB JSON payload. When the
            # serialized form exceeds the hard cap, replace it with the
            # truncated string (head + tail preserved) so it degrades
            # gracefully instead of flooding downstream.
            if structured is not None:
                try:
                    _structured_json = json.dumps(structured, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    _structured_json = None
                if _structured_json is not None and len(_structured_json) > _MCP_HARD_RESULT_CAP_CHARS:
                    structured = _truncate_mcp_text_result(_structured_json)
            meta = _strip_reserved_meta_keys(getattr(result, "meta", None))
            if structured is not None or meta is not None:
                payload: Dict[str, Any] = {}
                if text_result:
                    payload["result"] = text_result
                if structured is not None:
                    if text_result:
                        payload["structuredContent"] = structured
                    else:
                        payload["result"] = structured
                if meta is not None:
                    payload["_meta"] = meta
                if "result" not in payload:
                    payload["result"] = text_result
                try:
                    return json.dumps(payload, ensure_ascii=False)
                except (TypeError, ValueError):
                    # Non-serializable metadata: drop the extras rather than
                    # failing the whole tool call.
                    return json.dumps({"result": text_result}, ensure_ascii=False)
            return json.dumps({"result": text_result}, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            result = _call_once()
            # Check if the MCP tool itself returned an error
            try:
                parsed = json.loads(result)
                if "error" in parsed:
                    _bump_server_error(server_name)
                else:
                    _reset_server_error(server_name)  # success — reset
            except (json.JSONDecodeError, TypeError):
                _reset_server_error(server_name)  # non-JSON = success
            return result
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            # Transport session expiry: reconnect and retry once.
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once,
                f"tools/call {tool_name}",
            )
            if recovered is not None:
                return recovered

            _bump_server_error(server_name)
            logger.error(
                "MCP tool %s/%s call failed: %s",
                server_name, tool_name, exc,
            )
            return tool_error(_sanitize_error(
                f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
            ))

    return _handler


def _make_list_resources_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that lists resources from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _get_connected_server_for_call(server_name)
        if not server or not server.session:
            return tool_error(f"MCP server '{server_name}' is not connected")

        async def _call():
            async with server._rpc_lock:
                all_resources = await _paginate_full_list(
                    server.session.list_resources, "resources", server_name
                )
            resources = []
            for r in all_resources:
                entry = {}
                if hasattr(r, "uri"):
                    entry["uri"] = str(r.uri)
                if hasattr(r, "name"):
                    entry["name"] = r.name
                if hasattr(r, "description") and r.description:
                    entry["description"] = r.description
                # Key stays camelCase — this dict is the tool's own JSON
                # output shape, not an SDK model.
                _mime = getattr(r, "mime_type", None)
                if _mime:
                    entry["mimeType"] = _mime
                resources.append(entry)
            return json.dumps({"resources": resources}, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "resources/list",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/list_resources failed: %s", server_name, exc,
            )
            return tool_error(_sanitize_error(
                f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
            ))

    return _handler


def _make_read_resource_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that reads a resource by URI from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _get_connected_server_for_call(server_name)
        if not server or not server.session:
            return tool_error(f"MCP server '{server_name}' is not connected")

        uri = args.get("uri")
        if not uri:
            return tool_error("Missing required parameter 'uri'")

        async def _call():
            async with server._rpc_lock:
                result = await server.session.read_resource(uri)
            # read_resource returns ReadResourceResult with .contents list
            parts: List[str] = []
            contents = result.contents if hasattr(result, "contents") else []
            for block in contents:
                if getattr(block, "text", None) is not None:
                    parts.append(strip_unicode_tags(block.text))
                elif getattr(block, "blob", None) is not None:
                    # Materialize binary resource contents into the document
                    # cache instead of discarding them (same contract as
                    # EmbeddedResource blocks in tool results).
                    rendered = _render_mcp_resource_block(
                        SimpleNamespace(type="resource", resource=block),
                        server_name,
                    )
                    parts.append(rendered or f"[binary data, {len(block.blob)} bytes]")
            return json.dumps({"result": "\n".join(parts) if parts else ""}, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "resources/read",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/read_resource failed: %s", server_name, exc,
            )
            return tool_error(_sanitize_error(
                f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
            ))

    return _handler


def _make_list_prompts_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that lists prompts from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _get_connected_server_for_call(server_name)
        if not server or not server.session:
            return tool_error(f"MCP server '{server_name}' is not connected")

        async def _call():
            async with server._rpc_lock:
                all_prompts = await _paginate_full_list(
                    server.session.list_prompts, "prompts", server_name
                )
            prompts = []
            for p in all_prompts:
                entry = {}
                if hasattr(p, "name"):
                    entry["name"] = p.name
                if hasattr(p, "description") and p.description:
                    entry["description"] = p.description
                if hasattr(p, "arguments") and p.arguments:
                    entry["arguments"] = [
                        {
                            "name": a.name,
                            **({"description": a.description} if hasattr(a, "description") and a.description else {}),
                            **({"required": a.required} if hasattr(a, "required") else {}),
                        }
                        for a in p.arguments
                    ]
                prompts.append(entry)
            return json.dumps({"prompts": prompts}, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "prompts/list",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/list_prompts failed: %s", server_name, exc,
            )
            return tool_error(_sanitize_error(
                f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
            ))

    return _handler


def _make_get_prompt_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that gets a prompt by name from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _get_connected_server_for_call(server_name)
        if not server or not server.session:
            return tool_error(f"MCP server '{server_name}' is not connected")

        name = args.get("name")
        if not name:
            return tool_error("Missing required parameter 'name'")
        arguments = args.get("arguments", {})

        async def _call():
            async with server._rpc_lock:
                result = await server.session.get_prompt(name, arguments=arguments)
            # GetPromptResult has .messages list
            messages = []
            for msg in (result.messages if hasattr(result, "messages") else []):
                entry = {}
                if hasattr(msg, "role"):
                    entry["role"] = msg.role
                if hasattr(msg, "content"):
                    content = msg.content
                    if hasattr(content, "text"):
                        entry["content"] = strip_unicode_tags(content.text)
                    elif isinstance(content, str):
                        entry["content"] = strip_unicode_tags(content)
                    else:
                        entry["content"] = strip_unicode_tags(str(content))
                messages.append(entry)
            resp = {"messages": messages}
            if hasattr(result, "description") and result.description:
                resp["description"] = result.description
            return json.dumps(resp, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "prompts/get",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/get_prompt failed: %s", server_name, exc,
            )
            return tool_error(_sanitize_error(
                f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
            ))

    return _handler


def _make_check_fn(server_name: str):
    """Return a check function that verifies the MCP connection is alive."""

    def _check() -> bool:
        with _lock:
            server = _servers.get(server_name)
            return server is not None and server.session is not None

    return _check


# ---------------------------------------------------------------------------
# Discovery & registration
# ---------------------------------------------------------------------------

def _normalize_mcp_input_schema(schema: dict | None) -> dict:
    """Normalize MCP input schemas for LLM tool-calling compatibility.

    Repairs applied recursively, in order:

    * Legacy ``definitions`` / ``#/definitions/...`` refs are promoted to
      ``$defs`` / ``#/$defs/...`` (some backends accept only the latter).
    * Nullable unions (``anyOf: [{...}, {"type": "null"}]``) collapse to the
      non-null branch with a ``nullable: true`` hint, and unions of same-typed
      consts collapse to an ``enum`` (tools.schema_sanitizer).
    * Missing or ``null`` ``type`` on an object-shaped node is coerced to
      ``"object"``; an ``object`` node lacking ``properties`` gets an empty
      dict; ``required`` is pruned to names that exist in ``properties``.
    """
    from tools.schema_sanitizer import collapse_const_unions, strip_nullable_unions

    if not schema:
        return {"type": "object", "properties": {}}

    def _rewrite_local_refs(node):
        """Walk the schema, promoting legacy ``definitions`` to ``$defs``.

        ``definitions`` is renamed only as a JSON Schema *meta-keyword*, never
        as the *name of a property* (a key inside ``properties`` /
        ``patternProperties``): providers reject ``$`` in property names, so
        rewriting a tool parameter named ``definitions`` would 400 the whole
        tools array. Property schemas are still recursed into.
        """
        if isinstance(node, dict):
            normalized = {}
            for key, value in node.items():
                if key in ("properties", "patternProperties") and isinstance(value, dict):
                    normalized[key] = {
                        prop_name: _rewrite_local_refs(prop_schema)
                        for prop_name, prop_schema in value.items()
                    }
                else:
                    out_key = "$defs" if key == "definitions" else key
                    normalized[out_key] = _rewrite_local_refs(value)
            ref = normalized.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/definitions/"):
                normalized["$ref"] = "#/$defs/" + ref[len("#/definitions/"):]
            return normalized
        if isinstance(node, list):
            return [_rewrite_local_refs(item) for item in node]
        return node

    def _repair_object_shape(node):
        """Recursively repair object-shaped nodes: fill type, prune required."""
        if isinstance(node, list):
            return [_repair_object_shape(item) for item in node]
        if not isinstance(node, dict):
            return node

        repaired = {k: _repair_object_shape(v) for k, v in node.items()}

        # Coerce missing / null type when the shape is clearly an object
        # (has properties or required but no type).
        if not repaired.get("type") and (
            "properties" in repaired or "required" in repaired
        ):
            repaired["type"] = "object"

        if repaired.get("type") == "object":
            if not isinstance(repaired.get("properties"), dict):
                repaired["properties"] = {}

            # Prune required to only include names that exist in properties
            required = repaired.get("required")
            if isinstance(required, list):
                props = repaired.get("properties") or {}
                valid = [r for r in required if isinstance(r, str) and r in props]
                if len(valid) != len(required):
                    if valid:
                        repaired["required"] = valid
                    else:
                        repaired.pop("required", None)

        return repaired

    normalized = _rewrite_local_refs(schema)
    normalized = strip_nullable_unions(normalized)
    normalized = collapse_const_unions(normalized)
    normalized = _repair_object_shape(normalized)

    # Ensure top-level is a well-formed object schema
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized = {**normalized, "properties": {}}

    return normalized


def sanitize_mcp_name_component(value: str) -> str:
    """Return an MCP name component safe for tool and prefix generation.

    Preserves Son of Anton's historical behavior of converting hyphens to
    underscores, and also replaces any other character outside
    ``[A-Za-z0-9_]`` with ``_`` so generated tool names are compatible with
    provider validation rules.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


# Native MCP tool-name prefix. Son of Anton uses the ``mcp__<server>__<tool>``
# convention shared by Claude Code, Codex, and OpenCode (anomalyco/opencode
# #33533). The double-underscore delimiter disambiguates the server/tool
# boundary even when either component contains underscores, and matches the
# naming models are trained on.
MCP_TOOL_NAME_PREFIX = "mcp__"
_MCP_NAME_DELIM = "__"


def mcp_prefixed_tool_name(server_name: str, tool_name: str) -> str:
    """Build the registry/wire name for an MCP tool.

    Produces ``mcp__<sanitizedServer>__<sanitizedTool>``.
    """
    safe_server = sanitize_mcp_name_component(server_name)
    safe_tool = sanitize_mcp_name_component(tool_name)
    return f"{MCP_TOOL_NAME_PREFIX}{safe_server}{_MCP_NAME_DELIM}{safe_tool}"


def _convert_mcp_schema(server_name: str, mcp_tool) -> dict:
    """Convert an MCP tool listing to the Son of Anton registry schema format.

    Args:
        server_name: The logical server name for prefixing.
        mcp_tool:    An MCP ``Tool`` object with ``.name``, ``.description``,
                     and ``.input_schema`` (``.inputSchema`` before mcp 2.0).

    Returns:
        A dict suitable for ``registry.register(schema=...)``.
    """
    prefixed_name = mcp_prefixed_tool_name(server_name, mcp_tool.name)
    return {
        "name": prefixed_name,
        "description": strip_unicode_tags(
            mcp_tool.description or f"MCP tool {mcp_tool.name} from {server_name}"
        ),
        "parameters": _normalize_mcp_input_schema(
            getattr(mcp_tool, "input_schema", None)
        ),
    }


def _build_utility_schemas(server_name: str) -> List[dict]:
    """Build schemas for the MCP utility tools (resources & prompts).

    Returns a list of (schema, handler_factory_name) tuples encoded as dicts
    with keys: schema, handler_key.
    """
    return [
        {
            "schema": {
                "name": mcp_prefixed_tool_name(server_name, "list_resources"),
                "description": f"List available resources from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            "handler_key": "list_resources",
        },
        {
            "schema": {
                "name": mcp_prefixed_tool_name(server_name, "read_resource"),
                "description": f"Read a resource by URI from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uri": {
                            "type": "string",
                            "description": "URI of the resource to read",
                        },
                    },
                    "required": ["uri"],
                },
            },
            "handler_key": "read_resource",
        },
        {
            "schema": {
                "name": mcp_prefixed_tool_name(server_name, "list_prompts"),
                "description": f"List available prompts from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            "handler_key": "list_prompts",
        },
        {
            "schema": {
                "name": mcp_prefixed_tool_name(server_name, "get_prompt"),
                "description": f"Get a prompt by name from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the prompt to retrieve",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Optional arguments to pass to the prompt",
                            "properties": {},
                            "additionalProperties": True,
                        },
                    },
                    "required": ["name"],
                },
            },
            "handler_key": "get_prompt",
        },
    ]


def _normalize_name_filter(value: Any, label: str) -> set[str]:
    """Normalize include/exclude config to a set of tool-name patterns.

    Entries may be exact tool names or fnmatch-style globs
    (``*_radar_*``, ``get_zones_*``). Matching happens in
    :func:`matches_name_filter`.
    """
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value}
    logger.warning("MCP config %s must be a string or list of strings; ignoring %r", label, value)
    return set()


def matches_name_filter(tool_name: str, patterns: set[str]) -> bool:
    """True if ``tool_name`` matches any entry in ``patterns``.

    Exact names match literally; entries containing fnmatch metacharacters
    (``*``, ``?``, ``[``) match as case-sensitive globs — the same pattern
    semantics as ``approvals.deny``. Exact membership is checked first so
    large literal lists stay O(1).
    """
    if not patterns:
        return False
    if tool_name in patterns:
        return True
    return any(
        fnmatch.fnmatchcase(tool_name, p)
        for p in patterns
        if "*" in p or "?" in p or "[" in p
    )


def _parse_boolish(value: Any, default: bool = True) -> bool:
    """Parse a bool-like config value with safe fallback."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    logger.warning("MCP config expected a boolean-ish value, got %r; using default=%s", value, default)
    return default


_UTILITY_CAPABILITY_METHODS = {
    "list_resources": "list_resources",
    "read_resource": "read_resource",
    "list_prompts": "list_prompts",
    "get_prompt": "get_prompt",
}

# Maps each utility handler to the MCP capability key that must be non-None
# on the server's ``initialize`` response for the handler to be registered.
# Source of truth: MCP spec — capabilities.resources / capabilities.prompts
# are present on the response only when the server actually implements
# those request families. Without this gate, tools-only servers (e.g.
# Context7 @upstash/context7-mcp, which advertises only ``tools``) had
# all four utility stubs registered and every model call to them came
# back with JSON-RPC ``-32601 Method not found``, which made the model
# conclude the server was broken even when the real tools worked. See
# #18051.
_UTILITY_CAPABILITY_ATTRS = {
    "list_resources": "resources",
    "read_resource": "resources",
    "list_prompts": "prompts",
    "get_prompt": "prompts",
}


def _track_mcp_tool_server(tool_name: str, server_name: str) -> None:
    """Remember the exact raw MCP server that registered *tool_name*."""
    with _lock:
        _mcp_tool_server_names[tool_name] = server_name
        _mcp_departed_tools.pop(tool_name, None)


def _forget_mcp_tool_server(tool_name: str, reason: str = "removed") -> None:
    """Forget MCP server provenance for a deregistered tool.

    Remembers which server the tool came from and why it left (see
    ``_mcp_departed_tools``) so a call from a frozen tool snapshot gets an
    actionable error instead of "Unknown tool".
    """
    with _lock:
        server_name = _mcp_tool_server_names.pop(tool_name, None)
        if server_name is not None:
            _mcp_departed_tools[tool_name] = (server_name, reason)


def departed_mcp_tool_error(tool_name: str) -> Optional[str]:
    """Explain why a once-registered MCP tool can't be called right now.

    Returns ``None`` when *tool_name* is registered or was never an MCP tool,
    so callers fall through to normal dispatch / "Unknown tool".
    """
    with _lock:
        if tool_name in _mcp_tool_server_names:
            return None
        departed = _mcp_departed_tools.get(tool_name)
    if departed is None:
        return None
    server_name, reason = departed
    if reason == "reconnecting":
        return (
            f"MCP server '{server_name}' is currently unavailable "
            "(reconnecting); try again later or continue without it."
        )
    if reason == "disconnected":
        return (
            f"MCP server '{server_name}' is disconnected; its tools are "
            "unavailable until it reconnects (/reload-mcp). Continue without it."
        )
    return (
        f"MCP server '{server_name}' no longer provides '{tool_name}'; "
        "continue without it (the tool list updates on /reload-mcp or in a "
        "new session)."
    )


def _select_utility_schemas(server_name: str, server: MCPServerTask, config: dict) -> List[dict]:
    """Select utility schemas based on config and server capabilities."""
    tools_filter = config.get("tools") or {}
    resources_enabled = _parse_boolish(tools_filter.get("resources"), default=True)
    prompts_enabled = _parse_boolish(tools_filter.get("prompts"), default=True)

    # ``initialize_result.capabilities`` is the source of truth: its sub-objects
    # (``resources``, ``prompts``) are non-None iff the server advertises that
    # request family. ``hasattr(server.session, ...)`` was the old gate but
    # ClientSession always has the four method attributes defined on the class,
    # so it never filtered anything.
    advertised_caps = None
    init_result = getattr(server, "initialize_result", None)
    if init_result is not None:
        advertised_caps = getattr(init_result, "capabilities", None)

    selected: List[dict] = []
    for entry in _build_utility_schemas(server_name):
        handler_key = entry["handler_key"]
        if handler_key in {"list_resources", "read_resource"} and not resources_enabled:
            logger.debug("MCP server '%s': skipping utility '%s' (resources disabled)", server_name, handler_key)
            continue
        if handler_key in {"list_prompts", "get_prompt"} and not prompts_enabled:
            logger.debug("MCP server '%s': skipping utility '%s' (prompts disabled)", server_name, handler_key)
            continue

        # Preferred gate: check the server's advertised capabilities. Skip
        # if the capability is explicitly not advertised.
        if advertised_caps is not None:
            cap_attr = _UTILITY_CAPABILITY_ATTRS[handler_key]
            if getattr(advertised_caps, cap_attr, None) is None:
                logger.debug(
                    "MCP server '%s': skipping utility '%s' "
                    "(server does not advertise '%s' capability)",
                    server_name,
                    handler_key,
                    cap_attr,
                )
                continue
        else:
            # Legacy fallback for test fixtures or older code paths where
            # initialize_result wasn't captured. Preserves the old behavior
            # of registering every stub in that case rather than regressing
            # any server that was working before this fix.
            required_method = _UTILITY_CAPABILITY_METHODS[handler_key]
            if not hasattr(server.session, required_method):
                logger.debug(
                    "MCP server '%s': skipping utility '%s' (session lacks %s)",
                    server_name,
                    handler_key,
                    required_method,
                )
                continue
        selected.append(entry)
    return selected


def _existing_tool_names() -> List[str]:
    """Return tool names for all currently connected servers."""
    names: List[str] = []
    for _sname, server in _servers.items():
        if hasattr(server, "_registered_tool_names"):
            names.extend(server._registered_tool_names)
            continue
        for mcp_tool in server._tools:
            schema = _convert_mcp_schema(server.name, mcp_tool)
            names.append(schema["name"])
    return names


def _register_server_tools(name: str, server: MCPServerTask, config: dict) -> List[str]:
    """Register tools from an already-connected server into the registry.

    Handles include/exclude filtering and utility tools. Toolset resolution
    for ``mcp-{server}`` and raw server-name aliases is derived from the live
    registry, rather than mutating ``toolsets.TOOLSETS`` at runtime.

    Lossy provider-safe name normalization can map distinct raw names to the
    same registry name (for example ``read-file`` and ``read_file``). Such
    collisions fail closed: every ambiguous entry is skipped rather than
    selecting an arbitrary handler.

    Used by both initial discovery and dynamic refresh (list_changed).

    Returns:
        List of registered prefixed tool names.
    """
    from tools.registry import registry

    registered_names: List[str] = []
    toolset_name = f"mcp-{name}"

    # Selective tool loading: honour include/exclude lists from config.
    # Rules (matching issue #690 spec, extended with glob support):
    #   tools.include — whitelist: only matching tool names are registered
    #   tools.exclude — blacklist: all tools EXCEPT matching ones are registered
    #   entries may be exact names or fnmatch globs (e.g. "*_radar_*")
    #   include takes precedence over exclude
    #   Neither set → register all tools (backward-compatible default)
    tools_filter = config.get("tools") or {}
    include_set = _normalize_name_filter(
        tools_filter.get("include"), f"mcp_servers.{name}.tools.include"
    )
    exclude_set = _normalize_name_filter(
        tools_filter.get("exclude"), f"mcp_servers.{name}.tools.exclude"
    )

    def _should_register(tool_name: str) -> bool:
        if include_set:
            return matches_name_filter(tool_name, include_set)
        if exclude_set:
            return not matches_name_filter(tool_name, exclude_set)
        return True

    check_fn = _make_check_fn(name)
    candidates: List[dict] = []

    for mcp_tool in server._tools:
        if not _should_register(mcp_tool.name):
            logger.debug(
                "MCP server '%s': skipping tool '%s' (filtered by config)",
                name,
                mcp_tool.name,
            )
            continue

        _scan_mcp_description(name, mcp_tool.name, mcp_tool.description or "")
        schema = _convert_mcp_schema(name, mcp_tool)
        candidates.append(
            {
                "registry_name": schema["name"],
                "origin": f"tool {mcp_tool.name!r}",
                "schema": schema,
                "handler": _make_tool_handler(
                    name, mcp_tool.name, server.tool_timeout
                ),
                "check_fn": check_fn,
            }
        )

    # Generated resource/prompt utility tools share the same namespace as raw
    # MCP tools, so they must participate in the same collision preflight.
    handler_factories = {
        "list_resources": _make_list_resources_handler,
        "read_resource": _make_read_resource_handler,
        "list_prompts": _make_list_prompts_handler,
        "get_prompt": _make_get_prompt_handler,
    }
    for entry in _select_utility_schemas(name, server, config):
        schema = entry["schema"]
        handler_key = entry["handler_key"]
        candidates.append(
            {
                "registry_name": schema["name"],
                "origin": f"generated utility {handler_key!r}",
                "schema": schema,
                "handler": handler_factories[handler_key](
                    name, server.tool_timeout
                ),
                "check_fn": check_fn,
            }
        )

    # Exact duplicate rows from a server are harmless but should not inflate
    # counts. Distinct origins that collapse to one normalized name are unsafe.
    unique_candidates: List[dict] = []
    seen_candidates: set[tuple[str, str]] = set()
    origins_by_name: Dict[str, set[str]] = {}
    for candidate in candidates:
        key = (candidate["registry_name"], candidate["origin"])
        if key in seen_candidates:
            logger.debug(
                "MCP server '%s': duplicate registration candidate %s for '%s'; "
                "keeping one",
                name,
                candidate["origin"],
                candidate["registry_name"],
            )
            continue
        seen_candidates.add(key)
        unique_candidates.append(candidate)
        origins_by_name.setdefault(candidate["registry_name"], set()).add(
            candidate["origin"]
        )

    # A generated resource/prompt utility that normalizes onto a server-native
    # tool's name must not knock that native tool out of the registry: the
    # native tool is the capability the user connected the server for, while the
    # generated utility (read_resource/list_resources/list_prompts/get_prompt)
    # is optional sugar that only matters when the server exposes no such tool
    # of its own (#87112). Resolve that specific collision in favour of the
    # native tool — keep it, drop the shadowed utility — and fall back to the
    # conservative skip-everything only for genuinely ambiguous collisions (two
    # or more native tools normalizing to one name, which we cannot
    # disambiguate). The four utility keys are distinct, so a colliding set
    # holds at most one utility origin.
    ambiguous_names: Dict[str, List[str]] = {}
    shadowed_utilities: set[tuple[str, str]] = set()
    for registry_name, origins in origins_by_name.items():
        if len(origins) <= 1:
            continue
        utility_origins = sorted(
            o for o in origins if o.startswith("generated utility ")
        )
        native_origins = sorted(origins - set(utility_origins))
        if len(native_origins) == 1 and utility_origins:
            for util_origin in utility_origins:
                shadowed_utilities.add((registry_name, util_origin))
            logger.info(
                "MCP server '%s': generated utility %s normalizes onto "
                "server-native %s — keeping the native tool and dropping the "
                "utility (the utility only applies when the server has no such "
                "tool of its own)",
                name,
                ", ".join(utility_origins),
                native_origins[0],
            )
            continue
        ambiguous_names[registry_name] = sorted(origins)

    for registry_name, origins in sorted(ambiguous_names.items()):
        logger.error(
            "MCP server '%s': name normalization collision for '%s' from %s; "
            "skipping every colliding entry instead of choosing an arbitrary "
            "handler",
            name,
            registry_name,
            ", ".join(origins),
        )

    for candidate in unique_candidates:
        registry_name = candidate["registry_name"]
        if registry_name in ambiguous_names:
            continue
        if (registry_name, candidate["origin"]) in shadowed_utilities:
            continue

        existing_toolset = registry.get_toolset_for_tool(registry_name)
        if existing_toolset and existing_toolset != toolset_name:
            if existing_toolset.startswith("mcp-"):
                logger.error(
                    "MCP server '%s': %s normalizes to '%s', already owned by "
                    "MCP toolset '%s' — skipping to preserve the existing owner",
                    name,
                    candidate["origin"],
                    registry_name,
                    existing_toolset,
                )
            else:
                logger.warning(
                    "MCP server '%s': %s (→ '%s') collides with built-in tool "
                    "in toolset '%s' — skipping to preserve built-in",
                    name,
                    candidate["origin"],
                    registry_name,
                    existing_toolset,
                )
            continue

        registry.register(
            name=registry_name,
            toolset=toolset_name,
            schema=candidate["schema"],
            handler=candidate["handler"],
            check_fn=candidate["check_fn"],
            is_async=False,
            description=candidate["schema"]["description"],
        )

        # The pre-check above is advisory only. Multiple servers connect in
        # parallel, so ToolRegistry.register() is the atomic ownership gate.
        if registry.get_toolset_for_tool(registry_name) != toolset_name:
            logger.error(
                "MCP server '%s': registration of %s as '%s' was rejected by "
                "the registry; skipping provenance/count updates",
                name,
                candidate["origin"],
                registry_name,
            )
            continue

        _track_mcp_tool_server(registry_name, name)
        registered_names.append(registry_name)

    if registered_names:
        registry.register_toolset_alias(name, toolset_name)

    return registered_names


async def _discover_and_register_server(name: str, config: dict) -> List[str]:
    """Connect to a single MCP server, discover tools, and register them.

    Returns list of registered tool names.
    """
    connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
    # List-based claim (not a ``nonlocal`` rebind): the claim callback runs
    # inside ``_connect_server`` while this frame is suspended, and appending
    # keeps type narrowing intact for the module's other ``server`` locals.
    claimed: List[MCPServerTask] = []

    def _claim_server(created: MCPServerTask) -> None:
        claimed.append(created)

    claim_token = _connect_server_claim.set(_claim_server)
    try:
        server = await asyncio.wait_for(
            _connect_server(name, config),
            timeout=connect_timeout,
        )
    except BaseException:
        server = claimed[0] if claimed else None
        task = server._task if server is not None else None
        task_cancelling = (
            task.cancelling()
            if task is not None and hasattr(task, "cancelling")
            else 0
        )
        if (
            server is not None
            and server._error is not None
            and task is not None
            and not task.done()
            and not task_cancelling
        ):
            # Recoverable park: the run task deliberately stays alive to
            # self-probe, so adopt it into the registry for shutdown/revival.
            with _lock:
                _servers[name] = server
        elif server is not None:
            await server.shutdown()
        raise
    finally:
        _connect_server_claim.reset(claim_token)

    with _lock:
        _server_connecting.discard(name)
        _server_connect_errors.pop(name, None)
        _servers[name] = server

    registered_names = _register_server_tools(name, server, config)
    server._registered_tool_names = list(registered_names)

    transport_type = "HTTP" if "url" in config else "stdio"
    logger.info(
        "MCP server '%s' (%s): registered %d tool(s): %s",
        name, transport_type, len(registered_names),
        ", ".join(registered_names),
    )
    return registered_names


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_mcp_servers(servers: Dict[str, dict]) -> List[str]:
    """Connect to explicit MCP servers and register their tools.

    Idempotent for already-connected server names. Servers with
    ``enabled: false`` are skipped without disconnecting existing sessions.

    Args:
        servers: Mapping of ``{server_name: server_config}``.

    Returns:
        List of all currently registered MCP tool names.
    """
    _ensure_mcp_sdk()
    servers = _filter_suspicious_mcp_servers(servers)
    if not servers:
        logger.debug("No explicit MCP servers provided")
        return []

    # Only attempt servers that aren't already connected (or currently
    # connecting) and are enabled.  Checking ``_server_connecting`` prevents
    # duplicate subprocess spawns when ``discover_mcp_tools()`` is called
    # from multiple entry-points before the first batch finishes (#58862).
    with _lock:
        connecting = set(_server_connecting)
        new_servers = {
            k: v
            for k, v in servers.items()
            if k not in _servers
            and k not in connecting
            and _parse_boolish(v.get("enabled", True), default=True)
            # Skip a server still serving its post-failure backoff. Without
            # this, a server that fails to connect (and is therefore never
            # recorded in ``_servers``) would be re-spawned on every worker
            # session's discovery pass -- the #50394 restart storm. The
            # cooldown is cleared automatically on the next successful
            # connect or by a manual /mcp refresh.
            and not _connect_cooldown_active(k)
        }
        # Cached entries with no live session are parked or mid-reconnect.
        # Their tools are deregistered, so nothing else can reach
        # _signal_reconnect — without this nudge a new session silently
        # waits up to _PARKED_RETRY_INTERVAL for the next self-probe
        # (#50170). Wake them now so their tools come back promptly.
        stale_cached = [
            _servers[k]
            for k in servers
            if k in _servers and getattr(_servers[k], "session", None) is None
        ]
        _server_connecting.update(new_servers)
        for srv_name in new_servers:
            _server_connect_errors.pop(srv_name, None)

    for srv in stale_cached:
        _signal_reconnect(srv)

    if not new_servers:
        return _existing_tool_names()

    # Start the background event loop for MCP connections
    _ensure_mcp_loop()

    async def _discover_one(name: str, cfg: dict) -> List[str]:
        """Connect to a single server and return its registered tool names."""
        return await _discover_and_register_server(name, cfg)

    async def _discover_all():
        server_names = list(new_servers.keys())
        # Connect to all servers in PARALLEL
        results = await asyncio.gather(
            *(_discover_one(name, cfg) for name, cfg in new_servers.items()),
            return_exceptions=True,
        )
        for name, result in zip(server_names, results):
            if isinstance(result, BaseException):
                command = new_servers.get(name, {}).get("command")
                message = _format_connect_error(result)
                with _lock:
                    _server_connecting.discard(name)
                    _server_connect_errors[name] = message
                    # Arm the per-server backoff so the next discovery pass
                    # doesn't immediately re-spawn this failing server
                    # (#50394). Isolated to this server -- healthy servers
                    # in the same batch are unaffected.
                    _record_connect_failure(name)
                logger.warning(
                    "Failed to connect to MCP server '%s'%s: %s",
                    name,
                    f" (command={command})" if command else "",
                    message,
                )
            else:
                with _lock:
                    _server_connecting.discard(name)
                    _server_connect_errors.pop(name, None)
                    _clear_connect_failure(name)

    # Per-server timeouts are handled inside _discover_and_register_server.
    # The outer timeout is generous: 120s total for parallel discovery.
    #
    # Temporarily clear the interrupt flag on the current thread so that MCP
    # discovery is never cancelled by a stale interrupt from a prior agent
    # session (executor threads get reused and may carry old interrupt state).
    from tools.interrupt import is_interrupted as _is_interrupted, set_interrupt as _set_interrupt
    _was_interrupted = _is_interrupted()
    if _was_interrupted:
        _set_interrupt(False)
    try:
        _run_on_mcp_loop(_discover_all, timeout=120)
    except (TimeoutError, InterruptedError) as _e:
        # When the outer timeout fires or the user interrupts,
        # _discover_all's gather may not have finished, leaving
        # entries stranded in _server_connecting.  Those stale
        # entries would block future reconnection attempts (#58862).
        with _lock:
            stale = [n for n in new_servers if n in _server_connecting]
            if stale:
                logger.warning(
                    "MCP discovery %s while %d server(s) were still "
                    "connecting; clearing stale connecting set: %s",
                    "timed out" if isinstance(_e, TimeoutError) else "interrupted",
                    len(stale),
                    ", ".join(stale),
                )
                _server_connecting.difference_update(stale)
                for _sn in stale:
                    _server_connect_errors.setdefault(
                        _sn,
                        f"Connection attempt {'timed out' if isinstance(_e, TimeoutError) else 'interrupted'} during discovery",
                    )
        raise
    finally:
        if _was_interrupted:
            _set_interrupt(True)

    # Log a summary so ACP callers get visibility into what was registered.
    with _lock:
        connected = [
            n
            for n in new_servers
            if n in _servers and n not in _server_connect_errors
        ]
        new_tool_count = sum(
            len(getattr(_servers[n], "_registered_tool_names", []))
            for n in connected
        )
    failed = len(new_servers) - len(connected)
    if new_tool_count or failed:
        summary = f"MCP: registered {new_tool_count} tool(s) from {len(connected)} server(s)"
        if failed:
            summary += f" ({failed} failed)"
        logger.info(summary)

    return _existing_tool_names()


def discover_mcp_tools() -> List[str]:
    """Entry point: load config, connect to MCP servers, register tools.

    Called from ``model_tools`` after ``discover_builtin_tools()``.

    Idempotent for already-connected servers. If some servers failed on a
    previous call, only the missing ones are retried.

    Returns:
        List of all registered MCP tool names.
    """
    servers = _load_mcp_config()
    if not servers:
        logger.debug("No MCP servers configured")
        return []

    # SDK import is deferred to HERE so a config with zero MCP servers (the
    # default) never pays the ~260ms `mcp` import on CLI startup.
    _ensure_mcp_sdk()

    # Cross-process discovery guard (#62771). A lock loser waits for
    # the holder, then performs its own process-local discovery. If locking is
    # unavailable or the bounded wait expires, preserve the previous
    # fail-soft behavior by running discovery unguarded.
    cookie = _try_acquire_mcp_discovery_lock()
    if cookie is None:
        logger.debug(
            "Another process holds MCP discovery lock -- retrying with backoff"
        )
        for _ in range(_MCP_DISCOVERY_LOCK_MAX_RETRIES):
            time.sleep(_MCP_DISCOVERY_LOCK_RETRY_DELAY_S)
            cookie = _try_acquire_mcp_discovery_lock()
            if cookie is not None:
                break

        if cookie is None:
            logger.warning(
                "MCP discovery lock still held after %d retries -- "
                "running discovery unguarded",
                _MCP_DISCOVERY_LOCK_MAX_RETRIES,
            )
        elif cookie is not _LOCK_UNAVAILABLE:
            logger.debug("Retry succeeded -- acquired MCP discovery lock")

    try:
        with _lock:
            connecting = set(_server_connecting)
            new_server_names = [
                name
                for name, cfg in servers.items()
                if name not in _servers
                and name not in connecting
                and _parse_boolish(cfg.get("enabled", True), default=True)
            ]

        tool_names = register_mcp_servers(servers)
        if not new_server_names:
            return tool_names

        with _lock:
            connected_server_names = [
                name
                for name in new_server_names
                if name in _servers and name not in _server_connect_errors
            ]
            new_tool_count = sum(
                len(getattr(_servers[name], "_registered_tool_names", []))
                for name in connected_server_names
            )

        failed_count = len(new_server_names) - len(connected_server_names)
        if new_tool_count or failed_count:
            summary = f"  MCP: {new_tool_count} tool(s) from {len(connected_server_names)} server(s)"
            if failed_count:
                summary += f" ({failed_count} failed)"
            logger.info(summary)

        return tool_names

    finally:
        if cookie not in (None, _LOCK_UNAVAILABLE):
            cookie.release()

def get_mcp_status() -> List[dict]:
    """Return status of all configured MCP servers for banner display.

    Returns a list of dicts with keys: name, transport, tools, connected,
    disabled, and status. Includes connected servers, disabled servers,
    in-flight connection attempts, recorded failures, and servers that are
    configured but have not been started in this process yet.
    """
    result: List[dict] = []

    # Get configured servers from config
    configured = _load_mcp_config()
    if not configured:
        return result

    with _lock:
        active_servers = dict(_servers)
        connecting = set(_server_connecting)
        connect_errors = dict(_server_connect_errors)

    for name, cfg in configured.items():
        transport = cfg.get("transport", "http") if "url" in cfg else "stdio"
        enabled = _parse_boolish(cfg.get("enabled", True), default=True)
        server = active_servers.get(name)
        if server and server.session is not None:
            entry = {
                "name": name,
                "transport": transport,
                "tools": len(server._registered_tool_names) if hasattr(server, "_registered_tool_names") else len(server._tools),
                "connected": True,
                "disabled": False,
                "status": "connected",
            }
            if server._sampling:
                entry["sampling"] = dict(server._sampling.metrics)
            result.append(entry)
        elif not enabled:
            # A server with enabled: false is intentionally not connected — it is
            # disabled, not failed. Surface that distinction so consumers (banner,
            # TUI) can render "disabled" rather than an alarming "failed".
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": True,
                "status": "disabled",
            })
        elif name in connecting:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": False,
                "status": "connecting",
            })
        elif name in connect_errors:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": False,
                "status": "failed",
                "error": connect_errors[name],
            })
        else:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": False,
                "status": "configured",
            })

    return result


def probe_mcp_server_tools() -> Dict[str, List[tuple]]:
    """Temporarily connect to configured MCP servers and list their tools.

    Designed for ``son-of-anton tools`` interactive configuration — connects to each
    enabled server, grabs tool names and descriptions, then disconnects.
    Does NOT register tools in the Son of Anton registry.

    Returns:
        Dict mapping server name to list of (tool_name, description) tuples.
        Servers that fail to connect are omitted from the result.
    """
    _ensure_mcp_sdk()
    servers_config = _load_mcp_config()
    if not servers_config:
        return {}

    enabled = {
        k: v for k, v in servers_config.items()
        if _parse_boolish(v.get("enabled", True), default=True)
    }
    if not enabled:
        return {}

    _ensure_mcp_loop()

    result: Dict[str, List[tuple]] = {}
    probed_servers: List[MCPServerTask] = []

    async def _probe_all():
        names = list(enabled.keys())
        coros = []
        for name, cfg in enabled.items():
            ct = cfg.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
            coros.append(asyncio.wait_for(_connect_server(name, cfg), timeout=ct))

        outcomes = await asyncio.gather(*coros, return_exceptions=True)

        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, Exception):
                logger.debug("Probe: failed to connect to '%s': %s", name, outcome)
                continue
            probed_servers.append(outcome)
            tools = []
            for t in outcome._tools:
                desc = getattr(t, "description", "") or ""
                tools.append((t.name, desc))
            result[name] = tools

        # Shut down all probed connections
        await asyncio.gather(
            *(s.shutdown() for s in probed_servers),
            return_exceptions=True,
        )

    try:
        _run_on_mcp_loop(_probe_all, timeout=120)
    except Exception as exc:
        logger.debug("MCP probe failed: %s", exc)
    finally:
        _stop_mcp_loop_if_idle()

    return result


# Serializes in-place mutation of an agent's tool snapshot.  /reload-mcp (CLI
# and gateway) and the opening-turns between-turns refresh all swap
# ``agent.tools`` / ``agent.valid_tool_names`` after the agent was built; the
# agent's run loop reads those during tool iteration, so a concurrent write
# mid-read could otherwise expose a half-updated list.
_agent_tools_lock = threading.Lock()


def has_registered_mcp_tools() -> bool:
    """True if any MCP server has actually registered tools into the registry.

    Cheap — checks the global MCP-tool→server name map under ``_lock``, no
    registry walk.  Used by the per-turn refresh hook so a session with no MCP
    tools (the common case, and also a connected-but-zero-tool/prompt-only
    server) skips the ``get_tool_definitions`` rebuild entirely.  Checks
    registered TOOLS, not connected servers, so a server that registers no tools
    doesn't keep the hook firing every turn.
    """
    with _lock:
        return bool(_mcp_tool_server_names)


def get_registered_mcp_server_names() -> set:
    """Return the set of MCP server names that have actually registered at
    least one tool into the registry (post-connection, post check_fn/include-
    exclude filtering) -- i.e. the real, availability-filtered signal, not
    just what's present in config.toml under ``mcp_servers``.

    Used by capability-aware prompt building (e.g. gateway/session.py's
    Slack platform note) to detect an MCP server that provides a given
    platform's capability regardless of what its config key is named.
    """
    with _lock:
        return set(_mcp_tool_server_names.values())


def refresh_agent_mcp_tools(
    agent,
    *,
    enabled_override=None,
    disabled_override=None,
    quiet_mode: bool = True,
    additive_only: bool = False,
) -> set:
    """Re-derive an already-built agent's tool snapshot from the live registry.

    The agent snapshots ``agent.tools`` once at build time and never re-reads
    the registry (see ``run_agent`` / ``agent_init``).  When MCP servers connect
    *after* that snapshot — a slow HTTP/OAuth server that misses the bounded
    startup wait, or a ``/reload-mcp`` — their tools are invisible until the
    snapshot is rebuilt.  This is the single shared rebuild used by every such
    caller (CLI/TUI ``/reload-mcp``, the gateway reload, and the opening-turns
    between-turns refresh in ``agent/turn_context.py``) so they can't drift
    apart again.

    ``additive_only=True`` (the automatic between-turns path) never removes or
    reorders anything: tools absent from the live registry (a parked server)
    stay in the snapshot, and only names the snapshot lacks are appended.

    The rebuild respects the agent's own ``enabled_toolsets`` /
    ``disabled_toolsets`` (the same filtering it was built with) and diffs by
    tool **name** (not count — a count compare misses an equal-size add/remove
    swap).

    Crucially it is **additive-preserving**: ``get_tool_definitions`` returns
    only the registry-derived tools, but ``agent_init`` appends two further
    families directly onto ``agent.tools`` *after* that — external
    memory-provider tools (mem0/honcho/…) and context-engine tools
    (``lcm_*``).  A naive ``agent.tools = get_tool_definitions(...)`` would
    silently DELETE those.  So after rebuilding the registry set we re-run the
    same post-build injectors ``agent_init`` used, reconstructing the full
    surface.  The new ``(tools, valid_tool_names)`` pair is published together
    under ``_agent_tools_lock`` so a concurrent reader never sees a
    cross-attribute half-swap.

    Returns the set of newly-added tool names (empty when nothing changed), so
    callers can decide whether to notify the user / re-emit session info.  The
    caller owns the prompt-cache contract: this helper does NOT check turn state,
    because each caller has a different policy (``/reload-mcp`` rebuilds after
    explicit user consent; the between-turns path only runs, additively, during
    a session's opening turns — any change to ``tools=`` re-prefills the whole
    conversation, so it must never fire on an established conversation).
    """
    from model_tools import get_tool_definitions
    from tools.registry import registry

    # Explicit reloads (/reload-mcp) pass freshly-resolved toolsets so a server
    # the user just ENABLED in config is picked up; the agent's stored selection
    # is then updated to match. The automatic between-turns path
    # passes nothing and reuses the agent's build-time selection unchanged.
    if enabled_override is not None or disabled_override is not None:
        enabled = enabled_override if enabled_override is not None else getattr(agent, "enabled_toolsets", None)
        disabled = disabled_override if disabled_override is not None else getattr(agent, "disabled_toolsets", None)
        agent.enabled_toolsets = enabled
        agent.disabled_toolsets = disabled
    else:
        enabled = getattr(agent, "enabled_toolsets", None)
        disabled = getattr(agent, "disabled_toolsets", None)

    # Capture the registry generation this rebuild is derived from BEFORE the
    # (potentially slow) get_tool_definitions call. Used at publish time to
    # reject a stale write: if two callers race (e.g. /reload-mcp and the
    # between-turns prologue around turn 1), a slower caller that
    # computed an OLDER set must not clobber a newer set another caller already
    # published. ``registry._generation`` bumps on every (de)register.
    snapshot_generation = registry._generation

    # Registry-derived tools (built-ins + MCP), filtered to the agent's toolsets.
    # Computed OUTSIDE the lock (get_tool_definitions can be slow); the diff and
    # publish below happen together in ONE critical section so two concurrent
    # callers can't torn-publish or compute overlapping ``added`` sets.
    new_defs = list(
        get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=quiet_mode,
        )
        or []
    )
    new_names = {t["function"]["name"] for t in new_defs}

    # Single atomic read-diff-publish so the returned ``added`` is consistent
    # with what was actually published, even under concurrent callers, and a
    # stale (older-generation) rebuild can't overwrite a newer published one.
    with _agent_tools_lock:
        # Defensive: the published generation should be an int, but tolerate an
        # agent that never set it (or set a non-int, e.g. a test mock) rather
        # than throwing TypeError on the comparison and silently failing the
        # whole refresh.
        published_gen_raw = getattr(agent, "_tool_snapshot_generation", -1)
        published_gen = published_gen_raw if isinstance(published_gen_raw, int) else -1
        if snapshot_generation < published_gen:
            # A newer snapshot already won; our set is stale — drop it.
            return set()
        current = {
            t["function"]["name"]
            for t in (getattr(agent, "tools", None) or [])
        }
        if additive_only:
            added_defs = [
                t for t in new_defs if t["function"]["name"] not in current
            ]
            agent._tool_snapshot_generation = max(published_gen, snapshot_generation)
            if not added_defs:
                return set()
            added_names = {t["function"]["name"] for t in added_defs}
            # New list objects (never in-place appends) so a concurrent reader
            # holding the old list sees a consistent snapshot.
            agent.tools = list(getattr(agent, "tools", None) or []) + added_defs
            agent.valid_tool_names = (
                set(getattr(agent, "valid_tool_names", None) or current)
                | added_names
            )
            return added_names
        if new_names == current:
            # No change → leave the live snapshot untouched (no churn), but
            # record the generation so an in-flight older caller can't clobber.
            agent._tool_snapshot_generation = max(published_gen, snapshot_generation)
            return set()
        agent.tools = new_defs
        agent.valid_tool_names = new_names
        agent._tool_snapshot_generation = max(published_gen, snapshot_generation)
        return new_names - current


def pending_mcp_tool_names(agent) -> set:
    """MCP tools the registry offers that *agent*'s frozen snapshot lacks.

    Respects the agent's own toolset selection (a restricted gateway session
    never reports tools it could not get from /reload-mcp either). Empty when
    the snapshot carries the tool-search bridge: ``tool_search``/``tool_call``
    read the LIVE catalog, so new deferred tools are already reachable. Cheap
    when nothing is pending: the expensive ``get_tool_definitions`` only runs
    once some registered MCP name is missing from the snapshot.
    """
    from tools.tool_search import BRIDGE_TOOL_NAMES

    have = set(getattr(agent, "valid_tool_names", None) or ())
    if have & BRIDGE_TOOL_NAMES:
        return set()
    with _lock:
        registered = set(_mcp_tool_server_names)
    if not (registered - have):
        return set()
    from model_tools import get_tool_definitions

    defs = get_tool_definitions(
        enabled_toolsets=getattr(agent, "enabled_toolsets", None),
        disabled_toolsets=getattr(agent, "disabled_toolsets", None),
        quiet_mode=True,
        skip_tool_search_assembly=True,
    ) or []
    offered = {t["function"]["name"] for t in defs}
    return (offered & registered) - have


def shutdown_mcp_servers():
    """Close all MCP server connections and stop the background loop.

    Each server Task is signalled to exit its ``async with`` block so that
    the anyio cancel-scope cleanup happens in the same Task that opened it.
    All servers are shut down in parallel via ``asyncio.gather``.
    """
    with _lock:
        servers_snapshot = list(_servers.values())

    # Fast path: nothing to shut down. The connect-cooldown maps can still
    # be populated here — a server that failed to connect is never recorded
    # in ``_servers`` (that is the very premise of the #50394 cooldown), so
    # "no live servers" is the MOST likely state in which stale backoff
    # entries exist. Clear them so a post-shutdown restart re-attempts every
    # configured server immediately.
    if not servers_snapshot:
        with _lock:
            _server_connect_retry_after.clear()
            _server_connect_failures.clear()
        _stop_mcp_loop()
        return

    async def _shutdown():
        results = await asyncio.gather(
            *(server.shutdown() for server in servers_snapshot),
            return_exceptions=True,
        )
        for server, result in zip(servers_snapshot, results):
            if isinstance(result, Exception):
                logger.debug(
                    "Error closing MCP server '%s': %s", server.name, result,
                )
        with _lock:
            _servers.clear()
            # Drop connect-retry cooldowns too: a full shutdown/restart
            # should re-attempt every server immediately, not honour a
            # stale per-server backoff from before the restart (#50394).
            _server_connect_retry_after.clear()
            _server_connect_failures.clear()

    with _lock:
        loop = _mcp_loop
    if loop is not None and loop.is_running():
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(
            _shutdown(), loop,
            logger=logger,
            log_message="MCP shutdown: failed to schedule",
        )
        if future is not None:
            try:
                future.result(timeout=15)
            except BaseException as exc:
                logger.debug("Error during MCP shutdown: %s", exc)

    # Unconditional final sweep: whether the async ``_shutdown`` ran,
    # timed out, or was never scheduled (loop already stopped), a full
    # shutdown must leave no stale connect-cooldown state behind — the
    # next start should re-attempt every server immediately (#50394).
    with _lock:
        _server_connect_retry_after.clear()
        _server_connect_failures.clear()

    _stop_mcp_loop()


def _kill_orphaned_mcp_children(
    include_active: bool = False,
    server_name: Optional[str] = None,
) -> None:
    """Best-effort graceful shutdown of stdio MCP subprocesses to reap orphans.

    Orphans are PIDs that survived their session context exit (SDK teardown
    did not terminate the process — common on Linux when stdio children escape
    the parent cgroup on cancellation). By default only entries in
    ``_orphan_stdio_pids`` are reaped so concurrent cron jobs and live user
    sessions are not disrupted.

    Sends SIGTERM, waits 2 seconds, then escalates to SIGKILL for any
    survivors, avoiding shared-resource collisions when multiple son-of-anton
    processes run on the same host (each has its own ``_stdio_pids`` dict).

    Signals are sent via ``os.killpg`` to the spawn-time pgid when one is
    tracked, so reparented grandchildren in the same process group
    (e.g. ``claude mcp serve`` spawned by a stdio MCP wrapper that exited
    first) are reaped alongside the direct child.  Falls back to ``os.kill``
    when no pgid is recorded.

    When ``server_name`` is set, only orphaned PIDs known to belong to that
    MCP server are reaped. This lets stdio reconnects clean up their previous
    transport without touching unrelated servers.

    With ``include_active=True`` also kills every PID in ``_stdio_pids`` —
    used only at final shutdown, after the MCP event loop has stopped and no
    sessions can still be in flight.
    """
    import signal as _signal

    with _lock:
        pids: Dict[int, str] = {}
        for opid in _orphan_stdio_pids:
            owner = _orphan_stdio_pid_servers.get(opid, "orphan")
            if server_name is not None and owner != server_name:
                continue
            pids[opid] = owner
        for opid in pids:
            _orphan_stdio_pids.discard(opid)
            _orphan_stdio_pid_servers.pop(opid, None)
        if include_active:
            active = dict(_stdio_pids)
            if server_name is not None:
                active = {
                    pid: owner
                    for pid, owner in active.items()
                    if owner == server_name
                }
            pids.update(active)
            for pid in active:
                _stdio_pids.pop(pid, None)
        # Snapshot pgids for the pids we're about to kill, then drop the
        # entries so a future spawn can't collide with stale state.
        pgids: Dict[int, int] = {pid: _stdio_pgids[pid] for pid in pids if pid in _stdio_pgids}
        for pid in pgids:
            _stdio_pgids.pop(pid, None)

    # Fast path: no tracked stdio PIDs to reap. Skip the SIGTERM/sleep/SIGKILL
    # dance entirely — otherwise every MCP-free shutdown pays a 2s sleep tax.
    if not pids:
        return

    # Pre-compute the gateway's own pgid so _send_signal can avoid killing it.
    try:
        _my_pgid = os.getpgrp()
    except OSError:
        _my_pgid = None  # restricted environment

    def _send_signal(pid: int, sig: int, server_name: str) -> None:
        """SIGTERM/SIGKILL via pgroup, fall back to pid signal."""
        pgid = pgids.get(pid)
        killpg = getattr(os, "killpg", None)
        if pgid is not None and killpg is not None:
            if _my_pgid is not None and pgid == _my_pgid:
                # The MCP child shares the gateway's own process group.
                # Using killpg would deliver the signal to the gateway as
                # well, crashing it (see #47134).  Fall through to the
                # per-pid kill() path instead. Warn because per-pid kill
                # cannot reach grandchildren in this shared group — if the
                # direct child has already exited, they may leak (inherent:
                # group-killing them would also kill the gateway).
                logger.warning(
                    "MCP server '%s' pgid %d matches gateway pgid; skipping "
                    "killpg to avoid self-kill and using per-pid kill — any "
                    "grandchildren in this group may not be reaped",
                    server_name, pgid,
                )
            else:
                try:
                    killpg(pgid, sig)
                    return
                except (ProcessLookupError, PermissionError, OSError) as exc:
                    # Pgroup gone (all members exited) or refused — fall back to
                    # the per-pid path so we still try the direct child if alive.
                    logger.debug(
                        "killpg(%d, %d) failed for MCP server '%s': %s; falling back to kill(pid)",
                        pgid, sig, server_name, exc,
                    )
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # Phase 1: SIGTERM (graceful)
    for pid, server_name in pids.items():
        _send_signal(pid, _signal.SIGTERM, server_name)
        logger.debug("Sent SIGTERM to orphaned MCP process %d (%s)", pid, server_name)

    # Phase 2: Wait for graceful exit
    time.sleep(2)

    # Phase 3: SIGKILL any survivors
    _sigkill = _signal.SIGKILL
    # Use the shared existence check before escalating to SIGKILL.
    from gateway.status import _pid_exists
    for pid, server_name in pids.items():
        if not _pid_exists(pid):
            continue  # Good — exited after SIGTERM
        _send_signal(pid, _sigkill, server_name)
        logger.warning(
            "Force-killed MCP process %d (%s) after SIGTERM timeout",
            pid, server_name,
        )


def _stop_mcp_loop_if_idle() -> bool:
    """Stop the MCP loop only when no registered server still owns it.

    Probe paths create temporary MCPServerTask instances that are not placed in
    ``_servers``.  They should clean up an otherwise-idle loop, but must not
    tear down the process-global loop when live agent tools are registered on
    it.  Otherwise a dashboard/CLI probe can make later MCP tool calls fail
    with ``MCP event loop is not running``.
    """
    return _stop_mcp_loop(only_if_idle=True)


async def _drain_mcp_loop_tasks(
    *,
    timeout: float = _MCP_LOOP_DRAIN_TIMEOUT,
) -> None:
    """Cancel every task still pending on the MCP loop and reap it.

    Cancelling is not enough on its own: ``Task.cancel()`` only schedules the
    throw, so tasks need a cancellation cycle before the loop goes away. Wait
    for them here — on their owning loop — but keep the final drain bounded so
    a task that suppresses cancellation cannot hang process exit indefinitely.
    """
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if not pending:
        return
    logger.debug("Draining %d pending task(s) from the MCP loop", len(pending))
    for task in pending:
        task.cancel()

    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in done:
        if task.cancelled():
            continue
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Pending MCP loop task ended during shutdown: %s", exc)

    if still_pending:
        logger.warning(
            "%d MCP loop task(s) still pending after %.1fs drain",
            len(still_pending), timeout,
        )


async def _drain_and_stop_mcp_loop() -> None:
    """Drain pending tasks, then stop the loop from its owning thread.

    Keeping both operations in one loop-owned sequence matters when the caller
    times out waiting for a blocked loop. Queuing ``loop.stop`` separately from
    the caller can overtake the scheduled drain before it receives a loop cycle,
    leaving the drain coroutine itself pending when the loop is closed.
    """
    loop = asyncio.get_running_loop()
    try:
        await _drain_mcp_loop_tasks(timeout=_MCP_LOOP_DRAIN_TIMEOUT)
    finally:
        loop.call_soon(loop.stop)


def _stop_mcp_loop(*, only_if_idle: bool = False) -> bool:
    """Stop the background event loop and join its thread."""
    global _mcp_loop, _mcp_thread
    with _lock:
        if only_if_idle and (_servers or _server_connecting):
            logger.debug("Leaving MCP event loop running; active servers are registered or connecting")
            return False
        loop = _mcp_loop
        thread = _mcp_thread
        _mcp_loop = None
        _mcp_thread = None
    if loop is not None:
        # Drain before stopping: closing the loop with tasks still suspended
        # leaves their coroutines for the GC, whose finalizer then resumes them
        # to run cleanup against a loop that is already closed -> "Event loop
        # is closed" (#60197). ``shutdown_mcp_servers`` only reaps servers held
        # in ``_servers``, so anything else left on this loop ends up here.
        stop_owned_by_loop = False
        if loop.is_running():
            from agent.async_utils import safe_schedule_threadsafe

            future = safe_schedule_threadsafe(
                _drain_and_stop_mcp_loop(), loop,
                logger=logger,
                log_message="MCP loop drain: failed to schedule",
                log_level=logging.WARNING,
            )
            if future is not None:
                stop_owned_by_loop = True
                try:
                    future.result(timeout=_MCP_LOOP_DRAIN_TIMEOUT + 1)
                except TimeoutError:
                    logger.warning(
                        "Timed out waiting for MCP loop drain after %.1fs",
                        _MCP_LOOP_DRAIN_TIMEOUT + 1,
                    )
                except BaseException as exc:
                    logger.warning("Error draining MCP loop tasks: %s", exc)
        elif not loop.is_closed():
            try:
                loop.run_until_complete(
                    _drain_mcp_loop_tasks(timeout=_MCP_LOOP_DRAIN_TIMEOUT)
                )
            except BaseException as exc:
                logger.warning("Error draining stopped MCP loop tasks: %s", exc)

        if not stop_owned_by_loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("MCP event loop thread did not stop within 5.0s")
        try:
            loop.close()
        except Exception as exc:
            logger.warning("Unable to close MCP event loop cleanly: %s", exc)
        # After closing the loop, any stdio subprocesses that survived the
        # graceful shutdown are now orphaned — include active PIDs too
        # since the loop is gone and no session can still be in flight.
        _kill_orphaned_mcp_children(include_active=True)
    return True
