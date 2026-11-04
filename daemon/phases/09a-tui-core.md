# Phase 9a: the Go TUI, core

Goal: `soa-d tui`, a Go client of the daemon that does what the Textual TUI
does every turn: stream a turn, show tool rows, answer approvals and
questions, queue, interrupt, switch model, effort and mode, start, resume and
quit sessions, and come back after a daemon restart. It is opt-in: the
`son-of-anton` command keeps opening the Textual TUI until phase 9d.

Read first: `SPEC.md` sections 6 (protocol) and 17 (phase 9),
`phases/02a-server.md` (methods, events, objects), `phases/02b-tui-client.md`,
and the reference: `son_of_anton_tui/backend.py` (B), `tui.py` (T), `feed.py`
(F). Where this file and the Python differ, this file wins. Later phases:
9b the sidebar, wordmark, markdown, pickers, completion, palette, mouse,
editor and skins; 9c the subagent view; 9d switching `son-of-anton` over and
deleting the Textual TUI.

## Already written (do not modify)

`internal/client/client_test.go`, `internal/tui/tui_test.go`,
`internal/tui/history_test.go`, `cmd/soa-d/tui_test.go`.

## Dependencies

Bubble Tea v1 (`github.com/charmbracelet/bubbletea`), lipgloss v1 and the
bubbles textarea, at their newest v1 (bubbles: v0) releases; nothing else
from charm. The tests do not import them. Update the Nix `vendorHash`.

## package client (new)

```go
type Error struct { Code int; Message string } // a JSON-RPC error; Error() is the message
type Event struct {
	Session, Kind string
	Params        map[string]any // the event's whole params object
}
type Client struct{ ... }
func Dial(home, name string) (*Client, error)    // <home>/run/soa-d.sock, then hello {protocol: 1, client: name}
func Connect(home, name string) (*Client, error) // Dial, starting the daemon when nothing listens
func (c *Client) Hello() map[string]any          // hello's result
func (c *Client) Call(ctx context.Context, method string, params, result any) error
func (c *Client) Events() <-chan Event
func (c *Client) Done() <-chan struct{} // closed once the connection is gone
func (c *Client) Close() error
```

- Newline-delimited JSON-RPC 2.0, lines up to 16 MiB. Dial gives up after 5s
  to connect and 10s for hello. `Call` decodes the result into `result`
  (nil: discard) and returns `*Error` for an RPC error, the context's error
  on timeout or cancel, and an error once the connection is gone.
- Events never hold up responses: they go through an unbounded queue into
  `Events()`, in order. The channel closes after `Done()`.
