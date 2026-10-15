"""The relay tool: a messaging chat hands project work to terminal sessions.

Service-gated (footprint rung 3): present only in a gateway process whose live
runner has ``gateway.single_user`` — never in the CLI/TUI, and fixed for a
chat's lifetime because the flag is settled at gateway boot. One tool with an
``action`` parameter, since it is sent on every call of the relay chat. The
work happens in gateway/relay_workers.py on the gateway's event loop; this
module is the schema and the thread-to-loop bridge.

The schema description is the relay's whole instruction set.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys

from tools.registry import no_cache_check_fn, registry, tool_error

_ACTION_TIMEOUT_S = 120


def _live_runner():
    """The running GatewayRunner, without importing the gateway into the CLI."""
    module = sys.modules.get("gateway.run")
    if module is None:
        return None
    try:
        return module._gateway_runner_ref()
    except Exception:
        return None


def _single_user_runner():
    runner = _live_runner()
    if runner is None:
        return None
    # Read from the runner: startup clears the flag when the allowlists name
    # more than one person (_enforce_single_user_allowlist).
    if not getattr(getattr(runner, "config", None), "single_user", False):
        return None
    return runner


@no_cache_check_fn
def check_relay_requirements() -> bool:
    return _single_user_runner() is not None


def relay_tool(args: dict, **_kw) -> str:
    runner = _single_user_runner()
    if runner is None:
        return tool_error("The relay works only in a single_user gateway.")
    from gateway.session_context import get_session_env

    chat_key = str(get_session_env("SON_OF_ANTON_SESSION_KEY", "") or "").strip()
    if not chat_key:
        return tool_error("This call has no chat to relay for.")
    loop = getattr(runner, "_gateway_loop", None)
    if loop is None or loop.is_closed():
        return tool_error("The gateway event loop is not running.")
    try:
        if asyncio.get_running_loop() is loop:
            return tool_error("The relay cannot run on the gateway loop thread.")
    except RuntimeError:
        pass
    from gateway.relay_workers import handle_relay_action

    future = asyncio.run_coroutine_threadsafe(
        handle_relay_action(runner, dict(args or {}), chat_key=chat_key), loop,
    )
    try:
        result = future.result(timeout=_ACTION_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return tool_error("The relay action timed out.")
    if isinstance(result, dict) and result.get("error"):
        return tool_error(result["error"])
    return json.dumps(result, ensure_ascii=False, default=str)


RELAY_SCHEMA = {
    "name": "relay",
    "description": (
        "This chat is a relay to the user's terminal coding sessions. Project "
        "work — code, files, commands in a repository — is done by a worker "
        "session that runs in its own directory, never in this chat: hand it "
        "over with this tool and report back.\n\n"
        "Actions:\n"
        "- list: terminal sessions, most recent first (optional `workspace` "
        "path filter), each marked bound to this chat and/or running.\n"
        "- send: give `message` to a session. `session` (id or title) defaults "
        "to the one bound to this chat; `new_in` (a directory) starts a new "
        "session there instead. A busy session queues the message and runs it "
        "after its current turn.\n"
        "- status: running or idle, recent activity, queued messages, pending "
        "approvals.\n"
        "- stop: stop the running worker; the session stays bound.\n"
        "- detach: unbind the session from this chat.\n\n"
        "Relay the user's words faithfully: pass their request as they gave "
        "it, without rewriting it or adding instructions of your own. Before "
        "choosing between continuing an existing session and starting a new "
        "one, ask the user. When a worker finishes, its final reply arrives "
        "here as a notification; tell the user the result briefly. A worker's "
        "approval requests also arrive here, and the user answers them with "
        "/approve or /deny."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "send", "status", "stop", "detach"],
            },
            "session": {
                "type": "string",
                "description": "Session id or title. Default: the session bound to this chat.",
            },
            "message": {
                "type": "string",
                "description": "send: the user's request for the worker, as they gave it.",
            },
            "new_in": {
                "type": "string",
                "description": "send: start a new session in this directory instead.",
            },
            "workspace": {
                "type": "string",
                "description": "list: only sessions in this directory or below it.",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="relay",
    toolset="relay",
    schema=RELAY_SCHEMA,
    handler=relay_tool,
    check_fn=check_relay_requirements,
)
