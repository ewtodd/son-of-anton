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
   and replayed byte for byte on every later request. When
   `[agent] message_timestamps` is on (the default) *and* the session's system
   prompt carries the guidance's send-time rule, the turn's user message starts
   with its send time, `[Sat 2026-10-24 14:32 CDT]`, in `prompt.LocalZone`;
   the stored content stays clean. The rule is in the stock prompt, so a
   resumed session that predates it stays unstamped until compaction rebuilds
   the prompt, and a `--system` one-shot whose prompt lacks the rule gets no
   prefix. It is rendered once at that turn and persisted, so a later zone
   change does not rewrite history. A Signal message uses its own timestamp; a
   queued one carries it through the queue.
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
- **The view socket** (owner, 2026-11-08; `phases/12-view-socket.md`). With
  `soa-d serve --view-socket PATH --view-group G` (the NixOS module's
  `viewerGroup`) the daemon also listens on PATH, outside the home, 0660 with
  group G; its permissions are the authorization. Every connection there is
  read-only: `hello`, `session.list`, `session.history`, `session.children`,
  `session.attach` as viewer, `session.detach`, `model.list`, `physics.list`
  and `physics.watch`, and anything else, a method added later included, is
  refused with -32014. A viewer is never a driver, so it changes nothing about
  the session it watches; the wall holds as on the owner socket.
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
  streams live events. If the session's model is reported unavailable, only
  the attaching client gets the warning; the bounded catalog lookup runs on a
  short-lived goroutine after the session lock is released, so it never stalls
  streaming nor the connection's read loop.
- **Background receipts** (owner, 2026-11-06). A `notify_on_complete`
  background process ends in a receipt the daemon composes and tags:
  `[background receipt] process <id> exited with code <N>`, then the command
  and the output tail. The stock prompt carries the rule that a message
  starting with it is a daemon report, not the user's voice, answered
  tersely. Receipts that arrive together run as one turn, whether drained at
  once or queued while the session was busy: the batch drives at most one
  turn. Reading a finished process's output through `process` withdraws its
  queued receipt, so a wait or poll is never paid for twice.

## 6. Data

- SQLite through `modernc.org/sqlite` (pure Go, FTS5 included), WAL, one
  writer. Schema is new and versioned with forward migrations. Tables, at
  least: `sessions` (id, kind, parent_id, source, origin chat, title, cwd,
  model, prompt hash, created/ended, status), `system_prompts` (hash, text),
  `messages` (session, seq, role, content, api_content, reasoning, tool calls,
  tool call id, tool name, ts, images), `messages_fts`, `approvals`,
  `queue` (pending sends per session, images), `media` (id: the sha256 of the
  bytes, mime, name, data), `cron_runs`, `rag_*` (section 11).

**Images (owner, 2026-11-06).** Images reach the model's own vision encoder,
or the turn errors; there is no side model and no text summary. A user
message's images and a tool result's images ride the wire as `image_url`
parts after the text part (invariant 2 covers them: the reference and its
bytes are persisted — `media` is content-addressed, so hydration is
byte-stable). `read_file` on an image attaches it; chat attachments attach
natively and still land in `cache/signal` for the model to reach with tools;
queued and steered messages carry their images through the queue. A model
takes images when config says so: the model's route `supports_vision`, else
`[model].supports_vision`, else true — a text-only model (or a transcript
carrying images for one) fails the read or the turn with that as the error,
and nothing is quietly dropped. A `.svg`'s bytes are text above all: its
source is what `read_file` returns (so it stays editable), with the render
attached beside it when the session sees images and `rsvg-convert` works.
Text that merely looks like an image — bytes opening "BM…" with no BMP
header, or a `<svg>` inside prose, code or an HTML page — reads as text;
and when an image's bytes will not attach, `read_file` falls back to them
as text, an error only when they are binary.

**Empty sessions (owner, 2026-11-06).** A session that never got a turn is
not a session yet: the listings (`session.list`, the TUI's `/resume`, the
relay list, `-r latest` and `-c`) leave it out, and `/resume`
never offers the session it runs in. A session with its first turn in flight
still counts — the user row lands with the first step's end, so the daemon
adds what is loaded and running to what the store lists.
- Files that stay as they are: `config.toml`, `.env`,
  `memories/*.toml`, `notes/`, `cron/jobs.json` (same format), `scripts/`.
- Importer: `soa-d import --from <home>` reads the Python fork's `state.db`
  (sessions and messages, including foreign-imported ones), `cron/jobs.json`
  and the memory files, and leaves the source untouched. Recall reindexes.

## 7. Agent loop

