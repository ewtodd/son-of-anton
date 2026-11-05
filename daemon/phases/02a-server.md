# Phase 2a: the daemon's socket server

Goal: `soa-d serve`, the per-instance daemon. It owns sessions, runs their
turns, keeps a durable queue of pending sends, asks the attached client for
approvals and answers, and streams everything over a unix socket. Phase 2b
replaces the Textual TUI's Python backend with a client of this protocol.

Read first: `SPEC.md` sections 3, 4, 5 and 10, `AGENTS.md`, and the phase 0,
1a and 1b code. Where this file and SPEC.md differ, this file wins; the
refinements are listed at the end.

## Already written (do not modify)

- Contract tests: `internal/server/server_test.go` and its harness
  `internal/server/harness_test.go`, `internal/agent/stop_test.go`,
  `internal/store/server_store_test.go`, `cmd/soa-d/serve_test.go`.
- Changed earlier contracts (the old behavior was wrong, see "Endings"):
  `internal/agent/agent_test.go` (`TestAFailedTurnIsRecordedAndClosed`,
  `TestAnInterruptedFirstRequestKeepsTheMessage` replace the two "leaves no
  trace" tests) and `internal/agent/toolloop_test.go` (a failure after a tool
  step and an interrupt during tools now close with an assistant row;
  `checkPairing` forbids a user message right after a tool result).
- `internal/fakellm`: `Turn.Release` (hold a turn open mid-stream) and
  `Server.SetModels` (`GET /v1/models`).

## package agent

### Endings (fix)

Every turn that started is recorded, and the transcript always ends on an
assistant row. Python learned this the hard way: a transcript ending
`tool -> user` makes strict providers misread the next message
(`../agent/message_sanitization.py:296`), and a lost user message confuses
the user.

```go
const InterruptedText = "[response interrupted]" // ../agent/agent_runtime_helpers.py:1597
const FailedText = "[response failed]"
var ErrStopped = errors.New(...) // Send's error after Stop
```

When a turn does not end with a final answer:

- The user message is stored if it was not yet (it is stored with the first
  persisted step, as today).
- A model request in flight is cancelled and its output is not used, except:
  on a stop or cancellation, the content it had streamed, trimmed, becomes
  the closing assistant row (no tool calls; streamed reasoning goes in its
  reasoning column).
- Otherwise the closing row is `InterruptedText` (stop, cancellation) or
  `FailedText` (an error).
- Send returns `ErrStopped` after `Stop`, `ctx.Err()` after cancellation, or
  the error.

### Stop, model, mode, background

```go
func (s *Session) Stop(finishTools bool)
func (s *Session) Begin()
```

`Stop` ends the running turn at the next safe point; a no-op when idle (never
carried into a later turn). A model request in flight is cancelled. With
`finishTools`, running tool calls finish and keep their results; without, they
are cancelled (their context is cancelled). Calls that had not started get the
existing cancellation result. Then the step is persisted and the closing row
added. Calling `Stop` from inside `onEvent` is not required to work.

`Begin` (added in review) reserves the next turn: a `Stop` between `Begin`
and the end of the next `Send` applies to that `Send`, even before it has
registered. The server calls it before starting `Send` on a goroutine, so a
steer that lands in between is not lost.

```go
func (s *Session) SetModel(c *provider.Client, model, effort string) // from the next turn
func (s *Session) SetMode(m approval.Mode)                           // at once
func (s *Session) BackgroundRunning() int                            // running background processes
func (s *Session) KillBackground() int                               // kill them; returns how many
func (s *Session) OnNotify(f func())                                 // called when a completion notice is queued
```

`OnNotify`'s callback may run on any goroutine and must not be called with a
lock held that `TakeNotifications` takes. Add what `tools.State` needs for
these (`Gate.SetMode` in `internal/approval` must be safe while a turn runs).

### Events

`Event` gains `Message *store.Message` and `Duration time.Duration`. After
each persist, one `Kind: "message"` event per stored row, in order, with its
`Seq`. A step's message events come after its `tool.end` events and before
the next step's deltas. `tool.end` carries the call's `Duration`.

## package store

Schema v3 (a forward migration): `sessions` gains `reasoning_effort` and
`mode` (TEXT); a `queue` table (id, session_id, kind, text, created; ordered by
insertion).

