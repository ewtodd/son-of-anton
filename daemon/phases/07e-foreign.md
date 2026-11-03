# Phase 7e: importing Claude Code, Codex and opencode sessions

Goal: `soa-d import` reads other tools' sessions straight from their own
stores, read-only (SPEC section 14), so the Python's `sessions import` is no
longer needed once the Python backend goes.

Read first: `SPEC.md` section 14 (note: opencode keeps a SQLite database,
not JSONL), `phases/08a-import.md`, and the code. The reference is
`son_of_anton_cli/foreign_sessions.py` (FS). Where this file and the Python
differ, this file wins.

## Already written (do not modify)

`internal/foreign/foreign_test.go` with `internal/foreign/testdata/`
(fixtures, and `expected.json`: the Python parsers' output on them),
`cmd/soa-d/foreign_test.go`.

## package foreign (new)

```go
type Turn struct { Role, Content string; Timestamp *float64 } // epoch seconds
type Usage struct { Input, Output, CacheRead, CacheWrite, Reasoning, Calls, LastPrompt int }
type Session struct { Cwd, Title, ID string; Turns []Turn; Usage Usage } // Title: FS's title_guess
func ListClaude(root string) ([]string, error)   // root/*/*.jsonl (one level), sorted
func ParseClaude(path string) (Session, error)
func ListCodex(root string) ([]string, error)    // root/**/rollout-*.jsonl, sorted
func ParseCodex(path string) (Session, error)
func ListOpencode(db string) ([]string, error)   // root sessions (no parent), newest first
func ParseOpencode(db, id string) (Session, error)
```

They are FS's parsers exactly (`parse_claude_session` with `_claude_live_chain`,
`parse_codex_session`, `parse_opencode_session` with the revert bounds,
`_flatten_blocks`, `_is_wrapper_text`, `_coerce_timestamp`, `_merge_turns`,
`_first_user_line`, the usage), checked against `expected.json`. The opencode
database is opened read-only (`mode=ro`, `query_only`).

## The command

`soa-d import --from-claude [ROOT]`, `--from-codex [ROOT]`,
`--from-opencode [DB]` (defaults `~/.claude/projects`, `~/.codex/sessions`,
`$XDG_DATA_HOME/opencode/opencode.db` with `~/.local/share`), with
`--home DIR` and `--force` (import again what was imported before). Every
session with turns is imported; the line printed is 8a's: "imported <n>
sessions (<m> messages); <k> already present, <h> skipped" (skipped: no
turns).

- A session: kind interactive, source `claude-code`, `codex-cli` or
  `opencode`, a new id, `cwd` (and workspace), created at its first turn's
  time (else now), title "Imported from <Claude Code|Codex CLI|opencode>:
  <seed>" (FS:900-906: the title guess's first line, else the first user
  line, else the file's stem; over 60 characters, its first 59 and "…",
  title source "user"), its usage (`Input`, `Output`, `CacheRead` as cached,
  `Reasoning`, `Calls`; `LastPrompt` as the context), and a prompt built
  for it as for a new interactive session in that directory (so it resumes
  like one). Rows: the turns, user and assistant, with their times.
- Already imported: schema v11, `foreign_imports (source, foreign_id,
  session_id, PRIMARY KEY (source, foreign_id))`; the foreign id is the
  file's session id (Claude `sessionId`, Codex `id`, else the path) or the
  opencode `ses_…` id. 8a's import of the Python's sessions records the same
  for a Python session whose `origin_json` has `imported_from` (its `tool`
  as the source, its `foreign_session_id`), so a session the Python
  imported is not imported twice.

## README

Under "Sessions and the command line": the three flags, after 8a's line.
Status: drop "importing Claude Code, Codex and opencode transcripts" from
"Still Python".

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 7e
(foreign session import)`, body listing what changed, ending with the
`Model:` line.