- **Connect** (B's `connect`, D:37-212): when Dial finds nothing listening,
  run `<bin> serve --home <home>`, `bin` being `$SOA_D_BIN` or else this
  executable (`os.Executable`), in its own session (setsid), stdin from
  /dev/null, stdout and stderr appended to `<home>/logs/soa-d.log` (the
  directory made 0700), not waited for; then Dial every 50ms for up to 10s.
  A daemon already serving (or one that started meanwhile) is used as is.

## package tui (new)

```go
type Options struct {
	Home      string    // the daemon's home
	Cwd       string    // a new session's directory, and -c's workspace (default: the working directory)
	Continue  bool      // -c
	Resume    string    // a session id, or "latest"
	Model     string    // -m
	Reasoning string    // --reasoning
	Yolo      bool      // --yolo
	Version   string    // shown in the intro
	Autostart bool      // reconnect with client.Connect rather than client.Dial
	Stderr    io.Writer // -c's "nothing to continue" line; nil means os.Stderr
}
type App struct{ ... }
func Start(c *client.Client, o Options, in io.Reader, out io.Writer) (*App, error)
func (a *App) Wait() error
func (a *App) Type(s string)        // one key per rune
func (a *App) Press(keys ...string) // named keys, below
func (a *App) Resize(w, h int)
func (a *App) Screen() string
func (a *App) Session() string
```

- **Start** picks the session, attaches as driver, then (when any of Model,
  Reasoning, Yolo is set) makes one `session.set` with those, and only then
  starts the program on `in`/`out`. Every failure up to there is returned
  before a program starts.
  - Resume `latest`: the newest interactive session anywhere
    (`session.list`). Another id: attach it; -32011 returns an error whose
    text is `session <id> is not in soa-d`.
  - Continue: the newest interactive session of Cwd's workspace
    (`session.list` with `cwd`); none: write `soa-d: no session to continue
    in <cwd>; starting a new one` to Stderr and create one.
  - Otherwise `session.create` with Cwd.
- A nil `in` reads no input and installs no signal handler (tests); keys
  then come only from `Type` and `Press`. `Type`, `Press` and `Resize` send
  to the program, in order, and do nothing once it has ended. Key names:
  `enter`, `esc`, `tab`, `shift+tab`, `up`, `down`, `left`, `right`, `pgup`,
  `pgdown`, `home`, `end`, `backspace`, `space`, `alt+enter`, `ctrl+<letter>`,
  and any single character. `Screen` is what the next
  frame paints, ANSI stripped: exactly as many lines as the height, joined
  with `\n`, none wider than the width. `Screen` and `Session` are computed
  on the program's goroutine, so they are safe from any goroutine; once the
  program has ended they give the last frame and session. Until a size
  arrives the size is 100x30. `Wait` returns when the program ends, and
  again at once on a later call. `Client.Close` may be called more than
  once.
- In a real terminal: the alternate screen, and Bubble Tea's own resize.

### Layout

Content is inset 2 columns on each side, with one blank row at the bottom.
From the bottom up: the status line; a blank row; the prompt frame; a blank
row; the queued strip and a blank row (only while something is queued); the
feed fills the rest. The colour roles below are the default skin's
(`ui_accent` yellow, `ui_thinking` cyan, `ui_tool` yellow, `ui_error` red,
muted = faint); skins are 9b.

- **Prompt frame** (T:1082-1174): a `▎` accent rail on every row, then a
  1-row pad, the text area (1 to 10 rows, placeholder `Message Son of
  Anton…`), a blank row, the meta row, a 1-row pad. The text area grows
  with its text, but to no more than 10 rows and never so far that fewer
  than 3 feed rows remain; past that it scrolls inside itself. Text starts 2 columns
  after the rail. Meta: the mode (yolo bold red, lockdown yellow, others
  plain), muted `  ·  `, the model (`model_short`) bold, muted `  ·  `, muted
  `reasoning <effort or "default">`.
- **model_short** (B:784-818): the model's last `/` segment without
  `.gguf`; under `auto` with a served model, `auto → <short served>`; longer
  than 26 characters: the first 23 and `...`.
- **Status line**: left and right parts, right-aligned. Busy: left is the
  spinner frame bold, a space, the newest running tool's summary or
  `thinking`, then muted `  <N>s` since `turn_started`; right is bold
  `ctrl+c` and ` interrupt`. Idle: left is the cwd with the home directory
  as `~`; right is `<pct>% context  ·  ` when the context length is known,
  then bold `/help` and ` commands`. Left gains muted `  · <N> queued` while
  anything is queued. The spinner is `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` at 120ms; nothing ticks
  while idle.
- **Queued strip** (T:1703-1739): up to three rows of muted `↳ ` and the
  item's text (one line, cut with `…`), `   +<N> more` past three, then a
  muted hint: `/sendnow sends the oldest · /sendall sends all`, or
  `/sendnow sends this now` for one item.

### The feed

Blocks top to bottom, anchored to the bottom (F:683-722): it follows new
output while the view is at the bottom; `pgup` scrolls up a page and stops
following; `pgdown` scrolls down and follows again on reaching the bottom.
Output never moves a scrolled-up view. No row cap.

- Text is padded 3 columns and wraps at the content width minus 6. Blank
  lines between blocks follow F's `_separate`: a user, assistant or
  reasoning block always has one before it; a block after one of those has
  one; a note block of more than one line has one after it; consecutive
  tool rows and one-line notes stay tight.
- **Intro** (T:1439-1461), the first block, muted: `v<version>  ·
  <model_short>  ·  session <id>  ·  <~cwd>`, then `/help for commands · :q
  to quit`. Written once, at start.
- **User** (F:327-371): every row starts with the accent `▎` and 2 blank
  columns, on the panel colour (lipgloss `AdaptiveColor` grey) to the full
  width; a rail-only row above and below; the text bold.
- **Assistant**: the streamed text, wrapped, as plain text with its own
  line breaks kept (markdown is 9b). Deltas are drawn at most every 120ms.
- **Reasoning** (F:497-544): while streaming, `▼ reasoning` then the text
  padded 5, in the thinking colour; when the assistant text starts or the
  turn ends it folds to `▶ reasoning · <N> line(s)`. History arrives folded.
- **Tool rows** (F:425-494): the icon (T:94-110: `$` terminal and
  execute_code, `→` read_file and clarify, `%` web_extract, `◈` web_search,
  `✱` search_files and session_search, `←` write_file, patch and memory,
  `✓` delegate_task, `☰` todo, `◔` cronjob, `◉` vision_analyze, else `⚙`)
  in column 0 in the tool colour, then from column 2 the summary on one
  line, cut with `…`. Running: the spinner frame instead of the icon, and
  muted `  <N>s` once a second has passed. Done: muted, then `  <d.d>s`
  when the duration is not zero; an error shows `✗` and the label in the
  error colour. Rows pair by `call_id`. `turn.end` finishes rows still
  running. History rows (B:218-277) use `tool_summary` from B:61 and show no
  duration.
- **Notes** (F:403-422): muted lines padded 3; consecutive note lines merge
  into one block, at most 100 lines. Echoes: a slash command shows muted
  `⚙ <command>`; a `!` line shows itself muted.
- **Compaction** (T:822-835): a muted `── Compaction ──` rule padded 3, the
  summary as assistant text, then muted `~<before> tokens before` (live) or
  nothing (history).
- A `turn.start` whose text starts with `[Background process` is a muted
  note (its first line, 200 characters), not a user block.

### Events (B:290-620)

`turn.start` adds the user block (or the note above) and makes the session
busy; `reasoning.delta`, `assistant.delta`, `tool.start`, `tool.end`,
`compaction` as above; `turn.end` with `interrupted` adds the note
`interrupted`, with `error` the note `error: <error or "the turn failed">`
(not muted); `session.updated` replaces the session state; `queue.updated`
the queue; `role` with `as` viewer the note `another client is now the
driver of this session`; `notice` a note (muted when `info`);
`approval.request` and `question.request` open modals and their
`*.resolved` close them. `child.*` events are ignored until 9c.

**Attach and history** (B:218-277): the feed is drawn from the attach
result's `messages` (user blocks, assistant text, folded reasoning, tool rows
from `tool_calls`, compactions after the row whose seq is their
`through_seq`; `[response interrupted]` and `[response failed]` rows as
notes), after the note `resumed session <id>` (plus ` · earlier messages not
shown` when `more_before`) for a session that has messages. Then the live
tail.

