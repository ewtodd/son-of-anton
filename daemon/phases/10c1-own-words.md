# Phase 10c1: our own words

Goal: soa-d still carries text written by others: hermes-agent v0.20.5 (Nous
Research, MIT; it is this repository's first commit, `677b5c39`) and opencode
v1.19.5 (SST, MIT). The owner wants the project wholly its own, so that it can
say no upstream code remains and take its own license. This phase replaces
the words: prompts, tool descriptions, messages and the comments that cite
upstream sources. Phase 10c2 does the tables and code, 10c3 the Nix; they run
at the same time, so stay inside the files listed here.

## How to write it

- **Write from behaviour, not from the old text.** For each string: read the
  code that uses it and the tests that pin it, and note what the text must make
  the model (or the person reading it) know or do. Then write new text that
  does that, in your own structure. Do not rework the old sentences: a
  paraphrase of someone's text is still their text. The reviewer compares
  every replaced string with hermes-agent and opencode and sends close
  paraphrase back.
- Plain, direct English; short; no em-dashes. A schema description never
  names a tool from another toolset (AGENTS.md pitfall).
- **Keep what code depends on, exactly.** The `[SILENT]` reply marker cron
  reads (`internal/cron/due.go`); schema shapes (tool and parameter names,
  types, enums, required lists); config keys, environment variable names,
  SQL, protocol and JSON field names, the `User-Agent` and `Accept` header
  values. These are interfaces, not prose.
- A stored session keeps the system prompt it was created with (data). New
  tool descriptions and the new compaction frame reach live conversations at
  the upgrade: one cache miss each, accepted as for any release that changes
  a tool.

## What to replace

1. **Prompts**
   - `prompt.DefaultIdentity` (the identity when the home has no `SOUL.md`).
   - `internal/agent/summary_prompt.go`: the summarizer's instructions and
     the frame around a summary in a compacted request (`SummaryPrefix`,
     `SummaryEnd`). The summary must still carry what a continuing agent
     needs: the task and the user's intent, decisions and why, files and
     state touched, what is done and what is next, and the user's
     corrections. Choose the sections yourself.
   - `internal/server/cron.go` `cronHint`: a cron run is unattended, its final
     reply is delivered, and `[SILENT]` alone suppresses delivery.
   - `internal/title/title.go` `systemPrompt`.
   - `internal/subagent`: `task.txt` (the task tool's description),
     `explore.txt` (the explore subagent's prompt), both subagent types'
     descriptions, `BackgroundDescription` and the started/updated texts, and
     the `<task ...>` result rendering. The behaviour (SPEC section 9) stays.
2. **Tool schemas and what tools say back to the model** (descriptions,
   parameter descriptions, errors, result notes): every tool in
   `internal/tools/` except `relay`, `session_search` and `memory`, whose
   descriptions this project wrote; keep those three as they are. That
   includes `cronjob.go` in full, its prompt-threat patterns too (rewrite
   them from what the tests require), and `vision.go`/`vision_image.go` in
   full. Not `code_stub.go` (10c2). Also `internal/mcp/mcp.go`'s messages.
3. **Messages people read**: `internal/server/chat.go`,
   `internal/cron/{script,schedule,due}.go` (their small regexes too),
   `internal/tui/editor.go` (the editor template), `internal/tui/commands.go`,
   `internal/tui/modal.go`, `internal/tui/cmdtable.go`,
   `internal/approval/approval.go` (messages only; `patterns.go` is 10c2),
   `internal/memory/memory.go`.
4. **Comments** citing the deleted Python or opencode as the source of code
   or text ("verbatim", line references like `(TG:62-81)`, Python names like
   `_TITLE_PROMPT_TEMPLATE`, `son_of_anton_cli/...py`, "opencode's X") say
   what the code does instead. Not in the files 10c2 owns
   (`internal/approval/patterns.go`, `internal/tools/code_stub.go`,
   `internal/tui/{theme,palette,subagent,state}.go`,
   `internal/server/summary.go`), and not where a comment names a format the
   code reads (`internal/foreign/`, the importers).
5. **SPEC.md**: a last section, "Provenance", saying that from phase 10c the
   words, tables, code and packaging are this project's own; the Python files
   and opencode sources the sections name were the behavioural reference for
   the port, kept as history.

## Tests

- A test that pins old wording is updated to the new wording, and the report
  lists each such change. No other test changes.
- New, unless one exists: every tool the registry offers, and the task tool,
  has a non-empty description, and every parameter in its schema has one.
- New: the cron hint names `[SILENT]`, and a reply of exactly `[SILENT]` is
  not delivered (the existing due tests may already cover the second half).

## Done when

The gates in `daemon/AGENTS.md` pass (the Go ones; this phase changes no Nix).
One commit: summary `daemon: phase 10c1 (our own words)`, body listing what
changed, ending with the `Model:` line. Report: every file changed, and every
test whose wording changed.