```go
type Session struct { ...; ReasoningEffort, Mode string } // CreateSession stores them
func (s *Store) UpdateSessionSettings(ctx context.Context, id, model, effort, mode string) error // unknown id: error

type QueueItem struct { ID, Kind, Text string; Created time.Time } // Kind "user" | "notice"
func (s *Store) QueuePush(ctx context.Context, sessionID, kind, text string) (QueueItem, error)
func (s *Store) QueueItems(ctx context.Context, sessionID string) ([]QueueItem, error) // oldest first
func (s *Store) QueueRemove(ctx context.Context, sessionID string, ids ...string) error // unknown ids ignored
func (s *Store) QueueClear(ctx context.Context, sessionID string) error

type Summary struct { Session Session; Updated time.Time; Preview string; Messages int }
func (s *Store) ListSessions(ctx context.Context, kinds []string, limit int) ([]Summary, error)
func (s *Store) MessagesBefore(ctx context.Context, id string, beforeSeq int64, limit int) ([]Message, bool, error)
func (s *Store) MessagesAfter(ctx context.Context, id string, afterSeq int64, limit int) ([]Message, bool, error)
```

`ListSessions`: top-level sessions (no parent) of the given kinds, newest
activity first; `Updated` is the last message's time, or `Created`;
`Preview` is the first user message, at most 100 characters; `Messages` the
row count. `MessagesBefore` (`beforeSeq` 0 = from the end) and
`MessagesAfter` return at most `limit` rows, oldest first, and whether more
exist beyond them in that direction.

## Small additions elsewhere

- `config`: `[git] author_name`, `author_email`; a way to list the
  provider's configured model routes with their `reasoning_efforts`.
- `provider`: `(*Client) Models(ctx) ([]string, error)`: `GET <base>/models`,
  the `data[].id` list.

## package server (new)

```go
var ErrRunning error // another daemon is serving this home
type Options struct { Home, Version string }
func Listen(home string) (net.Listener, error)
func New(o Options) (*Server, error) // loads config, opens <home>/soa-d.db
func (s *Server) Serve(ctx context.Context, ln net.Listener) error
```

**Listen** creates `<home>/run` with mode 0700 (fixing the mode if it
exists) and listens on `<home>/run/soa-d.sock` with mode 0600. If something
exists at that path: when a connection to it succeeds, return `ErrRunning`
and leave it alone; otherwise remove it (stale) and listen.

**Serve** accepts until `ctx` is done, then shuts down: stop accepting, close
client connections, `Stop(false)` every running turn and wait for it to be
recorded (up to 10s), close every session (killing background processes),
close the store, remove the socket, return nil.

### Wire format

Newline-delimited JSON, one JSON-RPC 2.0 object per line, lines up to
16 MiB. Requests have an `id`; a request without one is ignored. Batches are
not supported (-32600). A line that is not JSON gets an error with a null id;
the connection stays open. Events are notifications:

```json
{"jsonrpc":"2.0","method":"event","params":{"session":"<id>","kind":"<kind>", ...}}
```

Writes to one connection are serialized and never block a turn: each
connection has an outgoing queue, and a client that falls more than 10,000
events behind is disconnected.

### Errors

| code | meaning |
|---|---|
| -32700 | parse error |
| -32600 | invalid request |
| -32601 | unknown method |
| -32602 | invalid params (bad cwd, mode, model, command name, ...) |
| -32001 | `hello` must come first |
| -32002 | unsupported protocol version |
| -32010 | not the driver of this session (or not attached) |
| -32011 | no such session, approval or question |
| -32012 | that approval or question is no longer pending |

### Objects

**session**: `id`, `kind`, `title` (empty until phase 3), `cwd`, `model`,
`reasoning_effort`, `mode`, `status` (`idle` | `running`), `created`,
`updated` (RFC 3339), `preview`, `turn_started` (RFC 3339, only while
running), `context_tokens` (prompt + completion tokens of the latest request,
0 before one), `usage` (`input_tokens`, `output_tokens`, `cached_tokens`,
`reasoning_tokens`, `api_calls`, summed since the daemon loaded the session),
`background_processes` (running count).

**message** (a stored row): `seq`, `role`, `content`, `reasoning`,
`tool_calls` (`[{id, name, arguments}]`, flattened from the stored form),
`tool_call_id`, `tool_name`, `ts`. A user row's `content` is what the user
typed (never `api_content`).

**queue item**: `id`, `kind` (`user` | `notice`), `text`.

### Methods

| method | params | result |
|---|---|---|
| `hello` | `protocol` (1), `client` | `protocol`, `version`, `pid`, `home` |
| `session.list` | `kinds` (default `["interactive"]`), `limit` (default 50) | `sessions` |
| `session.create` | `cwd` (absolute, existing directory) | `session`, `warnings` |
| `session.attach` | `session`, `as` (`driver` \| `viewer`), `after_seq`, `limit` | `session`, `messages`, `more_before`, `gap`, `queue`, `role` |
| `session.history` | `session`, `before_seq`, `limit` | `messages`, `more_before` |
| `session.detach` | `session` | `{}` |
| `session.set` | `session`, any of `model`, `reasoning_effort`, `mode` | `session` |
| `turn.send` | `session`, `text`, `mode` (`queue` default \| `step` \| `now`) | `started`, `queued` (item id), `steered` |
| `turn.interrupt` | `session` | `interrupted` |
| `queue.steer` | `session`, `all` | `started`, `steered` |
| `queue.remove` | `session`, `items` (ids) | `queue` |
| `queue.clear` | `session` | `queue` |
| `approval.answer` | `session`, `approval_id`, `choice` | `{}` |
| `question.answer` | `session`, `question_id`, `answers` (strings) | `{}` |
| `model.list` | `session` (optional) | `models` (`[{name, reasoning_efforts, status, serving}]`), `current`, `statuses` |
| `command.run` | `session`, `name`, `args` | per command |

