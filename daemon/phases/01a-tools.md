# Phase 1a: the tool loop, five tools, approvals

Goal: the agent loop runs tools. Five tools (`terminal`, `read_file`,
`write_file`, `patch`, `search_files`), a path policy, and the approval gate
with permission modes. `soa-d query` gets the tools.

Read first: `SPEC.md` sections 4, 7, 8 and 10, `AGENTS.md`, and phase 0's code.
The Python behavior was surveyed (references below are into `../`); where this
file differs from the Python, this file wins: several differences fix real
Python bugs and are marked **(fix)**.

## Already written (do not modify)

Contract tests: `internal/approval/approval_test.go`,
`internal/paths/paths_test.go`, `internal/tools/tools_test.go`,
`internal/agent/toolloop_test.go`, and additions at the end of
`internal/provider/provider_test.go`, `internal/config/config_test.go`,
`internal/store/store_test.go` and `cmd/soa-d/main_test.go`. Phase 0's tests
must keep passing.

## Dependencies

Phase 0's, plus `golang.org/x/text` (NFKC normalization) if you need it.
Runtime tools: `bash` and `rg` (ripgrep), found on PATH.

## package approval (`internal/approval`)

```go
type Mode string
const (Default Mode = "default"; Ask Mode = "ask"; Lockdown Mode = "lockdown"; Yolo Mode = "yolo")
func ParseMode(s string) (Mode, error)

type Choice string
const (Once Choice = "once"; Session Choice = "session"; Always Choice = "always"; Deny Choice = "deny")

type Request struct {
    Kind          string   // "command" | "write"
    Subject       string   // the command as given, or the absolute path
    Reasons       []string // the findings' descriptions
    PatternKeys   []string // the findings' keys
    AllowRemember bool     // false: session/always are not offered (and count as once)
}
type Approver func(ctx context.Context, r Request) (Choice, error)
type Allowlist interface {
    Has(key string) bool
    Add(key string) error
}

type Finding struct {
    Key, Description string
    Hardline         bool
}
func Classify(command string) []Finding // nil/empty = safe

type Verdict struct {
    Allowed, Asked bool
    Message        string // shown to the model when not allowed
}
func NewGate(mode Mode, approver Approver, allow Allowlist) *Gate // approver/allow may be nil
func (g *Gate) Command(ctx context.Context, command string) Verdict
func (g *Gate) Write(ctx context.Context, absPath string, protected bool) Verdict
```

### Classification

Port the pattern data from `../tools/approval.py`: `HARDLINE_PATTERNS`
(321-358), the parser limits (1014-1044), the `sudo -S` guard (383-403), and
`DANGEROUS_PATTERNS` (532-774, 72 entries, with the sensitive-path fragments
at 170-238). Each finding gets a stable short `Key` (a slug) and a
human-readable `Description`.

- **Normalize** before matching: strip ANSI escapes and NUL bytes; NFKC; join
  backslash-newline continuations; drop backslash escapes inside words
  (`r\m` -> `rm`) and empty `''` / `""`; replace `$IFS` / `${IFS}` with a
  space; fold `$HOME`, `${HOME}` and the absolute user home
  (`os.UserHomeDir`) to `~`.
- **Command positions:** split into segments at `;`, `&&`, `||`, `|`, `&`,
  newlines, `(`, `{`, `$(` and backticks. Hardline patterns match only at a
  segment's start, optionally after `sudo [flags]`, `env VAR=...`, or the
  `exec` / `nohup` / `setsid` / `time` / `timeout N` wrappers. So
  `echo shutdown` and `grep -r "rm -rf /" .` are not hardline.
- **Soft patterns** match anywhere in the normalized command, quoted text
  included (`psql -c "DROP TABLE x"` is flagged), except the pattern operand
  of `grep` / `rg` / `ag` / `ack`, which is hidden first.
- The payload of `bash|sh|zsh|ksh -c "<payload>"` is classified as a command
  too (recursively), so `bash -c "rm -rf build"` is flagged.
