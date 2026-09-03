"""MCP tools for the physics modes' Research Manager.

The physics modes have their own agent loop and their own tool executors, so
none of the main agent's MCP machinery reaches them: an Autophysicist run could
compute anything and look up nothing. That is the wrong shape for research. A
Research Manager deciding how to attack a problem wants the literature, and a
sub-agent about to write scikit-learn wants the current API rather than the one
it remembers.

What this deliberately does NOT do:

*Give every tool to the agent.* The aggregating gateway this points at exposes
25 tools, 19 of them arXiv. The Manager's whole budget is a handful of tools and
fifteen calls an iteration; 25 schemas in every request would crowd out the
problem statement and invite the model to go browsing. ``allow`` is required,
not optional — with nothing allowed, MCP stays off.

*Hold a session open.* One ``asyncio.run`` per call, with the tool list fetched
once per run and cached. A persistent session would need a background thread and
a lifecycle, to save a handshake on a call the Manager makes a few times per
iteration, inside a loop where a single LLM turn takes tens of seconds.

*Reach the sandbox.* These are called by the agent process. Computations still
run with no network at all.
"""

from __future__ import annotations

import asyncio
import os
import time
import warnings
from typing import Any

DEFAULT_TIMEOUT = 120.0

#: Default per-role allowlists, used when ``physics.mcp.roles`` is absent.
#:
#: Named tools, not server prefixes. ``"arxiv"`` matches all nineteen tools
#: that server exposes — including topic watches, alert checks, a reindexer and
#: four LaTeX-source readers, which are a human's library-management workflow —
#: and putting nineteen schemas in front of an agent with a fifteen-call budget
#: is the crowding the allowlist exists to prevent.
#:
#: The Manager gets reading tools only: find, triage, fetch, read, search
#: within. It does not get ``context7``, because it does not write code — it
#: writes the brief, and the sub-agent that writes the code has its own
#: documentation lookup.
_ARXIV_READING = (
    "arxiv-search_papers",
    "arxiv-get_abstract",
    "arxiv-download_paper",
    "arxiv-read_paper",
    "arxiv-search_paper_text",
)

DEFAULT_ROLES: dict[str, tuple[str, ...]] = {
    "manager": _ARXIV_READING,
    "subagent": ("context7",),
}

#: Keys from the flat pre-``roles`` shape. Present-but-ignored config is the
#: failure mode this whole audit kept turning up, so it is named rather than
#: dropped.
_RETIRED_KEYS = ("allow", "agents")
#: Tool results are truncated before they reach the model's context.
RESULT_LIMIT = 20_000


class MCPUnavailableError(RuntimeError):
    """The MCP SDK is missing, or the endpoint could not be reached."""


def _agent_config() -> dict:
    try:
        from son_of_anton_cli.config import load_config

        return load_config() or {}
    except Exception:
        return {}


def resolve_mcp_config(config: dict | None = None) -> dict | None:
    """Resolve ``physics.mcp`` into {url, headers, allow, timeout}, or None.

    ``physics.mcp.server`` names an entry in the top-level ``mcp_servers``, so
    an endpoint the deployment already declares for the main agent is written
    once and reused here rather than duplicated with its own credential.
    """
    agent = config if config is not None else _agent_config()
    section = ((agent.get("physics") or {}).get("mcp")) or {}
    if not isinstance(section, dict):
        return None

    stale = [k for k in _RETIRED_KEYS if k in section]
    if stale:
        warnings.warn(
            f"physics.mcp.{{{', '.join(stale)}}} is no longer read — allowlists "
            f"are per role now. Move them under physics.mcp.roles, e.g. "
            f"roles: {{manager: [arxiv-search_papers], subagent: [context7]}}. "
            f"Until then the built-in defaults apply and your list is ignored.",
            stacklevel=2,
        )

    declared = section.get("roles")
    roles: dict[str, tuple[str, ...]] = {}
    if isinstance(declared, dict):
        for role, allow in declared.items():
            entries = tuple(
                str(a).strip() for a in (allow or []) if str(a).strip()
            )
            if entries:
                roles[str(role)] = entries
    elif declared is None:
        roles = dict(DEFAULT_ROLES)
    if not roles:
        return None

    url = str(section.get("url") or "").strip()
    headers: dict[str, str] = {}

    server_name = str(section.get("server") or "").strip()
    if server_name:
        entry = (agent.get("mcp_servers") or {}).get(server_name) or {}
        url = url or str(entry.get("url") or "").strip()
        for key, value in (entry.get("headers") or {}).items():
            headers[str(key)] = os.path.expandvars(str(value))
    for key, value in (section.get("headers") or {}).items():
        headers[str(key)] = os.path.expandvars(str(value))

    key_env = str(section.get("api_key_env") or "").strip()
    if key_env and "Authorization" not in headers:
        token = os.environ.get(key_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

    if not url:
        return None
    return {
        "url": url,
        "headers": headers,
        "roles": roles,
        "timeout": float(section.get("timeout") or DEFAULT_TIMEOUT),
    }


def is_allowed(tool_name: str, allow: list[str]) -> bool:
    """True when *tool_name* matches an ``allow`` entry.

    An entry is either a whole tool name (``arxiv-get_abstract``) or a server
    name, which matches every tool that server prefixes (``context7`` matches
    ``context7-query-docs``). Gateways namespace as ``<server>-<tool>``, so one
    rule covers both granularities.
    """
    return any(
        tool_name == entry or tool_name.startswith(f"{entry}-") for entry in allow
    )


async def _with_session(url: str, headers: dict, timeout: float, action):
    try:
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise MCPUnavailableError(
            "the MCP SDK is not installed (son-of-anton[mcp])"
        ) from exc

    client = httpx2.AsyncClient(headers=headers, timeout=timeout)
    async with client:
        async with streamable_http_client(url, http_client=client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await action(session)


def _to_openai_schema(tool: Any) -> dict:
    schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (getattr(tool, "description", "") or "")[:1024],
            "parameters": schema,
        },
    }


