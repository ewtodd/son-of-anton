# Phase 1b: background processes, todo, clarify, prompt composition, oneshot

Goal: the rest of the core tools, the layered system prompt, the `oneshot`
command (the `-z` replacement), and the provider's idle timeout.

Read first: `SPEC.md` sections 4, 7 (including "Prompt composition" and
"Delegated rules"), 8 and 10, `AGENTS.md`, and the phase 0 and 1a code. The
Python behavior was surveyed; references are into `../`. Where this file and
the Python differ, this file wins; the differences that fix Python bugs are
marked **(fix)**.

## Already written (do not modify)

- Contract tests: `internal/prompt/prompt_test.go`,
  `internal/tools/process_test.go`, `internal/tools/todo_test.go`,
  `internal/tools/clarify_test.go`, `internal/agent/session_test.go`,
  `cmd/soa-d/oneshot_test.go`, and two tests appended to
  `internal/provider/provider_test.go`.
- `internal/prompt/guidance.md`: the guidance text, used verbatim.
- `internal/fakellm` gained `StallAfterFirst`.
- Two 1a assertions were loosened in this contract (the tool list grows): the
  builtin-order check in `internal/tools/tools_test.go` and the tool count in
  `cmd/soa-d/main_test.go`. All 1a and phase 0 tests must keep passing.

## package prompt (`internal/prompt`, new)

```go
const DefaultIdentity = "You are Son of Anton Agent, ..." // see below
var Guidance string // guidance.md, embedded, surrounding whitespace trimmed

type Options struct {
    Home, Cwd, Scope string // instance home; session cwd; "common" | "gateway"
    Memory           bool   // identity + all rules (true), or lean (false)
    Now              time.Time
    RulesCharLimit   int    // 0 means 6000
}
type Result struct {
    Text         string
    Warnings     []string // e.g. an unparseable rules.toml
    ContextFiles []string // absolute paths of the project files loaded
}
func Build(o Options) (Result, error)
```

`DefaultIdentity` is the Python's `DEFAULT_AGENT_IDENTITY`
(`../agent/prompt_builder.py:130-138`), verbatim.

Layers, joined with a blank line, in this order (the order is tested):

1. Identity, only when `Memory`: `<Home>/SOUL.md` trimmed, else
   `DefaultIdentity`.
2. `Guidance`.
3. Environment: `Host: Linux` and `Current working directory: <Cwd>` on two
   lines.
4. Project context, when any file is found:
   `# Project Context\n\nThe following project context files have been loaded and should be followed:\n\n`
   then one `## <path relative to the git root, or to Cwd without git>`
   section per file, each followed by its content.
5. Rules, when any apply: a header line containing `STANDING RULES` (say
   which scope and how many characters of `RulesCharLimit` are used; no em
   dashes), then one line per rule: `- [<id>] <text>` plus ` (<why>)` when
   there is a why.
6. `Conversation started: <Now formatted "Monday, January 2, 2006"> (<Now's
   location name>)`.

Rules come from `<Home>/memories/rules.toml` (`[[rule]]` tables: `id`,
`text`, optional `why`, `scope` default `common`, `source`, optional
`delegated` default false). A rule applies when its scope is `common`, or
`gateway` and `Scope` is `gateway`. With `Memory` false only rules with
`delegated = true` are kept. A missing file means no rules; an unparseable
one means no rules block and a warning.

Context files, ported from `../agent/prompt_builder.py:696-867`. The git root
is the nearest ancestor of `Cwd` (inclusive) containing `.git`; without one,
only `Cwd` is searched. The first kind found wins and only that kind loads:

1. `.son-of-anton.md` or `SON_OF_ANTON.md`: the nearest one from `Cwd` up to
   the git root.
2. The AGENTS chain: in each directory from the git root down to `Cwd`, the
   first of `AGENTS.override.md`, `AGENTS.md`, `agents.md`; merged root first,
   duplicates by content dropped.
3. `CLAUDE.md` or `claude.md` in `Cwd` only.
4. `.cursorrules` and `.cursor/rules/*.mdc` in `Cwd` only.