**Reconnect** (B:630-664): when the connection drops, pending modals close,
the session is no longer busy, and the note `lost the connection to soa-d;
reconnecting…` appears. Retry after 0.5s, doubling to 10s, with
`client.Connect` when Autostart is set (the real command) or `client.Dial`
(tests); each attempt re-attaches as driver with `after_seq` = the last seq
seen. Success: the note `reconnected to soa-d` (on `gap`, the feed is
redrawn from the result first). -32011: the note `session <id> is not in
soa-d` and no more retries.

### Keys

- `enter` sends; `alt+enter` and `ctrl+j` insert a newline. Text that is
  empty after trimming does nothing.
- `up` on the first row recalls older history; `down` on the last row newer,
  and past the newest the draft as it was. A recalled entry is not sent.
- `ctrl+c` (T:2286-2301): busy, the muted note `interrupting… (ctrl+c again
  to force quit)` and `turn.interrupt`; a second press within 2s interrupts
  and quits. Idle with text: clear the prompt. Idle and empty: quit.
- `esc`: while busy, the muted note `interrupting…` and `turn.interrupt`.
- `shift+tab`: the mode goes default → ask → lockdown → yolo → default
  (unknown → default) with `session.set`; failure: the note `could not
  switch permission mode: <error>`.
- `ctrl+l` clears the feed except the intro, locally. `ctrl+q` quits.
  `pgup`/`pgdown` as above.

