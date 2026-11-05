# soa-d: the Son of Anton daemon (master spec)

Status: approved by the owner, 2026-11-01. This replaces the Python backend
(agent loop, tools, gateway, cron, state) with one Go daemon per instance. The
Textual TUI stays as a client for v1 and moves to Go after it. Every decision
marked **(owner)** was made by the repo owner and is not up for re-litigation
in a phase; resolved questions are listed at the end.

Each phase below gets its own task spec (`daemon/phases/NN-*.md`) with exact
acceptance checks. A phase spec pulls behavior from the Python files named as
references here; the Python code is the behavioral reference, not a design to
copy.

## 1. Why

- The fork is ~242k lines of non-test Python for a single-provider,
  single-platform, mostly single-user daemon (`gateway/run.py` 17.7k,
  `son_of_anton_state.py` 11k, `cli.py` 10k, compaction ~11.5k). Repeated
  purges (e.g. `purge2/*`, 2026-10-16) have not stuck: every removal leaves
  residue (dead directories recreated at startup, removed platforms still
  listed, defensive branches for features that are gone).
- temple (`~/Software/harness/temple`, Rust, archived 2026-08) did a daemon +
  TUI client + Signal + cron + MCP in ~17.7k lines. It failed on bugs, not on
  shape. This time the spec and the contract tests come before the code.
- Target: daemon v1 under ~30k lines of Go, tests excluded.

## 2. Decisions (owner, 2026-11-01)

- Backend in **Go**. One daemon (`soa-d`) per instance. Same repo, a Go module
  under `daemon/`, one flake. The user-facing command stays `son-of-anton`.
- The **Textual TUI stays for v1** (`son_of_anton_tui/tui.py`, `feed.py`,
  `completion.py`); only `son_of_anton_tui/backend.py`, which subclasses the
  Python agent today, is replaced by a protocol client. **The TUI moves to Go
  after v1** (phase 9), so the protocol must assume nothing Textual- or
  Python-specific.
- **Implementer:** the local `radiance/qwen3.8-27b` via the delegate-local
  workflow; Sonnet when the 27B is stuck. Claude writes each phase spec and
  its contract tests first and reviews every merge.
- **Subagents work like opencode v1.19.5** (section 9). `/resume` is for
  sessions, never for viewing subagents.
- **fiona / relay-only Signal** is built here, not in the Python fork
  (section 12). The Python fork gets fixes only until it is retired.
- **Scope from measured usage** (section 18): kept foreign session import and
  LSP diagnostics; dropped `mcp serve`, `/goal`, `/loop`, skills, and the
  slash commands never typed.
- Physics (`autophysicist/`) stays a separate Python CLI
  (`son-of-anton problem create|run`), run by the agent through its terminal.
  (Removed in phase 11a; it is rebuilt in Go in phase 11b.)

## 3. Shape

```
                 ┌─────────────────────── soa-d (one per instance) ────────────────────────┐
 Textual TUI ──► │ unix socket API ─► sessions ─► agent loop ─► tools ─► approvals         │
 son-of-anton -q │        ▲               │           │          (terminal, files, MCP, …) │
 son-of-anton -z │        │               ▼           ▼                                    │
                 │  Signal adapter ◄── relay   provider client ──► maitred (/v1)           │
                 │  cron scheduler      memory + recall (rules, facts, notes, embeddings)  │
                 │  SQLite (state.db): sessions, messages, FTS, approvals, queues          │
                 └─────────────────────────────────────────────────────────────────────────┘
```

**One primitive: a session the daemon runs.** Every model turn belongs to a
session the daemon owns. Kinds:

| kind | created by | driver (may send turns) |
|---|---|---|
| `interactive` | TUI, or the chat's `relay` tool | the attached TUI; with none, the chat that owns it |
| `chat` | Signal chat | that chat |
| `child` | the `task` tool (subagent) | its parent session's loop |
| `cron` | the scheduler | nobody (runs its prompt) |
| `oneshot` | `son-of-anton -q` / `-z` | the invoking client |

