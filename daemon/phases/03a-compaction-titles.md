# Phase 3a: compaction, context length, titles

Goal: long sessions keep working (compaction), the daemon knows each model's
context window, and interactive sessions get titles. Phase 3b adds the memory
tool, facts, recall and `session_search`.

Read first: `SPEC.md` sections 4, 6 and 7, `AGENTS.md`, `phases/02a-server.md`
and `02b-tui-client.md`, and the code. The Python behavior was surveyed; the
references below are into `../agent/context_compactor.py` (CC),
`../agent/title_generator.py` (TG) and `../agent/model_metadata.py` (MM).
Where this file and the Python differ, this file wins.

## Already written (do not modify)

- Go: `internal/agent/compact_test.go`, `internal/store/compaction_store_test.go`,
  `internal/title/title_test.go`, `internal/provider/models_test.go`,
  `internal/config/compaction_config_test.go`,
  `internal/server/compact_server_test.go`; `fakellm.Server.SetModelMeta`.
- Python: the two phase 3a tests at the end of `tests/test_tui_daemon.py`.

## The design in one paragraph

Rows are never rewritten or copied. A compaction stores a summary of the
rows up to some seq on the session (`compactions` table) and rebuilds the
system prompt; from then on a request is the system prompt, the summary as a
user message, then the rows after that seq. Python archives every row and
re-inserts copies, and most of its compaction bug list comes from that
(duplicated rows, lost tails, broken pairing); this design has none of those
moving parts, keeps the full history for clients, and replays byte for byte.

## package provider

- `Request` gains `MaxTokens int`, `Temperature *float64`, `ResponseFormat
  json.RawMessage`, each omitted from the body when unset.
- `type ModelInfo struct { ID string; ContextLength int; ReasoningEfforts []string }`
  and `(*Client) ModelInfo(ctx) ([]ModelInfo, error)` from `GET <base>/models`.
  The context length is the first positive integer among `context_length`,
  `context_window`, `context_size`, `max_context_length`,
  `max_position_embeddings`, `max_model_len`, `max_input_tokens`,
  `max_sequence_length`, `max_seq_len`, `n_ctx_train`, `n_ctx`, `ctx_size`
  (MM:259-271); `reasoning_efforts` as listed. `Models` stays (the ids).
- `func ContextOverflow(err error) (limit int, ok bool)`: an `*HTTPError` with
  status 413, or 400 whose body names a context-length problem ("context
  length", "maximum context", "context size", "context window", "prompt is too
  long", "max_model_len", "maximum model length", "exceed_context_size",
  "too many tokens", "context_length_exceeded"). limit is the stated limit when there is one: the number
  after "maximum context length is", "max_model_len (is)", "maximum model
  length (is)", "context length/size/window (is|of|:)", the number after `>`
  before "max"/"maximum", or a JSON `n_ctx` field, or "the context is N" (gufo); otherwise 0.

## package config

- `[compaction] enabled` (default true) and `threshold` (fraction of the
  window, default 0.75): `CompactionEnabled() bool`, `CompactionThreshold()
  float64`.
- `custom_providers.<p>.models.<m>.context_length`: `RouteContextLength(model)
  int` (0 when unset).