class MCPToolset:
    """The allowed subset of one MCP endpoint, as OpenAI tool schemas."""

    def __init__(self, settings: dict):
        self.url = settings["url"]
        self.headers = settings["headers"]
        self.roles: dict[str, tuple[str, ...]] = dict(
            settings.get("roles") or DEFAULT_ROLES
        )
        self.timeout = settings["timeout"]
        self._discovered: list | None = None

    @classmethod
    def from_config(cls, config: dict | None = None) -> "MCPToolset | None":
        settings = resolve_mcp_config(config)
        return cls(settings) if settings else None

    def enabled_for(self, role: str) -> bool:
        """Whether *role* is configured for any lookups at all."""
        return bool(self._allow_for(role))

    def _allow_for(self, role: str) -> tuple[str, ...]:
        """The allowlist for *role*: exact name, then longest prefix."""
        if role in self.roles:
            return self.roles[role]
        matches = [k for k in self.roles if role.startswith(k)]
        return self.roles[max(matches, key=len)] if matches else ()

    def _discover(self) -> list:
        """Fetch the endpoint's tool list once per run.

        A run must not abort because a documentation server is unreachable —
        it degrades to the tools it already had.
        """
        if self._discovered is not None:
            return self._discovered

        async def _list(session):
            return (await session.list_tools()).tools

        try:
            self._discovered = asyncio.run(
                _with_session(self.url, self.headers, self.timeout, _list)
            )
        except Exception:
            self._discovered = []
        return self._discovered

    def tools_for(self, role: str) -> list[dict]:
        """The tools *role* may use, as OpenAI schemas. [] if it gets none."""
        allow = self._allow_for(role)
        if not allow:
            return []
        return [
            _to_openai_schema(t)
            for t in self._discover()
            if is_allowed(t.name, list(allow))
        ]

    def handles(self, tool_name: str, role: str) -> bool:
        """Whether *role* is allowed to call *tool_name*.

        Role-scoped, not global: the whole point of splitting the allowlists is
        that a sub-agent asking for a paper should be told it cannot, rather
        than quietly getting one because the Manager may.
        """
        allow = self._allow_for(role)
        if not allow or not is_allowed(tool_name, list(allow)):
            return False
        return any(t.name == tool_name for t in self._discover())

    def call(self, tool_name: str, arguments: dict) -> tuple[str, bool]:
        """Call one tool. Returns (text, is_error) — never raises."""

        async def _call(session):
            return await session.call_tool(tool_name, arguments or {})

        try:
            result = asyncio.run(
                _with_session(self.url, self.headers, self.timeout, _call)
            )
        except Exception as exc:  # noqa: BLE001 — tool failures are data
            return f"MCP error calling {tool_name}: {type(exc).__name__}: {exc}", True

        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            parts.append(text if text is not None else repr(item))
        text = "\n".join(parts).strip() or "(the tool returned no content)"
        if len(text) > RESULT_LIMIT:
            half = RESULT_LIMIT // 2
            text = (
                text[:half]
                + f"\n\n[... truncated {len(text) - RESULT_LIMIT} chars ...]\n\n"
                + text[-half:]
            )
        return text, bool(getattr(result, "isError", False))


class LookupExecutor:
    """Tool executor for an agent whose only tools are lookups.

    Satisfies the duck-type protocol ``run_agent_loop`` expects. It has no exit
    tool: the loop ends when the agent stops calling tools and answers, which is
    the same thing it did before it had any tools at all.
    """

    def __init__(self, toolset: "MCPToolset | None", agent_name: str):
        self.toolset = toolset
        self.agent_name = agent_name
        self.stop_after_round = False

    @property
    def active_tools(self) -> list[dict] | None:
        return None

    @property
    def exit_tool_names(self) -> frozenset[str]:
        return frozenset()

    def execute(self, tool_name: str, tool_input: dict):
        from ..core.tool_call import ToolCall

        start = time.time()
        if self.toolset is not None and self.toolset.handles(tool_name, self.agent_name):
            output, is_error = self.toolset.call(tool_name, tool_input)
        else:
            available = [
                t["function"]["name"]
                for t in (self.toolset.tools_for(self.agent_name) if self.toolset else [])
            ]
            output = (
                f"ERROR: Unknown tool '{tool_name}'. Available: "
                + (", ".join(available) or "(none)")
                + "."
            )
            is_error = True
        return ToolCall(
            tool_name=tool_name,
            tool_input=tool_input,
            output=output,
            is_error=is_error,
            duration=time.time() - start,
        )
