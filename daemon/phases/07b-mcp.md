# Phase 7b: MCP client

Goal: soa-d connects to the instance's MCP servers (`[mcp_servers.<name>]`:
the owner's socat stdio bridges and maitred's HTTP endpoint) and offers their
tools through three fixed tools, `tool_search`, `tool_describe` and
`tool_call` (SPEC sections 8 and 14), so no MCP schema rides every request
and a session's tool list never changes when a server comes or goes.

Read first: `SPEC.md` sections 4 (invariant 1), 8 and 14, and the code. The
reference is the Python `tools/mcp_tool.py` (MT) and `tools/tool_search.py`
(TS). Where this file and the Python differ, this file wins.

## Already written (do not modify)

`internal/server/mcp_server_test.go` (its fake servers speak the protocol
directly: stdio through the test binary, HTTP through `httptest`).

## Decisions made without the owner (2026-11-02, night)

- `tool_search`'s description names the configured servers, not a live
  catalog of their tools (the Python embeds one), so the tool list is
  byte-stable for a session's life whichever servers are up.
- An unset or empty `${VAR}` in a server's config refuses to connect,
  naming the variable (the Python sent the literal placeholder and got a 401).
- No sampling, no elicitation, no resource or prompt utility tools: the
  owner's history has no calls to them that mattered (list_resources and
  get_prompt three times each).
- The official Go SDK (`github.com/modelcontextprotocol/go-sdk`), pinned per
  `AGENTS.md`; update the Nix `vendorHash`.

## Config

`[mcp_servers.<name>]`, as the Python reads it (MT:2016-2390, 4621-4634):
`command`, `args`, `env`, `cwd` (stdio), or `url` and `headers` (HTTP; `url`
wins when both are set); `enabled` (default true); `timeout` (per call,
default 300 s); `connect_timeout` (default 60 s); `tools.include` /
`tools.exclude` (exact names or globs on the server's own tool names;
`include` wins). `${VAR}` and `${env:VAR}` in `command`, `args`, `env`,
`url` and `headers` resolve through `config.Getenv` (process env, then
`.env`).

## Servers

- At startup every enabled server connects in the background, concurrently.
  The three tools wait (up to its `connect_timeout`) for a server's first
  connection attempt to end, so a call right after the daemon starts sees
  every server that comes up; nothing else waits, and nothing waits on a
  later reconnect. A server that fails (or disconnects) is
  retried in the background, 5 s doubling to 5 min, and its tools are out of
  the catalog while it is down.
- stdio: the process gets only `PATH HOME USER LANG LC_ALL TERM SHELL
  TMPDIR` and `XDG_*` from the daemon's environment, then the config `env`;
  it runs in its own process group with a parent-death signal; its stderr is
  appended to `<home>/logs/mcp-stderr.log` after a line "===== [<local time,
  2006-01-02 15:04:05>] starting MCP server '<name>' =====". Shutdown sends
  SIGTERM to the group, then SIGKILL after 3 s.
- HTTP: streamable HTTP, the config `headers` on every request; an
  `Authorization` header is not sent across origins on a redirect.
- The catalog: each server's `tools/list` (all pages), filtered, named
  `mcp__<server>__<tool>`, each part with `[^A-Za-z0-9_]` replaced by `_`
  (MT:4270-4297). Two raw names that collide after that are both left out
  (logged). A `notifications/tools/list_changed` refreshes the server's part.

## The three tools

Offered to interactive, worker, cron and oneshot sessions and to `general`
subagents, whenever at least one enabled server is configured (connected or
not); never to chat sessions, never when no server is configured.

- `tool_search {query, limit (default 5, max 20)}`: BM25 (k1 1.5, b 0.75)
  over each catalog tool's name split into words, its description and its
  top-level parameter names; when nothing scores, a substring match on the
  name. Answers `{"query", "total_available", "matches": [{"name", "server",
  "description" (first 400 chars)}]}`; with no match it adds
  `"unavailable": [{"server", "error"}]` for each server that is down.
  Description: "Search the tools of this instance's MCP servers (<the
  enabled servers' names, sorted, comma-separated>). Returns up to `limit`
  matching tools with their names and descriptions. Call tool_describe for
  a tool's parameters, then tool_call to run it."
- `tool_describe {name}`: `{"name", "server", "description", "parameters"}`
  (the tool's `inputSchema`; an empty one becomes `{"type": "object",
  "properties": {}}`). Description: "Show an MCP tool's description and
  parameter schema, by the name tool_search gave."
- `tool_call {name, arguments}` (`arguments` an object, or a JSON string of
  one): calls the tool. Description: "Run an MCP tool found with
  tool_search, with arguments that match the schema tool_describe showed."
  Its result is wrapped by `tools.WrapUntrusted` under the tool's `mcp__…`
  name (the wrapper's `source`), not under `tool_call`.
  - Result: text blocks joined by "\n"; an image block saved to
    `<home>/cache/mcp/<sha256 of the data, 16 hex>.<ext>` and shown as
    `[image saved at <path>]`; an embedded resource's text as its text;
    other blocks left out. `{"result": text}`, plus `"structuredContent"`
    when the server sent it; `isError` gives `{"error": text}` ("MCP tool
    returned an error" when empty). Over 50,000 characters: the text keeps
    its first 35,000 and last 15,000 with "\n\n[... <n> characters omitted;
    the full result is saved at <path>]\n\n" between, the full text saved to
    `<home>/cache/mcp/<call id or a random id>.txt`.
  - An unknown name (not wrapped): `{"error": "no MCP tool <name>; use
    tool_search to find one"}`.
    A tool of a server that is down: `{"error": "MCP server '<server>' is
    unavailable (<why>); try again later or continue without it"}`. A
    transport failure mid-call reconnects once and retries the call once.
  - MCP calls are not gated by approvals or the permission mode (as in the
    Python).

## README

Status: drop "MCP servers" from "Not in the daemon yet". One line: MCP
tools are found with `tool_search` and run with `tool_call`.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 7b (MCP
client)`, body listing what changed, ending with the `Model:` line.
