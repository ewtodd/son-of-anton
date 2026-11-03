# Phase 5a: Signal chats

Goal: soa-d serves an instance's Signal chats itself: it reads signal-cli's
event stream, runs each chat as a `chat` session, answers in the chat, and
takes the control commands and approvals there. fiona's identity and toolset
(5b) and the relay (5c) build on this.

Read first: `SPEC.md` sections 3, 4 (invariant 6) and 12, `phases/02a-server.md`
("Turns and the queue"), `phases/03c-session-search.md` (the wall), and the
code. The reference is the Python gateway: `gateway/platforms/signal.py` (SIG),
`gateway/platforms/signal_format.py`, `gateway/platforms/base.py` (BASE),
`gateway/run.py` (RUN), `gateway/status.py`. Where this file and the Python
differ, this file wins; several Python bugs are fixed here on purpose and are
marked **fix**.

How the owner's instances use Signal (`/etc/nixos/modules`): every instance
shares one Signal number, and signal-cli runs as a linked device of the
owner's own account (`--send-read-receipts`, so receipts are signal-cli's
job). Each instance owns its groups (`SIGNAL_GROUP_ALLOWED_USERS`) and drops
the rest; DMs are ignored (`SIGNAL_DM_MODE=ignore`) except Note to Self. The
owner's messages in a group therefore arrive as **sync messages from the
account itself**, and so do the echoes of the bot's own replies: the echo
filter is what keeps the bot from answering itself.

## Already written (do not modify)

`internal/signalfake/` (a fake signal-cli HTTP daemon), `internal/signal/*_test.go`
and `internal/signal/testdata/format_cases.json` (generated from the Python
`markdown_to_signal`), `internal/server/chat_server_test.go`.

## When Signal runs

Only with `[daemon] signal = true` in config.toml (default false), so a
daemon a TUI autostarts never competes with the Python gateway. Settings, from
the process environment, then the home's `.env`, as the Python reads them:

| setting | source | meaning |
|---|---|---|
| URL | `SIGNAL_HTTP_URL` | signal-cli's HTTP daemon, trailing `/` dropped |
| account | `SIGNAL_ACCOUNT` | the bot's number |
| senders | `SIGNAL_ALLOWED_USERS` | comma list of numbers or UUIDs; empty denies everyone; `*` allows anyone (**fix**: the Python ignored `*`) |
| groups | `SIGNAL_GROUP_ALLOWED_USERS` | comma list of group ids; empty drops every group; `*` allows all |
| DM mode | `SIGNAL_DM_MODE` | `allow` (default; anything unknown counts as allow) or `ignore` |
| mentions | `[platforms.signal] require_mention`, else `SIGNAL_REQUIRE_MENTION` (`true/1/yes/on`) | groups only |
| typing | `[platforms.signal] typing_indicator` | default true |