A **worker** is not a kind (owner, 2026-11-05). It is an `interactive` session
with no terminal attached and a chat as its `Owner`, headless until a terminal
attaches and no different from one that started in a terminal.

A session has at most one driver and any number of viewers. Attaching a TUI to
a session as driver is `/resume` or `son-of-anton -r`; it replaces the whole
relay host/worker, yield, takeover and heartbeat machinery of the Python fork
(AGENTS.md "The relay"), because no session is ever owned by a separate
process.

The daemon is the only writer of `state.db`. Clients never open it.

## 4. Invariants (each needs a contract test)

1. **Byte-stable prompt.** A session's system prompt and tool list are fixed
   when it starts and rebuilt only by compaction. Nothing mid-session (memory
   writes, config changes, `/model` excepted, see section 7) changes them.
2. **Replay what was sent.** Anything injected into a user turn (recall,
   timestamps, gateway notes) is persisted with the message (`api_content`)
   and replayed byte for byte on every later request.
3. **Role alternation.** Never two same-role messages in a row; never a
   synthetic user message injected mid-loop.
4. **Interrupt is not yield.** Interrupt stops the turn and records it;
   taking over a worker lets running tools finish, cancels an in-flight model
   request without keeping its output, and adds nothing to the transcript.
5. **Approval boundaries.** A memory rule never changes without the user's
   approval; dangerous commands follow the permission mode; a durable approval
   can be answered from any driver of the session.
6. **The wall.** A `common` caller (terminal, worker, common cron, their
   children) never sees gateway memory, gateway facts, or any Signal
   conversation, by any path: prompt, recall, `session_search`, session lists.
7. **Secrets.** Redacted before anything is indexed or logged; never written
   into the Nix store; `.env` stays 0600.
8. **Tests never touch a real home.** Every test runs against a temp
   `SON_OF_ANTON_HOME` and a fake model server.

## 5. Client protocol

- Transport: unix socket `$SON_OF_ANTON_HOME/run/soa-d.sock`, mode 0600, owned
  by the instance user. Newline-delimited JSON. Requests and responses are
  JSON-RPC 2.0; events are notifications with a per-session sequence number so
  a client can resume after a reconnect.
- Handshake: `hello {protocol: 1, client: "tui"|"cli"}` → daemon version,
  instance name, capabilities.
- Requests (initial set): `session.list`, `session.create`, `session.attach
  {id, as: driver|viewer, from_seq}`, `session.detach`, `session.children`,
  `turn.send {text, attachments, mode: queue|step|now}`, `turn.interrupt`,
  `queue.clear`, `approval.answer`, `question.answer`, `command.run {name,
  args}` (daemon-side slash commands), `model.list`, `model.set`.
- Events (map onto the TUI's existing `Sink.emit` kinds in
  `son_of_anton_tui/backend.py`): `assistant.start|delta|end`,
  `reasoning.start|delta|end`, `tool.start|output|end` (structured: name,
  argument summary, status, result summary), `turn.end` (usage), `compaction`,
  `approval.request`, `question.request`, `session.updated` (title, usage,
  status), `child.created`, `child.done`, `notice` (text, level).
- The daemon sends structured data, never pre-rendered ANSI. The TUI renders
  tool rows itself (today they arrive as `ansi`/`rich` events from the Python
  display layer).
- On attach the daemon replays the transcript from `from_seq` (paged), then
  streams live events.

## 6. Data

- SQLite through `modernc.org/sqlite` (pure Go, FTS5 included), WAL, one
  writer. Schema is new and versioned with forward migrations. Tables, at
  least: `sessions` (id, kind, parent_id, source, origin chat, title, cwd,
  model, prompt hash, created/ended, status), `system_prompts` (hash, text),
  `messages` (session, seq, role, content, api_content, reasoning, tool calls,
  tool call id, tool name, ts, active), `messages_fts`, `approvals`,
  `queue` (pending sends per session), `cron_runs`, `rag_*` (section 11).