### Sending and commands (T:2014-2050, B:834-945)

- `/quit`, `/exit`, `/q` and `:q` quit at once, even mid-turn. Quitting
  detaches (`session.detach`, 3s) and closes the connection; it never
  interrupts the turn, which runs on in the daemon.
- `!<cmd>` runs `bash -c <cmd>` in the session's cwd, here, without a model
  turn; its output (stdout and stderr) streams in as notes, then `! exited
  <code>` when not zero; 120s timeout. A bare `!`: `Usage: !<command> — run a
  shell command without spending a model turn (e.g. !git status)`.
- `/word` (no further `/`) is a command; anything else is `turn.send` with
  mode `queue` (error: the note `could not send: <error>`). Everything sent
  from the prompt (text, commands, `!` lines, `:q`) is recorded in history
  first. A command shows its echo, then:

| command | does | note |
|---|---|---|
| `/help` | | `commands:`, then `  /<name> <args> — <what>` per command below, then `  !cmd runs a shell command here · :q quits` |
| `/model X` | `session.set {model}` | `model: X (from the next turn)` |
| `/model` | `model.list` | `model: <current>`, then `  ● name` (current) or `    name` per model |
| `/reasoning X` | `session.set {reasoning_effort}` (`default` sends `""`) | `reasoning: X (from the next turn)` |
| `/reasoning` | `model.list` | `reasoning: <effort or default>  (<the model's efforts joined by " \| ">)`, or `<model> has no configured reasoning levels` |
| `/perm X` | `session.set {mode}` | `permission mode: X` |
| `/perm` | | `permission mode: <m>  (default \| ask \| lockdown \| yolo)` |
| `/new`, `/reset`, `/clear` | `session.create`, attach it as driver, detach the old | the feed starts over with a new intro |
| `/resume ID`, `/sessions ID` | attach that session as driver, detach the old | the feed is redrawn; -32011: `session ID is not in soa-d` |
| `/resume`, `/sessions` | `session.list` (limit 10) | `  <id>  <title or untitled>  ·  <cwd>` per session, or `no sessions to resume` |
| `/pickup ID` | the worker with that id or title (case-insensitive) from `session.list {kinds: ["worker"]}`, then as `/resume` | `no worker ID` |
| `/commit [args]` | `command.run {name: "commit", args}` | |
| `/stop` | `command.run stop` | `killed <N> background process(es)` |
| `/compact --preview` (or `--dry-run`) | `command.run compact` with that arg | `would summarize <rows> rows (through seq <s>, about <t> tokens) and keep the rest verbatim` |
| `/compact [args]` | `command.run compact` (600s) | `compacting…` first |
| `/sendnow TEXT` | `turn.send {mode: "now"}` | |
| `/sendnow`, `/sendall` | `queue.steer {all}` | `nothing queued to send` when neither started nor steered |
| `/cancel` | `queue.clear` | `dropped <N> queued message(s)` |
| `/status` | | `session <id> · <model> · mode <mode> · in <in> out <out> · <calls> calls` |

Anything else: `unknown command /<word> (/help lists them)`. A daemon error:
`/<name>: <message>`. Numbers with thousands get commas (`12,345`).