Missing URL or account: a warning in the log and no Signal. Other config:
`gateway.model` (non-empty beats `model.default` for chat sessions),
`gateway.single_user` (default false), `gateway.group_sessions_per_user`
(default true), `terminal.cwd` (a chat session's directory; `.`, `auto`,
`cwd` or empty mean the user's home).

**The lock** (`gateway/status.py` `acquire_scoped_lock`): before connecting,
take the same file the Python gateway takes,
`<dir>/signal-phone-<first 16 hex of sha256(account)>.lock`, `<dir>` being
`SON_OF_ANTON_GATEWAY_LOCK_DIR`, else `$XDG_STATE_HOME/son-of-anton/locks`
(XDG default `~/.local/state`). The file is a JSON record with at least `pid`,
`start_time` (field 22 of `/proc/<pid>/stat`), `scope` ("signal-phone"),
`identity_hash`, `metadata` and `updated_at`. A record whose `pid` is alive
with a matching `start_time` (or none to compare) holds it: Signal stays off
with an error in the log naming the pid. A dead pid, a different start time,
or an unreadable file is stale and is replaced. Remove the file at shutdown.

## package signal (new)

```go
type Config struct {
    URL, Account   string
    Senders        []string // "*" anyone
    Groups         []string // "*" any
    DMMode         string   // "allow" | "ignore"
    RequireMention bool
    PerUser        bool     // group sessions per sender
}
func ConfigFromEnv(get func(string) string) Config // Senders, Groups, DMMode, RequireMention (env), URL, Account; PerUser true

type Attachment struct { ID, ContentType, Filename string; Size int64 }
type Quote struct { ID int64; Text string; Own bool } // Own: it quotes one of our replies
type Inbound struct {
    Chat       string // the chat stamp and session key: "signal:dm:<sender>", "signal:group:<id>", "signal:group:<id>:<sender>"
    To         string // where replies go: the sender, or "group:<id>"
    Sender, SenderUUID, SenderName, Group, GroupName string
    Text       string
    Timestamp  int64
    Attachments []Attachment
    Quote      *Quote
}

type Intake struct{ … }
func NewIntake(cfg Config) *Intake
func (in *Intake) Accept(raw []byte) (Inbound, bool) // one SSE data payload
func (in *Intake) Sent(ts int64)                       // a send of ours, for the echo filter and Quote.Own

func Format(markdown string) (text string, styles []string) // markdown_to_signal

type Client struct{ … }
func NewClient(url, account string) *Client
func (c *Client) Check(ctx context.Context) error                                    // GET /api/v1/check
func (c *Client) Events(ctx context.Context, handle func(data []byte)) error         // runs until ctx ends
func (c *Client) Send(ctx context.Context, to, text string, styles []string) (int64, error)
func (c *Client) Typing(ctx context.Context, to string, stop bool) error
func (c *Client) Attachment(ctx context.Context, id string) ([]byte, error)
```

**Intake** (SIG `_handle_envelope`, 571-807). In order:
1. Unwrap `envelope`. A `syncMessage.sentMessage` whose destination
   (`destinationNumber` or `destination`) is the account (Note to Self) or
   that carries `groupInfo.groupId` (the owner writing from another device)
   becomes the data message, unless its `timestamp` is one of ours (`Sent`;
   remember 512, for 5 minutes): that is an echo, dropped. Every other sync
   message is dropped.
2. Sender: `sourceNumber`, else `sourceUuid`, else `source`. A non-sync
   message from the account itself is dropped. `storyMessage` is dropped.
   The data message is `dataMessage` or `editMessage.dataMessage` (an edit is
   a new message).
3. Group (`dataMessage.groupInfo.groupId`) or DM. Groups: not in `Groups`,
   dropped. DMs: mode ignore drops them, except Note to Self.
4. The sender must be in `Senders` (by number or by UUID). Nothing is fetched
   or sent for a dropped message (**fix**: the Python fetched attachments
   first).
5. Mentions: each `mentions[{start, length, number, uuid}]` replaces its range
   of the text with `@<number or uuid>`, offsets in UTF-16 code units
   (**fix**: the Python used code points). With `RequireMention`, a group
   message must mention the account (by number, or by the account's own UUID
   once seen as a `sourceUuid` of the account) or contain `@<account>`;
   `@<account>` and `@<own uuid>` are then removed, doubled spaces collapsed,
   the text trimmed.
6. Quote: `quote.id`, `quote.text`; `Own` when `quote.id` is one of our last
   500 send timestamps (**fix**: on the owner's setup every message's author
   is the account, so the author cannot tell).
7. Attachments: `id`, `contentType`, `filename`, `size`; over 100 MB skipped.
8. No text left and no attachments: dropped (profile keys, receipts, typing,
   reactions).
9. Chat: `signal:dm:<sender>` (Note to Self: `signal:dm:<account>`), or
   `signal:group:<id>`, plus `:<sender>` when `PerUser`.

**Format** is `markdown_to_signal` exactly: the cases in
`testdata/format_cases.json` were produced by the Python function.

**Client** (SIG 403-561, 967-1222); `NewClient` drops a trailing `/`:
- `Events`: `GET /api/v1/events?account=<url-escaped account>`,
  `Accept: text/event-stream`; lines starting `:` are keepalives; each
  `data:` line is one payload. Reconnect at 2 s doubling to 60 s with 0-20%
  jitter, reset after a good connect. With no activity for 120 s, `Check`;
  failure closes the stream to reconnect.
- RPC: `POST /api/v1/rpc`, `{"jsonrpc":"2.0","method","params","id"}`, 30 s
  timeout; an `error` is an error.
- `Send`: method `send`, params `account`, `message`, then `textStyle` (one
  style) or `textStyles` (several) or neither, then `groupId` (to without its
  `group:` prefix) or `recipient: [to]`. Every `results[*].type` must be
  `SUCCESS`. Returns the result's `timestamp`.
- `Typing`: `sendTyping` with `account` and `groupId`/`recipient`, plus
  `"stop": true` to stop.
- `Attachment`: `getAttachment {account, id}`; the result is base64, or an
  object with `data` in base64.

## Chat sessions (server)

- A chat's current session is found by its stamp (schema v9: a `chats` table,
  `chat` → `session_id`); none, or `/new`, makes one: kind `chat`, source
  `signal`, scope `gateway`, `Chat` the stamp, cwd from `terminal.cwd`, model
  from `gateway.model` or `model.default`, a full prompt (`Memory: true`,
  scope gateway) and recall. Its tools until 5b: `memory`, `session_search`,
  `clarify`, `todo` (no terminal, no files).