The whole block is capped at 40,000 characters: keep the first 70% and the
last 20% around `[... truncated: read <path> for the rest ...]`. Not ported:
threat scanning, frontmatter stripping, subdirectory hints, the coding brief,
platform hints, model-specific guidance.

The same `Options` always give the same bytes.

The guidance text includes the send-time rule: a user message starting with
`[Day YYYY-MM-DD HH:MM ZONE]` — or carrying one right after a compaction
summary — was sent at that time, and a later time is known only if the model
can run commands (`date`); otherwise it is not guessed. `prompt.FormatSentAt`
renders that form in `prompt.LocalZone`; `prompt.SentTimeRule` is the rule's
`[Day YYYY-MM-DD HH:MM ZONE]` token. `[agent] message_timestamps` (default on)
prefixes a turn's user message in the API copy, not in the stored content —
and only while the session's system prompt carries the rule, so a `--system`
one-shot with a rule-less prompt, and a resumed session whose stored prompt
predates the rule (until compaction rebuilds it), get no prefix.

## package tools

```go
type Question struct {
    Text        string
    Choices     []string
    MultiSelect bool
}
type Questioner func(ctx context.Context, q Question) ([]string, error)
```

`Env` gains `Ask Questioner` (nil: nobody can answer) and `AsyncDelivery
bool` (whether a completion notification can ever be delivered). `State`
gains `TakeNotifications() []string` (drains), `RestoreTodos(result string)
error` (from a stored `todo` result), and `Close` now also kills the
session's background processes. `Builtin()` returns, in this order:
`terminal`, `process`, `read_file`, `write_file`, `patch`, `search_files`,
`todo`, `clarify`.

### terminal: background

New params `background` and `notify_on_complete` (no `watch_patterns`, no
`pty`). Reference: `../tools/terminal_tool.py:1321-1515`,
`../tools/process_registry.py`.

- Same gate and refusals as the foreground (a trailing `&` is refused with or
  without `background`). Blocked -> the 1a blocked result, nothing starts.