- Files that stay as they are: `config.toml`, `.env`, `SOUL.md`,
  `memories/*.toml`, `notes/`, `cron/jobs.json` (same format), `scripts/`.
- Importer: `soa-d import --from <home>` reads the Python fork's `state.db`
  (sessions and messages, including foreign-imported ones), `cron/jobs.json`
  and the memory files, and leaves the source untouched. Recall reindexes.

## 7. Agent loop

- Provider: one OpenAI-compatible chat-completions client (maitred). SSE
  streaming, tool calls, `reasoning_content`, `reasoning_effort` per model,
  timeouts, cancellation through `context.Context`, and retry with backoff on
  429/5xx/503 (temple's lesson: an idle-model swap answers 503).
- Loop: request → stream → run tool calls → append results → repeat, under an
  iteration budget, with interrupt checks between steps. Tool output is
  truncated to a budget before it enters context.
- Compaction: when the context passes a threshold, summarize the head, keep
  the tail, rebuild the system prompt (the one exception to invariant 1).
- `/model` and `/reasoning` change the model for the next turn; the prompt
  stays byte-stable and the server's prefix cache simply misses once.
  `model.list` carries each model's live `status`/`serving` as maitred
  reports it (status-less endpoints report none and filter nothing); the
  picker lists only what is served, naming an unloaded model stays valid, a
  503 `model_not_loaded` never changes the session's model (the dead-pin
  fallback stays on 404).
- Titles from the auxiliary model.
- References: `agent/conversation_loop.py`, `agent/turn_context.py`,
  `agent/conversation_compaction.py`, `agent/context_compactor.py`,
  `agent/retry_utils.py`.

### Prompt composition (owner, 2026-11-01)

A delegated run should not carry the user's whole context: a worker the owner
launches with `-z`, or a subagent, needs its task, not the user's memories.
The Python fork only has `--ignore-rules`, which drops everything (including
`AGENTS.md`) at once, and `-z` loads full memory by default.

The system prompt is built from layers, in this order (most stable first, for
the prefix cache):

1. identity: `SOUL.md`, or a short default;
2. tool and behavior guidance;
3. project context files from the session's cwd (`AGENTS.md` and friends);
4. memory rules for the session's scope.

Per-turn recall rides the user message, never the system prompt.

| session | layers | recall |
|---|---|---|
| `interactive`, `chat`, `cron`, `-q` | 1-4 | yes |
| `-z` | task prompt instead of 1, then 2-3, then delegated rules | no |
| `child` (subagent) | its agent type's prompt instead of 1, then 2-3, then delegated rules | no |

**Delegated rules (owner, 2026-11-01).** A few rules are guards for anything
that runs commands on the owner's machines ("never rm user files", "never
regenerate e-workstation's keys"), not preferences about working with the
owner. A rule with `delegated = true` is kept in the lean prompts above, so a
worker carries the guards without the rest of the user's context. Facts and
recall are never part of a lean prompt.

Flags on both `-q` and `-z`: `--system FILE` makes that file, verbatim, the
whole system prompt (nothing is added, not even delegated rules); `--memory` /
`--no-memory` override the default for layers 1, 4 and recall. Whatever is
chosen is fixed for the session like any other prompt (invariant 1).

## 8. Tools (v1)

Short schemas (the Python schemas trimmed in `85300ef3` are the starting
wording). Measured use over play's history is in parentheses.

- `terminal` with background processes and completion notices, and `process`
  (3111 / 292 calls). Reference: `tools/terminal_tool.py`,
  `tools/process_registry.py`.
- `read_file`, `write_file`, `patch`, `search_files` (ripgrep) (1735, 317,
  1358, 554), with LSP diagnostics appended after writes and patches
  (`agent/lsp/`).
- `execute_code` (213), `todo` (145), `clarify` (11).
- `memory` (rules and facts, section 11) and `session_search` (section 11).
- `vision_analyze` (123), `web_search` (SearXNG) and `web_extract`
  (`WEB_EXTRACT_URL`) (41, 5), `cronjob` (5).