Driver-only: `turn.*`, `queue.*`, `approval.answer`, `question.answer`,
`session.set`, `command.run` (-32010 otherwise). `session.history` needs no
attach.

- **session.create**: kind `interactive`, source = hello's `client`. The
  prompt is `prompt.Build` with `Memory: true`, `Scope: "common"`, the cwd,
  `Now` in the named local zone (as `cmd/soa-d` does), and
  `[memory].rules_char_limit`; its warnings are returned. Model: the config
  default; effort: resolved for that model as `query` does; mode: the
  config's permission mode. All three are stored on the session.
- **session.attach**: without `after_seq`, the newest `limit` rows (default
  200, at most 1000) and `more_before`. With `after_seq`, the rows after it;
  if more than `limit` exist, `gap: true` and the newest `limit` rows instead
  (the client starts over). Attaching again on the same connection changes
  the role. Attaching as driver makes this connection the driver; a previous
  driver on another connection becomes a viewer and gets a `role` event.
  Closing a connection detaches it everywhere.
- **The live tail.** When the session is running, right after the attach
  response the daemon sends the running turn's events since its last
  committed row (since `turn.start` if nothing is committed yet), including an
  open `approval.request` or `question.request`; `session.updated`,
  `queue.updated` and `role` are not replayed. Consecutive deltas of one kind
  may be merged. Then live events follow, with nothing lost or repeated.
- **model.list**: every model name a session may be set to — the provider's
  configured routes plus its `/models` ids (if that request fails, routes
  only), deduplicated, sorted by name; `reasoning_efforts` from the route
  (`[]` without one). Each row carries the catalog's live `status`
  (`"serving"`, `"unavailable"`, `""` when the endpoint does not say) and
  `serving` (true unless the status is `unavailable`); `statuses` says whether
  the endpoint reported any status at all. The catalog stays complete, so
  naming an unloaded model is still accepted. Statuses are trusted for 10
  seconds; the `/models` listing is read again once they are older, so a mode
  switch shows on the next picker open. Each `model.list` call does one
  catalog lookup; a failed or status-less listing filters nothing and does not
  pay the catalog timeout twice. `current` is the session's model, or the
  config default.
- **session.attach**: if the session's model is reported `unavailable`, the
  attaching client gets a `notice` (`level: warning`), exactly
  `<model> is not being served; pick another model`; a serving model is quiet
  and other attached clients are not warned again. The bounded lookup runs on
  a short-lived goroutine after the session lock is released, so attaching
  does not stall streaming nor the connection's read loop (the next request on
  the connection does not wait for the catalog). `modelStatus` distinguishes a
  failed lookup (`known: false`) from a status-less model.
- **A chat whose model is down**: chats have no picker, so the warning
  `<model> is not being served; pick another model` is sent as a chat message,
  at most once per outage. It is not repeated every turn; it resets only when
  the model is reported serving again or when `/model` changes it. A failed
  status lookup does not clear the outage warning.
- **session.set**: `model` must resolve through `config.Endpoint`;
  `reasoning_effort` must be in the model's route list when it has one (empty
  means the configured default); `mode` is one of the four modes. Model and
  effort apply from the next turn (the prompt never changes), the mode at once.
  Stored on the session; `session.updated` goes to every attached client.

### Turns and the queue

- `turn.send` on an idle session starts a turn at once (`started: true`),
  whatever the mode. While a turn runs: `queue` appends a durable queue item
  (`queued: <id>`); `now` steers (`steered: true`); `step` appends a `step`
  queue item, which steers once the running tools end (`OnStep`), every such
  item joined by a blank line, and otherwise runs from the queue as any item.
  A terminal's enter sends `step`; `/commit`, chats and the relay queue.
- **Steering** (`turn.send now`, `queue.steer` while running): the session's
  `Stop(true)`; the turn ends with status `steered`, and a turn with the steer
  text starts at once, ahead of the queue. Steering again before that turn
  starts appends to its text with a blank line. `queue.steer` takes the oldest
  item, or with `all` every item joined by a blank line, out of the queue. A
  steer never cancels a pending approval or question.