- Provider: one OpenAI-compatible chat-completions client (maitred). SSE
  streaming, tool calls, `reasoning_content`, `reasoning_effort` per model,
  timeouts, cancellation through `context.Context`, and retry with backoff on
  429/5xx/503 (temple's lesson: an idle-model swap answers 503).
- A model's reasoning goes back to it (owner, 2026-11-05): every assistant
  message is replayed with its `reasoning_content`, every request, so the
  prefix stays byte-stable. Without it each step of a tool loop loses the plan
  the last one made and works it out again. Compaction counts it.
- Loop: request → stream → run tool calls → append results → repeat, under an
  iteration budget, with interrupt checks between steps. Tool output is
  truncated to a budget before it enters context.
- Compaction: when the context passes a threshold, summarize the head, keep
  the tail, rebuild the system prompt (the one exception to invariant 1).
  Automatic compaction may run again in the same turn after a successful
  compaction; a failed summary does not retry on every step — it keeps a
  cooldown and counts against a per-turn cap of failed attempts (a slow step
  outlasts the cooldown), and nothing to compact counts against neither. A
  request that fails because the context overflowed (including gufo's HTTP
  400 `context_length_exceeded` / "the context is N") learns the stated
  limit, compacts, and retries; recovery can run again after a request
  succeeds, but a request that keeps failing ends the turn.
- `/model` and `/reasoning` change the model for the next turn; the prompt
  stays byte-stable and the server's prefix cache simply misses once.
  `model.list` carries each model's live `status`/`serving` as maitred
  reports it (status-less endpoints report none and filter nothing); each
  call does one `/models` catalog lookup, and a failed lookup filters nothing
  without paying the timeout twice — it is remembered briefly, so attach, the
  chat's outage warning and the models note do not each pay it again. A
  failed listing is never stored as a status-less one: the provider is
  re-read once the window expires, and statuses work again without a
  restart. The picker lists only what is served, naming an unloaded model
  stays valid, a 503 `model_not_loaded` never changes the session's model
  (the dead-pin fallback stays on 404).
- Titles. A session is named the moment its first turn is sent: `title.Derive`
  takes the opening line ("derived"), and, when a `title_generation`
  auxiliary model is configured, that model names the conversation from the
  opening message ("llm") — the reply is cleaned and capped, and the ask runs
  in the background (one at a time) with `session.updated` carrying the name.
  `/retitle <title>` (the TUI's, `session.rename`; owner 2026-11-06) is a
  plain rename, a user act that beats every automatic title and that nothing
  overwrites: the model does not name the conversation again. The
  model-driven retitle of the day (`session.retitle`, which asked the
  session's own model to name the conversation by where it stands) is gone —
  it titled the last half hour instead of the conversation. What reminds the
  owner to rename is deterministic, no model involved: after every compaction
  of an interactive session whose title is not the user's, the session says
  "tip: this conversation outgrew its title (…) — /retitle <what it does now>
  names it". One rename silences the tip for good.
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

1. identity: a short default (a subagent has its type's prompt);
2. tool and behavior guidance;
3. project context files from the session's cwd (`AGENTS.md` and friends);
4. memory rules of the session's scope only (common, or gateway for a chat
   or a Signal-bound cron job).

A chat's prompt is fiona's block (section 12) in place of 1 and 2, then her
notes folder, 3 and 4. A Signal-bound cron job's is the same with
`prompt.SignalJobIdentity` (the default identity and the Signal side's limits)
as its block.

Per-turn recall rides the user message, never the system prompt. The turn's
user message also carries its send time in the API copy (`api_content`), not
the stored content: `[Sat 2026-10-24 14:32 CDT]` in `prompt.LocalZone`, rendered
once per turn and replayed unchanged. `[agent] message_timestamps` turns it off
(default on), and it goes out only while the session's prompt carries the rule
that explains the stamp, so a prompt that lacks it (`--system`, a pre-rule
session before its next compaction) gets no prefix. The guidance says a
message starting with `[Day YYYY-MM-DD HH:MM ZONE]` — or carrying one right
after a compaction summary — was sent at that time; a later time is known only
if the model can run `date`, so fiona (who has no terminal) must not guess it.
A multimodal user
message is one text part (the stamped `api_content`) followed by one
`image_url` part per image: chat attachments and `read_file` results carry
images explicitly (section 6), on `supports_vision` models.

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
- `web_search` (SearXNG) and `web_extract` (`WEB_EXTRACT_URL`) (41, 5),
  `cronjob` (5). `vision_analyze` (123 calls in the old history) is not
  ported: the owner ruled it out (2026-11-06) — images go to the model's own
  vision encoder as message parts, or the model gets an error
  (section 6's Images).
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
- **The nudge** (owner, 2026-11-06; the Python fork's `prompt_notify_after`):
  an approval or clarify question that waits unanswered longer than
  `agent.prompt_notify_after` seconds (default 120, 0 or less off) fires
  `notify-send --app-name="Son of Anton"` with the subject or question (best
  effort: without notify-send or a notification daemon nothing happens).
  Resolving the request cancels the nudge.
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

A second addition: reranking. With `[memory.rag]` `rerank_model` set, the
cosine candidates (at least `min_score`, at most `rerank_candidates`, 10) go
to `<base_url>/rerank` (Cohere/Jina shape, as llama.cpp's `--reranking` server
and maitred serve it) with the turn's first 500 characters as the query, no
task prompts on either side. Those scoring at least `rerank_min_score` (0.01)
are kept in the reranker's order, at most `top_k`. The floor is low on
purpose: Qwen3-Reranker's score is its probability of "yes", and on 18
paraphrased questions against 12 facts (2026-11-10) it put the relevant fact
first every time but scored indirect ones as low as 0.015 ("the doctor wants
to prescribe me an antibiotic" against a penicillin allergy), while unrelated
facts stayed at 0.002 or under; 0.5 would have dropped 9 of 17. The reranker reads every
candidate against the query, so it costs time on every turn that has
candidates: `rerank_timeout_seconds` (3) bounds it, and a timeout or any other
failure keeps cosine order and leaves the reranker alone for
`failure_cooldown_seconds`, while the embedder stays in use.

`session_search` reranks too when it ranks by relevance (no `sort`). A query
fetches twice `limit` text-matched sessions and the meaning matches, and at
most 24 of those, after the wall, go to the reranker, each as the session's
title (`Conversation "…"`), then the message's role and its indexed chunk, or
1,200 characters of the text-matched message around the snippet's first
highlighted term. The query goes as plain words (FTS5 quotes, stars,
parentheses, `AND`/`OR`, and `NOT` with its word dropped) after "Find the past
conversation about: ": the reranker looks for passages that answer a query,
and a match is often a past question, which framed scores like an answer
("wifi disconnects" against a titled "my wifi keeps dropping every evening":
0.205 plain, 0.975 framed). Sessions come back in the reranker's order and
none is dropped, since a real match can score like an unrelated one (a past
"won't suspend since the kernel update": 0.002-0.013 for "sleep broken after
upgrade") while the order still puts it first. `session_id` + `query`
reorders that session's matches the same way, which picks the anchor. A tool
call can wait longer than a turn: `session_search_rerank_timeout_seconds`
(10) bounds it. A failure keeps the old order (text matches, then meaning
matches) and starts the same cooldown.

## 12. Signal, fiona and the relay

- Signal adapter over signal-cli's HTTP JSON-RPC (`SIGNAL_HTTP_URL`): receive,
  send, typing, receipts, attachments, groups, allowlists, `require_mention`,
  history backfill. Control messages (`/stop`, `/new`, `/sendnow`,
  `/sendall`, `/status`, `/approve`, `/deny`) bypass the queue. A message's own
  Signal timestamp stamps its user turn's API copy; a queued message keeps that
  timestamp in the queue. Reference: `gateway/platforms/signal.py`, the
  control-command path in `gateway/run.py`.
- Every instance's chat agent is **fiona**: it runs on fiona's auto route in
  maitred (whatever eklow serves), always on, and falls back to son-of-anton's
  auto route when fiona is down. Her block (`prompt.FionaIdentity`) is
  generated from config when a chat session starts (so it is byte-stable for
  the session) and is her whole identity and guidance: the default identity
  and the coding guidance never appear in a chat. It says, in this order
  (owner-approved, 2026-11-08, after the transcripts of 2026-11-03 to 11-08
  showed fiona reading the coding guidance as hers: "I can run shell
  commands", "I can't run `date`"):
  - who she is: fiona, the front desk of this instance (`<instance>`), on
    Signal; she answers questions, looks things up on the web, reads images,
    keeps notes and memories;
  - what she cannot do: her file tools reach only her notes folder, she runs
    no commands, and web_search / web_extract read the internet, not this
    machine; anything needing a command or a file outside her notes goes to
    a worker through `relay`;
  - handing work over: the user's own words, then context from her memory or
    notes marked as hers; one request, one send; after a send she ends her
    turn, since the report arrives as a message starting with "[worker"
    (2026-11-08: 493 status calls in a row "to wait");
  - this instance's worker hours, or "any time", and what to tell the user
    when a send queues;
  - getting it right: look up how something here works before explaining it
    (cronjob list, relay list, notes), ask when a message could be about more
    than one thing, recheck when told she is wrong and change her answer for
    what she finds, say plainly when a tool fails;
  - time: each user message starts with its send time (the send-time rule,
    so her messages are stamped), the only clock she has;
  - replies: the answer first, short, for a phone.

  The prompt then names her notes folder and carries the project files, the
  gateway-scope rules only (owner, 2026-11-08: the common ones are about
  son-of-anton work) and the date line.
- The chat is relay-only on every instance: `relay`, `memory`,
  `session_search`, `clarify`, `cronjob`, `todo`, `web_search`, `web_extract`,
  and read/patch/write confined to
  `notes/gateway/`. No terminal, no execute_code, no MCP, no `task`.
  Signal-bound cron gets the same set less `clarify`, `relay` and `cronjob`,
  runs in the notes folder whatever its workdir, and keeps the cron model, so
  it is not fiona (owner, 2026-11-08: before, it had every tool but clarify,
  and play's 7am check-in hunted through `tool_search` and the shell for a way
  to send its message itself).
- `relay send` creates or feeds a worker (section 3: top-level, terminal
  scope, `model.default`). Worker hours per instance (ricky and markets 20-7);
  outside them, or while the worker is busy, the send queues. The `relay`
  result states exactly when it will run ("queued until 20:00", "after the
  current turn") and that the report arrives in the chat as a message, so she
  ends her turn; fiona passes the when on, and `relay list` and the TUI session
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
- A chat's conversation lasts a day (owner, 2026-11-10, instead of compacting
  fiona's long chats sooner). With `[gateway] daily_reset` (default true), the
  first message, worker report or notice after `daily_reset_hour` (4, local
  time) has come round with nothing happening in the chat's session since
  starts a new session, as `/new` would. The old one stays, and that chat's
  `session_search` still finds it. A conversation still going past midnight is
  not cut, and a running turn is never reset (what arrives queues into it).
  The new session keeps the chat's `/model` choice and reasoning effort: those
  belong to the chat, not to the day.
- Shared instances (house, ricky, markets): a chat sees and drives only the
  sessions it started, it keeps hearing of them while a terminal drives them,
  and `session_search` from a chat sees only that chat's sessions plus common
  ones.
- End-of-turn checker (`[checker]`, chats only; owner, 2026-11-10). A reply
  that would end a chat turn, served by a model matching a `models` prefix, is
  first sent to `<base_url>/systemone` (laya's wire shape: `{state,
  questions: {id: {type: "noul", instructions}}}`, answered with
  `answers[id].noul`, the probability of yes). The state is the tool names, the
  user message as the model saw it (stamp, recalled notes and model note
  included), this turn's tool calls then results per step (arguments cut at
  600 characters, results at 1500, the message and the draft at 3000), and the
  draft: byte for byte the text the replay's `checker.py` built, which the
  heads were fitted on. A first draft is asked up to three questions, those
  `questions` lists (default all), each against its own cut-off (`cutoffs`,
  default 0.5): `claim` (says it did something no tool call did), `unchecked`
  (answers a question about current state with no tool call that checked it),
  `offtopic` (does not answer the latest message). `unchecked` is asked only
  of a turn that made no tool call: the head cannot tell which state a call
  checked, every draft it caught on the replay came from a turn without one,
  and on 2026-11-10 it flagged (0.93) the reply to a check-in the turn had just
  written to the notes. A retried draft is asked only `claim`, at
  `recheck_cutoff` (0.5), since a nudged model tends to answer with an invented
  "I've logged it"; with `claim` off, a retry is not checked. A flagged draft
  goes back with a note naming each problem, as a user message prefixed
  "[A check by son-of-anton, not a message from the user]" and ending with an
  instruction to write the reply to the user's latest message: on 2026-11-10,
  without it, Bolt answered the note itself ("Understood. I will check
  cronjobs ... before answering"), and that reached the chat. The draft and the
  note ride that request only and are never stored, and the chat sees only the
  reply that stands. At most `max_retries` (2) drafts go back per turn. Not
  checked: answers under tool_choice none (the step budget: the notes ask for
  a tool call), and a stopped turn, whose stop cancels the check and keeps the
  draft. A check that fails, answers incompletely or exceeds
  `timeout_seconds` (3) lets the draft stand and is logged. Why: on the fiona
  replay (2026-11-10), Bolt-E4B with laya's 0.8B reader and heads fitted on
  that replay's other models caught 11 of 14 bad replies and flagged 3 of 15
  good ones (held-out cut-offs claim 0.73, unchecked 0.59, offtopic 0.44).

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

Every enabled NixOS instance gets tools on the agent's PATH by default
(owner, 2026-11-06): `libnotify` (the prompt nudge, section 10), `nixfmt`,
`deadnix` and the host's `nix` (the agents maintain Nix and the NixOS
configuration with them). `extraPackages` adds on top per instance.

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
   `/reasoning`, `/commit`, `/resume`, `/new`, `/stop`,
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