- The chat drives its sessions in-process. Nothing on the socket may reach a
  chat or gateway session: `session.list`, `session.attach`,
  `session.history`, `session.children` treat one as not found, and the TUI's
  lists never show one (SPEC invariant 6). `session_search` from a chat
  session: on a shared instance (`gateway.single_user` false) it reads common
  sessions plus those with the same chat stamp; with `single_user` it reads
  everything (as 3c).
- **A message** becomes the user text: `[Replying to your previous message:
  "<quote, first 500 chars>"]` or `[Replying to: "<quote>"]` and a blank line
  when it quotes something; the text; then, for each attachment, fetched and
  saved as `<home>/cache/signal/<timestamp>_<filename, or id plus an
  extension from the content type>`, a line `[The user sent <filename>
  (<content type>), saved at <path>]`. Idle: a turn starts. Running: it is
  queued (its own turn later), and the chat gets "Queued for the next turn.
  I'll respond once the current task finishes." at most once per 30 s.
- **Replies**: a turn that ends `done` sends its final content through
  `Format`, or "The model returned no response." when empty; `error` sends
  "Sorry, that failed: <error>"; `interrupted` and `steered` send nothing. Each send is
  passed to `Intake.Sent`. While a turn runs and typing is on, `sendTyping`
  every 5 s; a stop before each send. Notice turns reply like any other.
- **Control commands**, handled at once whether or not a turn runs, never
  queued and never sent to the model (a first word that is `/` and letters,
  with no other `/`):
  - `/stop`: interrupt and drop the chat's queue; "Stopped. You can continue
    this session." Idle: "No active task to stop."
  - `/new`: interrupt if running, then a new session for this chat (the old
    one stays in the store); "New session started." No confirmation.
  - `/sendnow [text]`: running, with text: steer it in, "Steered into the
    current turn."; bare: steer the oldest queued message, "Steered the
    oldest queued message into the current turn." plus " (<n> still
    queued)" when more wait, or "Nothing queued to steer." Idle, with text: a
    normal turn; bare: "Nothing is running."
  - `/sendall`: running: steer every queued message, "Steered <n> queued
    messages into the current turn." or "Nothing queued to steer."; idle:
    "Nothing is running."
  - `/status`: one message: the session id and title, the model (and served
    model when known), context use, running or idle, queued count, and
    approvals waiting.
  - `/approve [id] [always]`, `/deny [id] [reason]`: below.
  - Anything else: "Unknown command /<name>. Commands: /new, /stop, /status,
    /sendnow, /sendall, /approve, /deny."
- **Approvals** of a chat session go to its chat: "⚠️ Approval needed:
  <what>" then the subject (a command in a code block) and the reasons, then
  "Reply /approve, /approve always, or /deny." ("always" only when allowed).
  With several waiting, each names its id (the approval id without `ap_`) and
  `/approve <id>` takes any unique prefix of 4 or more; a bare `/approve` or
  `/deny` answers the oldest. `/deny` with a reason passes it to the model.
  While one waits, a message of only `yes`, `y`, `ok`, `okay`, `approve`,
  `confirm` or 👍 approves the oldest, `no`, `n`, `deny`, `reject`, `cancel`
  or 👎 denies it, `always` approves always; any other message is handled as
  usual. Replies: "Approved.", "Approved, and remembered.", "Denied.",
  "Nothing is waiting for approval." No timeout: silence is not consent;
  `/stop` and `/new` deny what waits.
- **Questions** (`clarify`): "❓ <question>", a blank line, "  <i>. <choice>"
  lines, a blank line, "Reply with the number, the option text, or your own
  answer." The chat's next message that is not a command answers it (a number
  picks that choice).

## Not in 5a

fiona's identity and toolset, images to the model, the notes folder (5b);
relay and workers (5c); cron delivery (6); sending files; reactions (the
owner never enabled them); history backfill (the Python had none).

## README

Status: Signal moves from "Still Python" to "On soa-d now (off until an
instance moves; `[daemon] signal = true`)". One short paragraph under
"Signal": what the chat accepts (the control commands) and that a typed
`/new` no longer asks to confirm.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 5a
(Signal chats)`, body listing what changed, ending with the `Model:` line.
