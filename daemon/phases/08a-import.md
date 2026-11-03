# Phase 8a: importing the Python sessions

Goal: `soa-d import --from <python home>` copies the Python fork's sessions
from its `state.db` into `soa-d.db`, so the TUI can resume them and
`session_search` finds them (SPEC section 6, "Importer"). The source is
never written.

Read first: `SPEC.md` sections 3, 4 (invariant 6) and 6, and the code
(`internal/store`, `cmd/soa-d/main.go`). The Python schema is
`son_of_anton_state.py` (`sessions`, `messages`).

## Already written (do not modify)

`cmd/soa-d/import_test.go`.

## Decisions made without the owner (2026-11-03, night)

- Only a session's active rows (`active = 1`) are imported: they are what
  the model saw last, and a Python compaction leaves its summary as the
  first active user row ("[CONTEXT COMPACTION — REFERENCE ONLY]…"), so the
  imported transcript resumes as the Python's did. Compacted rows are left
  behind.
- Sessions from the sources the Python hid (`subagent`, `tool`, `kanban`)
  are not imported, and no imported session gets a `parent_id` (in soa-d
  that marks a subagent's run).
- An imported Signal session becomes its chat's current session when it is
  that chat's newest, so a chat continues where the Python left it once
  soa-d serves Signal.
- A session already in `soa-d.db` (same id) is skipped whole; rows the
  Python added to it later are not merged.

## The command

`soa-d import --from DIR [--home DIR]`. `--from` is a Python home (its
`state.db`, opened read-only: `file:<path>?mode=ro`); `--home` is the
target instance home (the usual default). It may run while the daemon
serves the same home. It prints one line: "imported <n> sessions (<m>
messages); <k> already present, <h> skipped". Exit 0, or 1 with the error on
stderr.

## Mapping

| Python `source` | soa-d kind | scope, chat |
|---|---|---|
| `cli` | interactive | common |
| `oneshot` | oneshot | common |
| `cron` | cron | gateway when its `session_key` or origin says Signal, else common |
| `signal` | chat | gateway; the chat stamp below |
| `claude-code`, `opencode`, `codex-cli`, anything else | interactive | common |
| `subagent`, `tool`, `kanban` | not imported | |

- Kept: the id (so a second run skips it), `source` as is, `title`
  (title source "user"), `cwd` (and its workspace), `model`, `started_at`
  as created, `ended_at` as ended, the `system_prompt` as the session's
  stored prompt (so a resumed session sends the same prompt), and usage:
  `input_tokens`, `output_tokens`, `cache_read_tokens`, `reasoning_tokens`,
  `api_call_count`, and `last_prompt_tokens` as the context. `store.CreateSession`
  keeps a given id.
- A Signal session's stamp comes from its `session_key`:
  `agent:main:signal:group:group:<id>[:<user>]` gives
  `signal:group:<id>[:<user>]`, `agent:main:signal:dm:<x>` gives
  `signal:dm:<x>`; without one, from `chat_type`/`chat_id` the same way.
- Messages, in `id` order, `active = 1` only, roles user, assistant and
  tool (others skipped): `content` (NULL as ""), `api_content`, `reasoning`
  (else `reasoning_content`), `tool_calls` (re-encoded as soa-d's
  `[{"id", "type": "function", "function": {"name", "arguments"}}]`, other
  keys dropped, arguments kept as their string), `tool_call_id`,
  `tool_name`, and `timestamp` as the row's time. Seqs from 1.
- Each session and its rows are written in one transaction.

## README

Under "Sessions and the command line": `soa-d import --from ~/.son-of-anton`
brings the Python's sessions into the TUI (safe to run again).

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 8a
(import the Python sessions)`, body listing what changed, ending with the
`Model:` line.