### Modals (T:533-710, 1812-1959)

One at a time, queued in arrival order, deduplicated by id; a modal's
`*.resolved` or the connection dropping closes it. While one is open the whole
screen is dimmed (the backdrop colour, muted text) and the modal is drawn on an
opaque card centred on the screen (rounded border in the accent colour, warning
yellow for approvals; width 76 or the content width), keys go to it, and the
prompt keeps its draft.

- **Approval**: title `The agent wants to run a command` (`Save to memory?`
  for kind `memory`), the subject bold, the reasons joined by `; ` muted,
  then the choices numbered from 1 with the labels and descriptions of
  T:1873-1899 (`Allow once` run this now, `Allow for this session` no more
  prompts for this until exit, `Always allow` add to the allowlist, `Deny`
  the agent is told it was refused; memory: `Save` write it to memory,
  `Don't save` the agent is told it was refused), only those the request
  offers. A digit picks; `up`/`down` and `enter` pick; `esc` answers
  `deny`. Hint: `1-9 pick · ↑↓ move · enter pick · esc cancel`.
  `approval.answer`; -32011 and -32012 are ignored.
- **Question**: title `Son of Anton needs your input`, the question, then
  its choices numbered and `Other…` (type your own answer). No choices, or
  Other: a one-line text input (`your answer`), `enter` sends it. With
  `multi_select`: `space` toggles the highlighted choice, `enter` sends the
  picked ones (and the typed text, when Other was picked, after them).
  `esc`, or an empty answer, sends `The user dismissed the question without
  answering; use your best judgement.` `question.answer`.

### History file (T:244-356)

`<home>/.son_of_anton_history`, prompt_toolkit's format, shared with the
Textual TUI: entries as `\n# <YYYY-MM-DD HH:MM:SS.ffffff>\n` and each line of
the entry prefixed `+`.

```go
func ReadHistory(path string) []string     // oldest first
func AppendHistory(path, entry string) error
```

`ReadHistory` reads at most the last 512 KiB (a cut first line is not a `+`
line, so it falls away) and keeps the newest 500 entries; a missing file is
empty. `AppendHistory` appends one entry with the current local time,
creating the file 0600. The app records a sent text with its surrounding
newlines trimmed, skips one that is blank or equal to the newest entry, and,
when the file cannot be written, keeps history in memory only.

## soa-d tui

`soa-d tui [--home DIR] [--in DIR] [-c | --resume ID|latest] [-m MODEL]
[--reasoning LEVEL] [--yolo]`: the home as `serve` resolves it, `client.Connect`
(name `tui`), `tui.Start` on the terminal with Autostart and the version,
then `Wait`. A bad or unknown flag, `-c` with `--resume`, or a missing value:
the usage, exit 2. A connect or start failure: `soa-d: <error>`, exit 1. Add
it to the usage text.

## README

Under "Sessions and the command line": one line for `soa-d tui`, opt-in
while the Textual TUI is the default.

## Decisions made without the owner

- The Go TUI is opt-in until it matches the Textual one (9d), so the owner's
  production test of the Textual TUI is untouched.
- Bubble Tea v1, lipgloss and bubbles are the one new dependency set: a
  terminal UI from the standard library alone would be a library of its own.
- Without pickers until 9b, `/model`, `/reasoning`, `/resume` and `/cancel`
  with no argument list or act instead of opening a picker; `/cancel` drops
  the whole queue.
- Unknown and not-yet-ported commands say `unknown command`: the Go TUI has
  no registry of Python commands to say "not available" from.
- A reconnect in tests dials only; the real command restarts the daemon as
  startup does.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md` (with `go test -race
./internal/client/ ./internal/tui/` as well), `ruff check .` and `ruff check
--select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole suite),
and `nix flake check`. One commit: summary `daemon: phase 9a (Go TUI core)`,
body listing what changed, ending with the `Model:` line.