- Not findings in 1a: interpreter `-c` / `-e` flags and pager flags (the
  Python flags them, then an LLM pre-screen clears most; without that
  pre-screen they would prompt constantly).
- **(fix)** The Python sensitive-write fragments say `config.yaml`; the real
  file is `config.toml`. Use `config.toml`.
- The `rm -f /tmp/son-of-anton-...` exemption is not needed.

### The gate

| mode | hardline | dangerous | safe | protected write | other write |
|---|---|---|---|---|---|
| `yolo` | blocked | allowed | allowed | **asks** | allowed |
| `default`, `ask` | blocked | asks | allowed | asks | allowed |
| `lockdown` | blocked | asks | **asks** | asks | **asks (fix)** |

- `default` and `ask` behave the same in 1a; the LLM pre-screen that makes
  `default` ask less comes later.
- A hardline finding is never asked about: blocked with a message starting
  `BLOCKED (hardline):`.
- In `default`/`ask`, a command whose finding keys are all approved for this
  gate's session, or all in the allowlist, is allowed without asking.
- Asking: `AllowRemember` is true for dangerous commands in default/ask, false
  for lockdown and for writes. `Once` allows. `Session` allows and remembers
  the keys for this gate. `Always` allows, remembers, and `Allowlist.Add`s each
  key (when an allowlist is set). `Deny` blocks: `BLOCKED: the user denied...`
  plus "do not retry". When `AllowRemember` is false, session and always count
  as once.
- **Nobody to ask means deny (fix).** With a nil approver, whatever would ask
  is blocked instead, and the message says nobody is available to approve
  and that the run can be started with `--yolo` (for lockdown, that lockdown
  needs approval for everything). The Python fails open here.
- An approver error (e.g. a cancelled context) blocks, with the error in the
  message.
- Every `Message` that blocks starts with `BLOCKED`.

## package paths (`internal/paths`)

```go
type Policy struct{ Home, SoaHome string } // the user's home; the instance home
func (p Policy) Resolve(cwd, path string) (string, error)
type WriteCheck struct {
    Denied, Protected bool
    Reason            string
}
func (p Policy) CheckWrite(abs string) WriteCheck
func (p Policy) CheckRead(abs string) error // nil = allowed
```

- `Resolve`: empty path is an error; `~` and `~/...` expand to `Home`;
  relative paths join `cwd`; the result is cleaned and symlinks are resolved
  on the longest existing prefix (a new file under a symlinked directory
  resolves to the real directory).
- `CheckWrite` **denied** (with a reason): everything under `~/.ssh/`
  (`config` included: the Python gates it behind an approval that crashes,
  **fix**), `~/.aws/`, `~/.gnupg/`, `~/.kube/`, `~/.docker/`, `~/.azure/`,
  `~/.config/gh/`, `~/.config/gcloud/`; `~/.netrc`, `~/.pgpass`, `~/.npmrc`,
  `~/.pypirc`, `~/.git-credentials`; everything under `/etc/`, `/boot/` and
  `/usr/lib/systemd/` (the message can suggest the terminal with sudo); in the
  instance home: `.env`, `config.toml`, `soa-d.db*`, `state.db*`, `sessions/`,
  `pairing/`, `.nix-managed.json`, `memories/.nix-rules.json`.