- **Interrupt**: `Stop(false)`; open approvals resolve as `deny` and open
  questions are withdrawn (the tool gets an error result); status
  `interrupted`.
- **What runs next.** After a turn ends `done` (or the steer turn after a
  `steered` one), the oldest queue item starts. After `interrupted`, `error`,
  or a daemon restart, the queue waits: it continues after the next turn that
  ends `done`, or when `queue.steer` is called.
- **Notices**: when `OnNotify` fires, the daemon drains `TakeNotifications`;
  each notice starts a turn at once if the session is idle and its queue is not
  waiting, otherwise it becomes a `notice` queue item. Sessions are created
  with `AsyncDelivery: true`.
- Every queue change sends `queue.updated` with the whole queue.

### Events

| kind | fields |
|---|---|
| `turn.start` | `text` |
| `assistant.delta`, `reasoning.delta` | `text` |
| `tool.start` | `call_id`, `name`, `arguments` (the raw JSON), `summary` |
| `tool.end` | `call_id`, `name`, `result`, `is_error`, `duration_ms` |
| `message` | `message` |
| `turn.end` | `status` (`done` \| `interrupted` \| `steered` \| `error`), `error`, `usage` (`prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`), `api_calls` |
| `approval.request` | `approval_id`, `approval_kind` (`command` \| `write`; `kind` is the event kind), `subject`, `reasons`, `choices` |
| `approval.resolved` | `approval_id`, `choice` |
| `question.request` | `question_id`, `text`, `choices`, `multi_select` |
| `question.resolved` | `question_id` |
| `queue.updated` | `items` |
| `session.updated` | `state` (the session object; `session` is the id, as on every event) |
| `role` | `as` |
| `notice` | `level` (`info` \| `warning` \| `error`), `text` |

- Order: `turn.start`, then `session.updated` (running), the turn's events,
  `turn.end`, then `session.updated` (idle). A step's `message` events follow
  its `tool.end` events.
- `summary` is one line of at most 120 characters: the command for
  `terminal`, the path for the file tools, the pattern for `search_files`,
  the action and id for `process`, the question for `clarify`, the item count
  for `todo`; otherwise the arguments cut to 120.
- `is_error`: the result is a JSON object with a non-empty `error` field.
- `choices`: `["once", "session", "always", "deny"]`, or `["once", "deny"]`
  when the request does not allow remembering.

### Approvals and questions

The session's `Approver` and `Questioner` send the request to every attached
client and wait for the driver's answer; the first answer wins and
`*.resolved` goes to everyone. With no driver attached the request waits (no
timeout) until one attaches and answers, or the turn is interrupted.
`approval.answer` choices are the four `approval.Choice` values. A question's
`answers` become the clarify result (the first answer, or the list for
`multi_select`). Pending requests live in memory in 2a; durable approvals
come with the relay (phase 5).

### Commands (`command.run`)

- `commit`: a turn whose text is a port of `build_commit_prompt`
  (`../son_of_anton_cli/cli_commands_mixin.py:1454`), naming `[git]`'s author
  when both keys are set and ending with `Additional instructions from the
  user: <args>` when args are given. Queued like `turn.send`; same result.
- `stop`: `KillBackground`; result `killed` (count); `session.updated`.
- Anything else: -32602.

### Loaded sessions

A session is loaded (an `agent.Session` in memory) while a client is
attached, a turn runs, or it has running background processes; otherwise it
may be closed. Loading an existing session is `Runner.Resume` with the stored
model, effort and mode, and a provider client for that model.

## cmd/soa-d

`soa-d serve [--home DIR]`: `server.Listen` first (on `ErrRunning`: `soa-d
serve: already running (<socket path>)` on stderr, exit 1), then
`server.New` and `Serve` until SIGINT or SIGTERM; exit 0 after a clean
shutdown. One line on stderr when it starts and when it stops.

## Not in 2a

Titles and compaction (phase 3), the TUI client (2b), durable approvals and
the relay (phase 5), subagents (phase 4), the systemd unit (phase 8), running
`-q`/`-z` through the daemon.

## Refinements of SPEC.md section 5

- The sequence number a client resumes from is the stored row's `seq`
  (`after_seq`, named `from_seq` in SPEC.md); transient events carry none,
  and a reconnecting client gets the live tail instead.
- `queue.steer`, `queue.remove`, `session.set`, `session.history` and the
  `role` event are added; `/sendnow`, `/sendall` and `/cancel` map onto them.
- There is no `assistant.start|end` or `reasoning.start|end`: deltas, the
  `message` events and `tool.start` bound them.

## Done when

All gates in `AGENTS.md` pass, earlier phases included. One commit: summary
`daemon: phase 2a (socket server, sessions, queue, approvals)`, body listing
what changed and any contract-test typo fixed. Aim for under about 2,500 new
lines of non-test Go.
