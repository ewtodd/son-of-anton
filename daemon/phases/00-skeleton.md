# Phase 0: skeleton

Goal: the smallest daemon that can hold a conversation. Config loading, a
SQLite store, an OpenAI-compatible streaming client, a tool-less agent loop
that keeps the prompt-cache invariants, and `soa-d query "<prompt>"`.

Read first: `SPEC.md` sections 2, 3, 4, 6, 7 and 16, and `AGENTS.md`.

## What already exists (do not modify)

- `internal/fakellm/`: the scripted fake model server every test uses.
- Contract tests: `internal/config/config_test.go`,
  `internal/provider/provider_test.go`, `internal/store/store_test.go`,
  `internal/agent/agent_test.go`, `cmd/soa-d/main_test.go`.

Your job is the code that makes every one of them pass, with exactly the API
below. Read each test file before writing its package.

## Dependencies

- `github.com/BurntSushi/toml` (TOML)
- `modernc.org/sqlite` (pure-Go SQLite with FTS5)

Use their latest releases; commit `go.mod` and `go.sum`. Nothing else outside
the standard library.

## package config (`internal/config`)

```go
func Home() (string, error)                 // $SON_OF_ANTON_HOME, else $HOME/.son-of-anton
func Load(home string) (*Config, error)     // <home>/config.toml (missing = empty) + <home>/.env
func (c *Config) Endpoint(model string) (Endpoint, error) // "" = the default model
func ClampEffort(requested string, supported []string) string

type Endpoint struct{ BaseURL, APIKey, Model, ReasoningEffort string }
```

`Config` holds at least `[model]` (`default`, `provider`, `reasoning_effort`),
`[agent]` (`reasoning_effort`, `reasoning_overrides` map), and
`[custom_providers.<name>]` (`base_url`, `key_env`, `models.<id>.
reasoning_effort` / `reasoning_efforts`). Unknown keys and sections are
ignored, never errors. Invalid TOML is an error.

- `.env`: `KEY=VALUE` lines; `#` comments and blank lines skipped; optional
  `export ` prefix; one pair of surrounding single or double quotes stripped.
  The API key is the process environment variable named by `key_env` when it
  is set and non-empty, otherwise the `.env` value. Never write to the process
  environment.
- `Endpoint` errors, naming the provider, when `[model].provider` has no
  `[custom_providers.<name>]` entry (or none is configured).
- Requested effort, first match wins: `agent.reasoning_overrides[model]`;
  `custom_providers.<p>.models.<model>.reasoning_effort`;
  `[model].reasoning_effort`, only when `model` is the default model;
  `agent.reasoning_effort`. A TOML `false`, or `"off"`, `"no"`, `"false"`,
  means `"none"`. The result is clamped to the route's `reasoning_efforts`.
- `ClampEffort` ports `clamp_effort` from `../agent/reasoning_effort.py`:
  ladder `none minimal low medium high xhigh max ultra`. Empty request, an
  unknown vocabulary, a supported level, or a level not on the ladder passes
  through. Otherwise the nearest weaker supported level; if none is weaker,
  the weakest supported level. `none` is never a degradation target.

## package provider (`internal/provider`)

```go
type Message struct {
    Role       string     `json:"role"`
    Content    string     `json:"content"`
    ToolCalls  []ToolCall `json:"tool_calls,omitempty"`
    ToolCallID string     `json:"tool_call_id,omitempty"`
    Name       string     `json:"name,omitempty"`
}
type ToolCall struct {
    ID       string       `json:"id"`
    Type     string       `json:"type"`
    Function FunctionCall `json:"function"`
}
type FunctionCall struct {
    Name      string `json:"name"`
    Arguments string `json:"arguments"`
}
type Tool struct {
    Type     string       `json:"type"`
    Function ToolFunction `json:"function"`
}
type ToolFunction struct {
    Name        string          `json:"name"`
    Description string          `json:"description,omitempty"`
    Parameters  json.RawMessage `json:"parameters,omitempty"`
}
type Request struct {
    Model           string
    Messages        []Message
    Tools           []Tool
    ReasoningEffort string
}
type Delta struct{ Content, Reasoning string }
type Usage struct{ PromptTokens, CompletionTokens, ReasoningTokens, CachedTokens int }
type Response struct {
    Message      Message // Role "assistant", Content, ToolCalls
    Reasoning    string
    FinishReason string
    Usage        Usage
}
type RetryPolicy struct {
    MaxAttempts         int
    BaseDelay, MaxDelay time.Duration
}
type HTTPError struct {
    Status int
    Body   string
}

func (e *HTTPError) Error() string
func New(baseURL, apiKey string) *Client // Retry defaults: 4 attempts, 500ms base, 8s max
func (c *Client) Complete(ctx context.Context, req Request, onDelta func(Delta)) (*Response, error)
```

`Client` has an exported `Retry RetryPolicy` field (tests set it).

- POST `<baseURL>/chat/completions`, `Authorization: Bearer <key>`,
  `Content-Type: application/json`. The body is `model`, `messages`, `tools`
  (omitted when empty), `reasoning_effort` (omitted when empty),
  `"stream": true`, `"stream_options": {"include_usage": true}`. Serialize it
  from structs, never maps, so the same request is always the same bytes.
- Parse the SSE stream: `data: ` lines, `[DONE]` ends it. Deltas carry
  `content`, `reasoning_content` (also accept `reasoning`), and `tool_calls`
  fragments keyed by `index` (id, name, then argument pieces to concatenate).
  The usage chunk has `choices: []` and `usage` with
  `completion_tokens_details.reasoning_tokens` and
  `prompt_tokens_details.cached_tokens`.