- `[auxiliary.compaction]` and `[auxiliary.title_generation]` (`base_url`,
  `key_env`, `model`; `provider` is accepted and ignored):
  `Auxiliary(task string, fallback Endpoint) Endpoint`. Each unset field comes
  from `fallback` (the session's endpoint); the key is the `key_env` variable
  from the process environment, then `.env`, as `Endpoint` does.

## Context length

A session's window, in order: the model's route `context_length`; its
`/models` entry (fetched once per daemon per base URL, refreshed by
`model.list`); a limit learned from an overflow error (kept for the session's
life in the daemon); else 256000 (MM:222). The agent gets it through
`Runner.Compaction.ContextLength` and `Session.SetContextLength`.

## package agent

```go
type Compaction struct {
    Enabled       bool             // automatic compaction; manual always works
    Threshold     float64          // of the context window; 0 means 0.75
    ContextLength int              // the model's window; 0 means 256000
    Client        *provider.Client // the summary model; nil means the session's
    Model         string           // "" means the session's model
}
// Runner.Compaction Compaction

type CompactResult struct { ThroughSeq int64; Summary string; BeforeTokens int }
type CompactPlan struct { Rows int; ThroughSeq int64; Tokens int }
var ErrNothingToCompact error

func (s *Session) Compact(ctx context.Context, focus string) (CompactResult, error)
func (s *Session) PlanCompaction() (CompactPlan, error)
func (s *Session) SetContextLength(n int)
func (s *Session) ContextLength() int
```

`Event` gains `Compaction *CompactResult` (`Kind: "compaction"`) and a
`Kind: "warning"` with `Text`. The `PromptBuilder` is called at `NewSession`
and again at each compaction (never at `Resume`).

### When

- After every model response, the agent keeps that response's
  `prompt_tokens` (never an estimate; CC:2889). If it is at least `Threshold ×
  ContextLength` and automatic compaction is on, it compacts before the next
  request: the next step of this turn, or the first request of the next turn.
  A successful automatic compaction may be followed by another in the same
  turn when the threshold is crossed again; a failed one does not retry on
  every step.
- The first response after an automatic compaction that still reports at
  least the threshold turns automatic compaction off for that session, with a
  warning saying so and suggesting `/compact` with a focus or a new session.
- A failed summary request leaves the transcript as it is, emits a warning,
  and the turn goes on; automatic compaction then waits 60 seconds (CC:4608).
- A request that fails with `provider.ContextOverflow`: learn the limit if one
  is stated, compact (ignoring the cooldown and the off switch), and retry the
  request once. `ContextOverflow` recognizes HTTP 400 bodies with
  `context_length_exceeded` and reads the limit from `"the context is N"` (gufo
  answers this way). Recovery may run again after a request succeeds; a request
  that keeps failing ends the turn. Nothing to compact, or a persistent
  overflow: the turn fails.

### What

Rows after the last compaction's seq, oldest first, are the candidates.

- **The tail** (kept verbatim). Automatic: the longest suffix whose estimated
  size (characters / 4, content plus tool-call JSON) fits in `0.2 × Threshold
  × ContextLength` tokens. Manual (`Compact`, `PlanCompaction`): only the floor.
  The floor in both: at least the last 2 rows. Then, if the tail would start on
  a tool row, it starts at the assistant row that made those calls instead: a
  tool group is never split.
- **The head**: the candidates before the tail. Empty means
  `ErrNothingToCompact`.
- **The summary request** (one user message; no tools, no `max_tokens`;
  CC:4367): port the Python prompt verbatim: the preamble (CC:4226-4239)
  with its language rule (CC:4167-4169), the first-compaction form with "TURNS
  TO SUMMARIZE:" and the Markdown template (CC:4266-4301, 4337-4346), the
  iterative form with "PREVIOUS SUMMARY:" and "NEW TURNS TO INCORPORATE:" when
  the session already has a summary (CC:4314-4334), and the focus suffix
  (CC:4350-4354) for a manual focus. The head is serialized as `[USER]:`,
  `[ASSISTANT]:` (with `[Tool calls:` … `]`) and `[TOOL RESULT <id>]:` blocks,
  with the Python's cuts: tool results over 2000 characters keep the first 1500
  and last 400 around "...[truncated]..."; user and assistant text over 6000
  keep the first 4000 and last 1500; tool arguments over 1500 keep 1200 plus
  "..."; the whole block is capped at 160,000 characters, 45% head and the
  rest tail, around an omitted-middle marker (CC:3623-3714, 4040-4070). The
  summary budget in the template: `max(2000, min(0.2 × head tokens, min(0.05 ×
  ContextLength, 10000)))` (CC:3592-3601).
- **The summary**: the reply with `<think>…</think>` blocks removed and
  trimmed; an empty one is a failure.
- **Stored**: `store.AddCompaction` with the head's last seq, the summary,
  `BeforeTokens` (the `prompt_tokens` that triggered it, or the last known, or
  the estimate for a manual one), and the rebuilt prompt's hash.

### The request after a compaction

System prompt, then the summary block `SummaryPrefix + "\n" + summary +
"\n\n" + SummaryEnd`, with `SummaryPrefix` and `SummaryEnd` the Python's text
verbatim (CC:115-149, 270-273). If the first kept row is a user row, the block
is joined to it as `block + "\n\n" + its content` in one user message (Gemma
and llama.cpp templates reject two user messages in a row); otherwise the
block is its own user message before the kept rows. The same state always
gives the same bytes, so the prompt cache holds from the next request on.

## package store

Schema v5: a `compactions` table (session, through_seq, summary,
before_tokens, created) and `sessions.title_source`.

```go
type Compaction struct { ThroughSeq int64; Summary string; BeforeTokens int; Created time.Time }
func (s *Store) AddCompaction(ctx context.Context, sessionID string, c Compaction, promptHash string) error
func (s *Store) Compactions(ctx context.Context, sessionID string) ([]Compaction, error) // oldest first
func (s *Store) SetTitle(ctx context.Context, id, title, source string) (changed bool, err error)
// Session gains TitleSource
```

`AddCompaction` (one transaction) refuses a `ThroughSeq` not beyond the last
compaction's or past the last row, and sets the session's `prompt_hash`.
`SetTitle`: sources rank `derived < llm < user`; a title from a lower source
than the current one is ignored (`changed` false); an unknown source is an
error. `Messages` still returns every row.

## package title (new)

Port of TG.

```go
func Derive(text string) string                                          // TG:203-227
func Generate(ctx context.Context, c *provider.Client, model, text string) (string, error)
```

- `Derive`: the first non-empty line, cut at 48 characters on a word boundary
  with "…".
- `Generate`: the system prompt verbatim (TG:62-81), the opening message
  (capped at 1000 characters) as the user message, `max_tokens` 64,
  `temperature` 0.3, `response_format` the Python's strict `json_schema`
  (TG:87-99, 367-377). The reply is parsed as JSON, fenced JSON, the `"title"`
  value by pattern, then the first prose line after removing think blocks
  (TG:230-280), cleaned (TG:283-295: collapse whitespace, strip quotes, then a
  `Title:` prefix, then trailing `.!,;:`; over 80 characters becomes the first
  77 plus "...") and rejected (an error) when empty, over 12 words, containing
  `{` or `}`, or a run of 4 or more identical words (TG:298-318, 380-395).

## package server

- The session object gains `context_length`, `compactions` (count) and
  `title` (empty, a derived one, the model's, or the user's). The attach
  result gains `compactions` (`[{through_seq, summary, created}]`, oldest
  first). `session.list` items carry `title`.
- Titles, interactive sessions only: when a turn starts on a session with no
  title, `SetTitle(Derive(text), "derived")` and `session.updated`, then in the
  background `title.Generate` with `Auxiliary("title_generation", …)`; on
  success `SetTitle(…, "llm")` and `session.updated`; on failure the derived
  title stays. Never again once a session has an `llm` or `user` title.
  `session.set` accepts `title` (source `user`).
- `command.run compact`: `args` `--preview` (or `--dry-run`) returns
  `{"preview": {"rows", "through_seq", "tokens"}}` and changes nothing; other
  args are the focus; the result is `{"through_seq", "summary"}`. While a turn
  runs: error -32013 (busy, a new code). Nothing to compact: -32602.
- Events: `compaction` (`summary`, `through_seq`, `before_tokens`) for manual
  and automatic compactions (an automatic one arrives before the request it
  precedes); the agent's warnings become `notice` events with level `warning`.
- Sessions run with `Runner.Compaction` from config (`CompactionEnabled`,
  `CompactionThreshold`, the context length above, and a client and model from
  `Auxiliary("compaction", <the session's endpoint>)`), refreshed on
  `session.set model`.

## The TUI (Python)

- `compaction` events render as today's compaction block (the summary, plus
  the stats line with `before_tokens`); in history, the block is drawn after
  the row whose seq is the compaction's `through_seq`.
- `/compact [focus | --preview]` runs `command.run compact`; `--preview`
  prints the plan as a note.
- `status_snapshot()`: `session_title` is the title, else the preview;
  `context_length` from the session; `context_percent` is
  `round(100 × context_tokens / context_length)`; `compactions` the count.
- Remove "/compact" from the commands that say "not available".

## README

In the Status section, drop compaction and titles from "Not in the daemon
yet"; mention `/compact` in the commands table.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 3a
(compaction, context length, titles)`, body listing what changed and any
contract-test typo fixed. Aim for under about 2,000 new lines of non-test Go.