- MCP tools behind deferred search (`tool_search` / `tool_describe` /
  `tool_call`), so MCP schemas never ride every request.
- `task` (subagents, section 9) and `relay` (chat sessions only, section 12).

## 9. Subagents (opencode v1.19.5 model)

Reference: opencode tag `v1.19.5`, `packages/opencode/src/tool/task.ts` and
`task.txt`, `packages/tui/src/routes/session/index.tsx` and
`subagent-footer.tsx`. The Python `delegate_task` was offered on every request
and never called once; do not port it.

- `task {description, prompt, subagent_type, task_id?, background?, model?}`
  creates a `child` session (`parent_id` set) titled "<description> (@<type>
  subagent)". Foreground by default: the parent's tool call returns the
  child's final message plus `task_id`. `background: true` returns at once and
  the result arrives as a notice when the child finishes. `task_id` resumes the
  same child. `model` pins the child's model; without it the child takes the
  parent's, or the instance's `[delegation] model` when that is set. Depth
  limit 1 (configurable). Several `task` calls in one assistant message run
  concurrently.
- A child's tools and permissions are derived from the parent's. Its approval
  and question requests surface in the parent's driver, which blocks the
  parent's prompt until they are answered.
- TUI: a parent turn that called `task` shows "<leader>down view subagents".
  `<leader>down` enters the first child, `left`/`right` cycle siblings, `up`
  returns to the parent (active only inside a child with no dialog open). A
  child view has no prompt; a footer shows the subagent's label, "(N of M)",
  context use and cost, and Parent / Prev / Next. Session lists show only
  top-level sessions. A finished child emits a `subagent_done` notice.

## 10. Approvals and permission modes

- Modes `default | ask | lockdown | yolo` (`/perm`, shift+tab cycles).
- Dangerous-command detection: port the pattern set and its tests from
  `tools/approval.py`; keep the approval UX (TUI modal, Signal `/approve <id>`
  / `/deny <id>`).
- Approvals are durable rows: the first answer from any driver wins.
- **Nobody to ask means deny** (owner, 2026-11-01): `-q` and `-z` have no one
  to answer an approval, so a dangerous command is denied with a reason the
  model sees, unless the run was started with `--yolo`. The Python fork's `-z`
  silently auto-approves everything; that is not carried over.

## 11. Memory and recall

Port the 2026-11-01 design as it stands (AGENTS.md "Memory and recall", and
`tests/test_memory_*`, `tests/test_rag*`, `tests/test_session_search_wall.py`,
`tests/test_nix_rules_merge.py` as the behavior contract): rules and facts in
TOML with ids, scopes `common` and `gateway`, rule changes always approved,
facts retrieved per turn with a keyword fallback, notes indexed by scope,
conversation text indexed for `session_search` only, the wall. Embeddings
come from maitred; cosine scan in Go over facts and notes per turn.

One addition: a rule may carry `delegated = true` (default false), which keeps
it in lean prompts (section 7). The rules loader, the Nix `memoryRules` /
`memoryRulesFile` validation and the rules merge must all accept the field; the
Python fork's merge rejects unknown keys, so rules are tagged only once soa-d's
merge is the one running.

## 12. Signal, fiona and the relay

- Signal adapter over signal-cli's HTTP JSON-RPC (`SIGNAL_HTTP_URL`): receive,
  send, typing, receipts, attachments, groups, allowlists, `require_mention`,
  history backfill. Control messages (`/stop`, `/new`, `/sendnow`,
  `/sendall`, `/status`, `/approve`, `/deny`) bypass the queue. Reference:
  `gateway/platforms/signal.py`, the control-command path in
  `gateway/run.py`.