- `CheckWrite` **protected** (needs approval in every mode): basenames
  `AGENTS.md`, `CLAUDE.md`, `SOUL.md`, `.cursorrules` (any case, any
  directory), and `<instance home>/memories/rules.toml` (rule changes always
  need the user's approval).
- `CheckRead` blocks: basenames `.env`, `.env.local`, `.env.development`,
  `.env.production`, `.env.test`, `.env.staging`, `.envrc` anywhere (not
  `.env.example`); `<instance home>/.env`; everything under `/dev/`;
  `/proc/*/{environ,mem,maps,smaps,auxv,pagemap,cmdline}` and `/proc/*/fd/*`.

## package tools (`internal/tools`)

```go
type Env struct {
    Policy paths.Policy
    Gate   *approval.Gate
    State  *State
}
func NewState(cwd string) (*State, error) // per-session mutable state
func (s *State) Cwd() string
func (s *State) Close() error             // removes the session's private temp dir

type Tool interface {
    Schema() provider.Tool
    // Run returns the result content (a JSON object). An error is for
    // infrastructure failures only; the loop turns it into {"error": ...}.
    Run(ctx context.Context, env *Env, args json.RawMessage) (string, error)
}
func NewRegistry(ts ...Tool) *Registry
func (r *Registry) Schemas() []provider.Tool  // registration order
func (r *Registry) Lookup(name string) (Tool, bool)
func Builtin() []Tool // terminal, read_file, write_file, patch, search_files
```

Results are JSON objects; failures are `{"error": "..."}` (extra fields
allowed). `State` holds the session cwd, a private 0700 temp dir for the
shell snapshot, and per-path read stamps (mtime, size, whether the read was
partial). It is safe for concurrent use.

Schemas: start from the Python wording (`../tools/*` schemas; trimmed in
`85300ef3`), keep them short, and drop what this phase does not do:
`cross_profile` everywhere (the profiles system it guarded is gone),
`terminal`'s `pty` and `watch_patterns` (1 and 3 uses in the owner's
history), and its `background` / `notify_on_complete` until phase 1b.
`patch` has no `mode` parameter (V4A mode: 12 uses, dropped).

### terminal

Params: `command` (required), `timeout` (seconds, default 180; 0 or negative
is an error; over 600 is an error that points at background mode, coming in
phase 1b), `workdir` (absolute; only `A-Za-z0-9/_-.~ +@=,:` allowed, else an
error). Reference: `../tools/terminal_tool.py`, `../tools/environments/`.

- First the gate: `Gate.Command`. Blocked -> `{"error": Message, "status":
  "blocked", "exit_code": -1, "output": ""}`; the command does not run.
- Foreground backgrounding is refused with an error mentioning background
  mode: a trailing `&`, and `nohup` / `setsid` / `disown` at a command
  position.
- Each call runs `bash -c <wrapper>` with stdin `/dev/null`, stdout and stderr
  on one pipe, in its own process group. The wrapper sources the session's
  env snapshot (if any), `builtin cd -- <cwd> || exit 126`, evals the command
  (pass it in an environment variable, not spliced into the script, and unset
  that variable before dumping), saves its exit status, writes `export -p` to
  the snapshot atomically, prints a cwd marker with a per-call random nonce
  (`pwd -P`), and exits with the saved status. So exported variables and the
  cwd persist between calls; plain variables do not. If the command `exit`s
  early and the marker is missing, cwd and snapshot are unchanged.
- cwd = `workdir` when given (a one-off: the session cwd is not updated),
  else the session's. The session cwd is updated from the marker; when it
  changed, the result has `cwd`.
- Timeout: SIGTERM to the process group, SIGKILL after 1s; exit_code 124 and
  `[Command timed out after Ns]` appended. Context cancellation: the same
  kill, exit_code 130, `[Command interrupted]`.
- Output: the marker line removed, ANSI stripped, surrounding whitespace
  trimmed; over 50,000 chars, keep the first 40% and last 60% around
  `\n\n... [OUTPUT TRUNCATED - N chars omitted out of T total] ...\n\n`.
- Result: `{"output", "exit_code"}`, plus `cwd` when it changed.

### read_file

Params: `path` (required), `offset` (1-based, default 1), `limit` (default
2000, max 2000). **(fix)** The Python default is 500 although its schema says
2000; use 2000. Reference: `../tools/file_tools.py`,
`../tools/file_operations.py`.