- A stream that ends without a finish_reason and `[DONE]` is an error.
- Retry 429, 500, 502, 503, 504 and connection errors, and only before any
  response byte reached `onDelta`. Exponential backoff from BaseDelay capped at
  MaxDelay, honoring `Retry-After` in seconds (also capped). Any other status
  fails at once with `*HTTPError`. After the last attempt, return the last
  error. Cancelling `ctx` returns promptly with an error that `errors.Is`
  `context.Canceled`.

## package store (`internal/store`)

```go
var ErrNotFound = errors.New(...)

func Open(path string) (*Store, error) // creates the file, WAL, migrates to the latest schema
func (s *Store) Close() error
func (s *Store) SchemaVersion() (int, error)

type Session struct {
    ID, Kind, ParentID, Source, Title, Cwd, Model, PromptHash string
    Created, Ended                                            time.Time
}
func (s *Store) CreateSession(ctx, Session) (Session, error) // generates ID and Created
func (s *Store) Session(ctx, id string) (Session, error)     // ErrNotFound

func (s *Store) PutPrompt(ctx, text string) (hash string, err error) // sha256 hex, stored once
func (s *Store) Prompt(ctx, hash string) (string, error)

type Message struct {
    Seq                                                      int64
    Role, Content, APIContent, Reasoning, ToolCallID, ToolName string
    ToolCalls                                                json.RawMessage
    TS                                                       time.Time
}
func (s *Store) AppendMessages(ctx, sessionID string, msgs ...Message) ([]int64, error) // one transaction
func (s *Store) Messages(ctx, sessionID string) ([]Message, error)                       // seq order

type Hit struct {
    SessionID string
    Seq       int64
    Snippet   string
}
func (s *Store) Search(ctx, query string, limit int) ([]Hit, error) // FTS5 over message content
```

- Session IDs are `YYYYmmdd_HHMMSS_` plus 6 random hex digits (local time),
  unique. `Seq` starts at 1 per session. Appending to an unknown session
  fails and writes nothing.
- Schema versioning: a `schema_version` table and ordered migrations, so
  `Open` on an existing file is a no-op at the latest version.
- Kinds used later: `interactive`, `chat`, `worker`, `child`, `cron`,
  `oneshot`.

## package agent (`internal/agent`)

```go
type PromptBuilder func(ctx context.Context, s store.Session) (string, error)

type Runner struct {
    Store           *store.Store
    Client          *provider.Client
    Model           string
    ReasoningEffort string
    Prompt          PromptBuilder
}
func (r *Runner) NewSession(ctx context.Context, kind, cwd string) (*Session, error)
func (r *Runner) Resume(ctx context.Context, id string) (*Session, error)

type Event struct{ Kind, Text string } // "assistant.delta", "reasoning.delta"
type Result struct {
    Content, Reasoning string
    Usage              provider.Usage
}
func (s *Session) ID() string
func (s *Session) Send(ctx context.Context, text string, onEvent func(Event)) (Result, error)
```

- `NewSession` calls `Prompt` exactly once, stores the text with `PutPrompt`
  and creates the session with that `PromptHash`, the runner's `Model`, and
  the given kind and cwd. `Resume` loads the stored prompt by hash and never
  calls `Prompt`.
- Every request is built from the stored transcript: the system message with
  the stored prompt, then each stored message in order. A user message is
  sent as its `APIContent` when non-empty, else its `Content`. Reasoning is
  stored but never sent. Model and effort come from the runner.
- `Send` persists nothing until the model has answered: on success it
  appends the user message (with `APIContent` set to exactly the bytes sent)
  and the assistant message (content and reasoning) in one `AppendMessages`
  call. A failed or cancelled turn stores nothing, so the transcript always
  alternates.
- `onEvent` (may be nil) receives the deltas as they stream.

## cmd/soa-d

```go
func run(args []string, stdout, stderr io.Writer) int
```

`main` calls `os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))`.

- `soa-d query [--home DIR] [--model M] PROMPT`: home from `--home`, else
  `config.Home()`. Open `<home>/soa-d.db`, resolve `Endpoint`, create a
  session of kind `oneshot`, source `query`, cwd = the working directory, run
  one turn, stream the answer to stdout, then print a newline. The system
  prompt is `<home>/SOUL.md` (trimmed) when present, else one short default
  sentence identifying Son of Anton. Exit 0 on success; on failure print the
  error (including the HTTP status) to stderr, nothing to stdout, exit 1.
- `soa-d version` prints a version string. A missing prompt or unknown
  command prints usage to stderr and exits 2.
- Never create or open `<home>/state.db`.

## Nix

- In `flake.nix`, add `packages.<system>.soa-d`: `buildGoModule` over
  `./daemon` only (filter the source so Python changes do not rebuild it),
  `env.CGO_ENABLED = "0"`, `subPackages = [ "cmd/soa-d" ]`, the real
  `vendorHash` (build once with `lib.fakeHash`, copy the hash Nix reports),
  and the tests run in its check phase.
- Add `go` (and `gopls`) to the existing dev shell.
- Add the package to the flake's checks so `nix flake check` builds and tests
  it. Follow how the flake already wires its Python package and checks.

## Done when

All gates in `AGENTS.md` pass. One commit on this branch: summary
`daemon: phase 0 skeleton (config, store, provider, agent loop, query)`, and a
body listing what each package does and any contract-test typo you fixed.
Keep it small: under about 2,500 lines of non-test Go.