- Starts `bash -c` that sources the session's env snapshot, `cd`s to the cwd
  (`workdir` or the session's) and evals the command; no snapshot dump, no
  cwd marker. Own process group, stdin `/dev/null`, stdout and stderr into
  one rolling buffer holding the most recent 200,000 characters.
- Returns `{"output": "Background process started", "session_id":
  "proc_<12 hex>", "pid", "exit_code": 0}`.
- `notify_on_complete` with `AsyncDelivery` false: the process still starts;
  the result adds `"notify_unsupported": true` and one sentence saying no
  notification can arrive in this run. The flag is recorded before the
  process starts **(fix: the Python sets it after, so a fast exit never
  notifies)**.
- Processes belong to the `State` that started them **(fix: the Python
  stamps every process `task_id="default"`, so nothing ever reaps them)**.

### process

Params: `action` (`list|poll|log|wait|kill`), `session_id` (required except
for `list`; a unique prefix of 4+ characters resolves, with or without
`proc_`), `timeout` (wait), `offset` / `limit` (log). Any other action is an
error listing these five (the Python's `write` / `submit` / `close` always
failed: stdin is `/dev/null`, **fix**). Only this State's processes are
visible.

- `list`: `{"processes": [{"session_id", "command" (first 200 chars), "pid",
  "status": "running"|"exited", "started_at" (RFC 3339), "uptime_seconds",
  "exit_code" (when exited)}]}`.
- `poll`: `{"session_id", "command", "status", "pid", "uptime_seconds",
  "output_preview" (last 1000 chars, ANSI stripped), "exit_code" (when
  exited)}`.
- `log`: lines of the buffer; `offset` is 0-based, default is the last
  `limit` lines; `limit` default 200. `{"session_id", "status", "output",
  "total_lines", "showing"}`.
- `wait`: `timeout` default 180, max 600, at least 1. Exited: `{"status":
  "exited", "exit_code", "output" (last 2000)}`. Timeout: `{"status":
  "timeout", "process_running": true, "output" (last 1000), "timeout_note"}`.
  Context cancelled: `{"status": "interrupted"}`; the process keeps running.
- `kill`: SIGTERM to the process group, SIGKILL after 2s. `{"status":
  "killed", "exit_code" (the real one)}`; an exited process gives
  `"already_exited"`.
- `wait`, `log` and `kill` on an exited process consume its notification;
  `poll` does not.

Notifications: when a process started with `notify_on_complete` (and
`AsyncDelivery`) exits and its result has not been consumed, queue one text
for `TakeNotifications`: `[Background process <id> exited with code <n>.
Command: <command>. Output (last 2000 chars):\n<output>]`. Delivering it as a
turn is a later phase's job.

### todo

Port `../tools/todo_tool.py` (schema at 294-351): `todos` (array of `{id,
content, status}`, status one of `pending|in_progress|completed|cancelled`)
and `merge` (default false); no `todos` reads the list. Replace mode keeps the
last item per id (in that item's position) and maps an invalid status to
`pending`. Merge mode skips items without an id, updates only non-empty
content/status of existing ids (an invalid status is ignored), and appends new
ids. A JSON string in `todos` is parsed; anything else not a list is an error.
Caps: 256 items, 4000 characters of content plus `... [truncated]`. Result:
`{"todos": [...], "summary": {"total", "pending", "in_progress",
"completed", "cancelled"}}`. The list lives in the `State`. Not ported: the
in_progress reordering, the compaction re-injection (phase 3).

### clarify

Params: `question` (required, non-empty), `choices` (up to 4 kept),
`multi_select`. No batch `questions`. With `Ask`: ask, and return
`{"question", "choices_offered", "user_response"}` where `user_response` is
the first answer, or the list of answers for `multi_select` (never split on
commas, **fix**). With no `Ask`: `user_response` is "No one can answer
questions in this run. Make the most reasonable choice yourself, say which
one you made and why in your final answer, and continue." (a string, even for
`multi_select`, **fix**: the Python's text is mangled by its comma split).

## package provider

`Client` gains `IdleTimeout time.Duration` (default 10 minutes). If no byte
arrives for that long, from sending the request to the end of the stream,
the attempt is abandoned: before any delta reached the caller it is a
transient failure (retried like a connection error); after, it is an error.

## package agent

`Runner` gains `AsyncDelivery bool` and `Ask tools.Questioner`, passed into
each session's `Env`. `Session` gains `TakeNotifications() []string`.
`Resume` restores the todo list from the newest stored `todo` tool row.
`Close` closes the State (killing background processes). `Result` gains
`APICalls int`, and its `Usage` sums every request of the turn.

## cmd/soa-d

- `query` gains `--system FILE`, `--memory` (the default) and `--no-memory`.
  Its prompt is `prompt.Build` with `Scope: "common"`, the session cwd, and
  `RulesCharLimit` from `[memory].rules_char_limit`; warnings go to stderr.
  `--system FILE` replaces the whole prompt with the file's bytes.
- `oneshot [--home DIR] [--model M] [--yolo] [--system FILE] [--memory |
  --no-memory] [--usage-file PATH] PROMPT`:
  - lean by default (`Memory: false`), session kind `oneshot`, source
    `oneshot`, no approver, no questioner, `AsyncDelivery` false; the tools,
    mode and allowlist as `query`;
  - stdout carries only the final answer plus a newline, and only on
    success; exit 0;
  - an error, or an empty final answer, prints `soa-d oneshot: <reason>` to
    stderr, nothing to stdout, exit 1; a bad invocation exits 2;
  - `--usage-file` writes JSON (indented) even on failure:
    `input_tokens`, `output_tokens`, `cache_read_tokens`, `reasoning_tokens`,
    `total_tokens`, `api_calls`, `model`, `session_id`, `completed`, `failed`,
    and `failure` when it failed;
  - the session is closed at the end (its background processes die with it).
- Both commands close their session before exiting.

## Nix

Add `git` to the soa-d package's `nativeCheckInputs` (the prompt tests create
repositories).

## Done when

All gates in `AGENTS.md` pass, phases 0 and 1a included. One commit: summary
`daemon: phase 1b (background processes, todo, clarify, prompt, oneshot)`,
body listing what changed and any contract-test typo fixed. Aim for under
about 3,000 new lines of non-test Go.