- Resolve, then `CheckRead`; directories and other non-regular files are
  errors; a NUL byte in the first 8 KiB or invalid UTF-8 is "Cannot read
  binary file ...". Missing files: `{"error": "File not found: ...",
  "similar_files": [...]}` with up to 5 names from the same directory within
  edit distance 2 or sharing a case-insensitive prefix of 4+ characters.
- Lines are `N|text` joined with `\n` (no trailing newline); lines longer than
  2000 chars are cut to 2000 plus `... [truncated]`. The content stops at a
  whole line before exceeding 100,000 chars.
- Result: `{"content", "total_lines", "file_size", "truncated"}`; when the read
  stops before the end of the file (limit or budget), `truncated` is true and
  `next_offset` is the next line number. An empty file is `content ""`,
  `total_lines 0`.
- Record a read stamp (partial when offset > 1 or truncated).
- Not in 1a: images, PDFs, office documents (0 uses in the owner's history),
  unicode-variant retries, repeat-read guards.

### write_file

Params: `path`, `content` (both required).

- Resolve; `CheckWrite`: denied -> error, nothing touched; then
  `Gate.Write(abs, protected)`, blocked -> error, nothing touched.
- Create missing parent directories (0755). Write atomically: a temp file in
  the target's directory, then rename. Keep an existing file's mode; new files
  0644. Re-read and compare SHA-256: `verified` true on match.
- Result: `{"bytes_written", "dirs_created" (whether any directory was
  created), "verified", "resolved_path"}`.
- Staleness never blocks: if this session read the file and its mtime or size
  changed since, or the last read was partial, add a `_warning`. Refresh the
  read stamp after writing.

### patch

Params: `path`, `old_string`, `new_string`, `replace_all` (default false). A
`mode` argument of `"patch"` is an error saying V4A is not supported. Same
resolve / CheckWrite / gate / atomic write as `write_file`. Reference:
`../tools/fuzzy_match.py` (9 strategies; port these 3):

1. exact substring;
2. line-trimmed: compare whole lines with surrounding whitespace trimmed; when
   it matches, re-indent `new_string`: the leading whitespace of the first
   matched file line replaces the leading whitespace of `old_string`'s first
   line, on every line of `new_string` that starts with it;
3. whitespace-normalized: runs of spaces and tabs collapsed to one space on
   both sides, the match mapped back to the original span.

- The first strategy with any match wins. Several matches without
  `replace_all` -> error naming the count and the line numbers (up to 5);
  `replace_all` with a fuzzy strategy matching more than once -> error.
- Errors: empty `old_string`; `old_string == new_string`; no match.
- Already applied: no match, but `new_string` (8+ chars) is present -> success
  with `no_change: true`, nothing written.
- Result: `{"success": true, "diff": <unified diff, a/ b/ headers, 3 lines of
  context>, "resolved_path"}`, plus `_warning` for staleness.

### search_files

Params as the Python schema: `pattern` (required), `target` (`content` |
`files`), `path` (default the session cwd), `file_glob`, `limit` (default 50),
`offset` (default 0), `output_mode` (`content` | `files_only` | `count`),
`context` (default 0). ripgrep is required ("ripgrep (rg) is required" if
missing). Reference: `../tools/file_operations.py:2420-2646`.

- content mode: `rg --json --sort path [-C n] [--glob g] -- pattern path`.
  Each match is `{"path","line","content"}` (content cut to 500 chars);
  context lines are entries with `"context": true` next to their match.
  `limit`/`offset` page over matches (context rides along). `total_count`
  is all matches found; `truncated` is true when matches remain past the page.
- `files_only`: `"files": [...]`; `count`: `"counts": [{"path","count"}]`.
- files target: `rg --files --sortr modified --glob <pattern> path`; a
  pattern with no glob metacharacters becomes `*pattern*`. `"files"` plus
  `total_count` / `truncated`.
- A missing path is an error. Results inside read-blocked paths are dropped.
  The result is always valid JSON (the Python appends a hint after the JSON).

## package provider

`Request` gains `ToolChoice string` (`tool_choice`, omitted when empty).

## package config

`func (c *Config) PermissionMode() approval.Mode`: `[security].lockdown =
true` -> lockdown; else `[approvals].mode`: `"manual"` -> ask, `"off"` ->
yolo, `"smart"`, absent or anything else -> default.

## package store

`func (s *Store) Allowlist() *Allowlist` with `Has(key) bool` and
`Add(key) error` (idempotent), backed by a table added in a new migration.

## package agent

`Runner` gains:

```go
Tools              *tools.Registry   // nil: tool-less, as in phase 0
MaxSteps           int               // tool steps per turn; 0 means 500
MaxToolResultChars int               // 0 means 100,000
Policy             paths.Policy
Mode               approval.Mode
Approver           approval.Approver // nil: nobody to ask
Allowlist          approval.Allowlist
```

`Event` gains `Tool` and `CallID`; new kinds `tool.start` (Text = the
arguments) and `tool.end` (Text = the result content). `Session` gains
`Close() error`. Each session owns a `tools.State` (cwd = the session's cwd)
and an `approval.Gate`, created by `NewSession` / `Resume`. An empty `Mode`
means `Default`, and `NewState` does not require the cwd to exist (phase 0's
tests create sessions with neither).

The turn:

1. Every request lists `Tools.Schemas()` (the same bytes on every request) and
   is built from the stored transcript as in phase 0. Stored assistant tool
   calls and tool rows (`tool_call_id`, `name`) replay as stored.
2. If the answer has tool calls, run them in order (sequentially in 1a). Per
   call: emit `tool.start`; invalid JSON arguments -> `{"error":"invalid
   arguments: ..."}` without running; unknown tool -> `{"error":"unknown tool:
   <name>"}`; a `Run` error -> `{"error": ...}`. Truncate the result to
   `MaxToolResultChars` (first 40% and last 60% around a notice containing
   `TRUNCATED`). Emit `tool.end`.
3. Persist each completed step in one `AppendMessages`: the first step
   includes the user message (with `APIContent` = the bytes sent); a step is
   the assistant message plus one tool row per call. Then loop.
4. No tool calls: persist the assistant message (with the user message if it
   is the first step) and return.
5. A model call that fails on the first step stores nothing (phase 0). On a
   later step, the completed steps stay stored and the error is returned.
6. Cancellation during a step: the running call keeps the result its tool
   returned; every call not yet run gets `{"error":"cancelled: the user
   interrupted before this call ran"}`; the step is persisted; return the
   context error. Every stored tool call always has exactly one result.
7. After `MaxSteps` tool steps, one more request with `ToolChoice: "none"`
   (tools still listed, so the prefix stays cached); its answer is final, and
   any tool calls in it are dropped. **(fix)** Never a synthetic user message:
   the Python injects one, and its "grace call" is dead code.

## cmd/soa-d

`query` gains the tools and `--yolo`:

- `Tools: tools.NewRegistry(tools.Builtin()...)`, `Mode:
  cfg.PermissionMode()` or `Yolo` with `--yolo`, `Approver: nil` (nobody to
  ask: SPEC section 10), `Allowlist: store.Allowlist()`,
  `Policy{Home: os.UserHomeDir(), SoaHome: home}`, cwd `os.Getwd()`.
- stdout carries only assistant text; tool activity does not go to stdout.

## Nix

The `soa-d` package wraps its binary so `bash` and `ripgrep` (and coreutils)
are on PATH at run time, and puts them in `nativeCheckInputs` so the check
phase can run the terminal and search tests. Make sure the dev shell has
`rg`.

## Done when

All gates in `AGENTS.md` pass, phase 0 included. One commit: summary
`daemon: phase 1a (tool loop, five tools, path policy, approval gate)`, with a
body listing the packages and any contract-test typo you fixed. Aim for under
about 4,000 new lines of non-test Go.