- Every instance's chat agent is **fiona**: it runs on fiona's auto route in
  maitred (whatever eklow serves), always on, and falls back to son-of-anton's
  auto route when fiona is down. Its identity block is generated from config
  when a chat session starts (so it is byte-stable for the session): it is
  fiona, it does not run commands or edit project files, real work goes to
  son-of-anton workers through `relay`, and this instance's worker hours (or
  "any time"). The instance's `SOUL.md` persona follows it after a blank
  line; with none, the block is the whole identity (never the default
  "Son of Anton Agent"). The text (owner-approved first pass, 2026-11-02;
  `<instance>` and the hours come from config):

  > You are fiona, the front desk of this son-of-anton instance
  > (`<instance>`), talking with the user over Signal. You answer questions,
  > look things up, read images, and keep notes and memories yourself.
  >
  > You never run commands or edit project files. Your file tools reach only
  > your notes folder. Real work (code, files, commands, anything in a
  > repository or on a machine) goes to a son-of-anton worker through the
  > relay tool: pass the user's request on as they gave it, then tell them
  > what the worker reports. A worker cannot see your memory, so add
  > anything from it the worker needs, marked as context from you.

  then, with no worker hours:

  > Workers here can run at any time. A request sent while a worker is busy
  > waits in a queue, and the relay result says when it will run (for
  > example "after the current turn"). Tell the user exactly that. At a
  > terminal, the user can take a worker over with /resume.

  or, with hours (ricky and markets: 20:00 to 07:00):

  > Workers here run only between 20:00 and 07:00. A request sent outside
  > those hours, or while a worker is busy, waits in a queue, and the relay
  > result says when it will run (for example "queued until 20:00" or "after
  > the current turn"). Tell the user exactly that; don't promise it sooner.
  > At a terminal, the user can take a worker over with /resume, which runs
  > it at once whatever the hours.
- The chat is relay-only on every instance: `relay`, `memory`,
  `session_search`, `clarify`, `cronjob`, `todo`, `web_search`, `web_extract`,
  `vision_analyze`, and read/patch/write confined to
  `notes/gateway/`. No terminal, no execute_code, no MCP, no `task`.
  Signal-bound cron gets the same set.
- `relay send` creates or feeds a worker (section 3: top-level, terminal
  scope, `model.default`). Worker hours per instance (ricky and markets 20-7);
  outside them, or while the worker is busy, the send queues. The `relay`
  result states exactly when it will run ("queued until 20:00", "after the
  current turn"), fiona passes that on, and `relay list` and the TUI session
  list show the same status. A TUI `/resume` makes the TUI the session's driver
  and overrides the hours.
- On a single-user instance `relay` drives any session, not just the ones it
  started. `relay list` shows the ten most recent idle ones and every busy one,
  with its cwd, and a send continues the session itself, with its own prompt,
  history, model and directory. The first send takes it on (`Owner`, so it
  survives a restart): whenever no terminal drives it, its turns report back,
  its approvals and questions go to the chat, and its queue runs. `relay detach`
  lets it go. The chat and the terminal are one person here, and someone
  writing from the chat is not at the terminal they left open, so within worker
  hours a send takes any session from an attached terminal, even mid-turn:
  waiting approvals move to the chat, and the TUI closes and prints who took it
  and how to take it back. A turn a terminal runs is not reported. A TUI that
  loses the driver role to anyone closes the same way. A turn outlives its
  terminal, so `/quit` asks what becomes of a running one. `/handoff` is the
  same thing from the terminal's end: it gives the session to the chat used
  last, tells fiona, and closes the TUI.
- `new_in` is refused while a session the chat may drive has a turn running in
  that directory's workspace (the git root): two agents in one repository undo
  each other's work.
- Shared instances (house, ricky, markets): a chat sees and drives only the
  sessions it started, it keeps hearing of them while a terminal drives them,
  and `session_search` from a chat sees only that chat's sessions plus common
  ones.

## 13. Cron

`cron/jobs.json` stays compatible. Agent jobs and script jobs (`no_agent`),
delivery to a chat or local, memory scope from where the job delivers, and the
hardening invariants in AGENTS.md "Cron". Reference: `cron/jobs.py`,
`cron/scheduler.py`.

## 14. MCP, LSP, foreign sessions, web

- MCP client: the official Go SDK, stdio servers (the socat bridges) and HTTP
  with headers; tools reached through the deferred trio.
- LSP: servers found on PATH; diagnostics after edits. Reference `agent/lsp/`.
- Foreign session import (Claude Code and Codex JSONL, opencode's SQLite database), read-only on the
  source. Reference: `son_of_anton_cli/foreign_sessions.py`.
- Web: SearXNG search; page extraction through `WEB_EXTRACT_URL`.

## 15. Nix

`buildGoModule` package; the NixOS and Home Manager modules keep their option
names (`instances.*`, `settings`, `memoryRules`, `memoryRulesFile`,
`environment`, `environmentFiles`, `workingDirectory`, ...) so `/etc/nixos`
changes as little as possible. The gateway unit becomes the `soa-d` unit.

## 16. Testing

- Go tests per package, written as behavior contracts (no snapshots of data
  expected to change).
- A fake OpenAI-compatible server (scripted completions: text, tool calls,
  reasoning, streaming, 429/503) is the core fixture for loop tests.
- Protocol tests drive the daemon over its socket; the TUI client gets a
  headless test mode.
- Each phase ports the intent of the Python contract tests it replaces.
- Gates: `gofmt -l` clean, `go vet ./...`, `go test ./...`, `nix flake check`.

## 17. Phases

Each phase ends merged to `main` with its gates green. The Python fork keeps
serving every instance until the last one migrates.

0. **Skeleton:** module layout, config load (`config.toml` + `.env`), SQLite
   schema v1, provider client against the fake server, a tool-less loop,
   `son-of-anton -q`.
1. **Core tools and safety:** terminal/process, file tools, todo, clarify,
   approvals and permission modes, `-z`, and the prompt composition of
   section 7 (layers, per-kind defaults, `--system`, `--memory`).
2. **Protocol and TUI:** socket API, attach/replay, events; the Textual
   client replaces `backend.py`; the slash commands in use (`/model`,
   `/reasoning`, `/commit`, `/resume`, `/sessions`, `/new`, `/stop`,
   `/cancel`, `/sendnow`, `/sendall`, `/perm`, `/compact`).
3. **Memory, recall, `session_search`, compaction, titles.**
4. **Subagents** (section 9), daemon and TUI.
5. **Signal, fiona, relay workers, worker hours.**
6. **Cron.**
7. **MCP, LSP, web, vision, execute_code, foreign import.**
8. **Nix modules, importer, migration** one instance at a time: markets,
   ricky, house, work, play. Then delete the Python backend.
9. **Go TUI** (owner, after v1): a Go client against the same protocol,
   keeping today's layout and keybinds (including the subagent navigation of
   section 9); then delete the Textual TUI and the last of the Python except
   physics.

## 18. Out of scope

`mcp serve`, skills, `/goal`, `/loop`, `/plan`, `/undo`, `/retry`,
`/background`, `/agents`, `/focus`, `/insights`, `/usage`, `/worktree`,
`delegate_task`, multi-provider catalogs, and every other slash command with no
measured use. Anything wanted later comes back as a deliberate feature.

## 19. Resolved questions (2026-11-01)

- **fiona's identity and the queue:** see section 12 (identity generated from
  config at session start; queued sends report when they will run).
- **TUI language:** Go, after v1 (owner); phase 9.
- **`/commit`:** daemon-side, a templated turn on the session (review the
  uncommitted changes in the session's directory, write a message in the
  repo's style, commit), so every client gets it and the Go TUI has nothing
  extra to port.


## 20. Provenance

From phase 10c on, nothing of hermes-agent remains: the prompts, tool
descriptions and messages were rewritten from the behaviour they have to
produce, the pattern tables and the execute_code helper were replaced by this
project's own, and the Nix packaging is its own. The Python files the
sections above name were the behavioural reference for the port, kept there
as history.

opencode is different: the TUI's layout is a port of its session view, and
some TUI code follows opencode's closely (the palette derivation, the
subagent footer, small formatters), with comments saying so. The owner
decided (2026-11-03) that this may stay, with what that means for the
license.
