# Phase 5b: fiona

Goal: a Signal chat's agent is fiona (SPEC section 12): her own identity
block, the chat's relay-only toolset (the relay itself is 5c), a notes
folder her file tools cannot leave, and a model route that falls back to
son-of-anton's when hers is down.

Read first: `SPEC.md` sections 7 and 12, `phases/05a-signal.md`, and the code
(`internal/server/chat.go`, `internal/prompt`, `internal/paths`,
`internal/provider`, `internal/tools`).

## Already written (do not modify)

`internal/server/fiona_server_test.go`, `internal/paths/confine_test.go`, and
the tool list in `TestAChatMessageGetsAnAnswer` (`chat_server_test.go`).

## Decisions made without the owner (2026-11-02, night)

- The approved identity text gains one sentence after "…what the worker
  reports.": "A worker cannot see your memory, so add anything from it the
  worker needs, marked as context from you." The owner said fiona should hand
  a worker the gateway memory it needs, and kept the one-way memory wall.
- fiona reads images through `vision_analyze` (phase 7c), not as image
  parts in her messages: the tool works the same in the TUI and in a chat,
  and no message has to carry image data across replays.
- `search_files` joins read/write/patch in the notes folder.

## Config

- `daemon.instance` (string): the instance's name for the identity ("" leaves
  it out). Nix sets it in phase 8.
- `relay.worker_hours` (two integers, start and end hour, 0-23, wrapping
  midnight): when workers may run (5c enforces it). Absent, empty, equal
  hours or anything malformed (a warning in the log) mean any time.
- `gateway.model`: fiona's route (5a). Its fallback is `model.default`.

## The identity

Layer 1 of a chat session's prompt (`prompt.Options.Identity`, with
`Memory: true` so the gateway's rules and recall stay), built when the
session is created and stored with it like any prompt:

1. "You are fiona, the front desk of this son-of-anton instance
   (<instance>), talking with the user over Signal. You answer questions,
   look things up, read images, and keep notes and memories yourself." With
   no instance: "…of this son-of-anton instance, talking…".
2. "You never run commands or edit project files. Your file tools reach only
   your notes folder. Real work (code, files, commands, anything in a
   repository or on a machine) goes to a son-of-anton worker through the
   relay tool: pass the user's request on as they gave it, then tell them
   what the worker reports. A worker cannot see your memory, so add anything
   from it the worker needs, marked as context from you."
3. With no worker hours: "Workers here can run at any time. A request sent
   while a worker is busy waits in a queue, and the relay result says when it
   will run (for example \"after the current turn\"). Tell the user exactly
   that. At a terminal, the user can take a worker over with /resume."
   With hours S and E: "Workers here run only between SS:00 and EE:00. A
   request sent outside those hours, or while a worker is busy, waits in a
   queue, and the relay result says when it will run (for example \"queued
   until SS:00\" or \"after the current turn\"). Tell the user exactly that;
   don't promise it sooner. At a terminal, the user can take a worker over
   with /resume, which runs it at once whatever the hours." (Hours two
   digits.)
4. The home's `SOUL.md`, trimmed, when it has text.

Paragraphs are joined by a blank line. `DefaultIdentity` is never used in a
chat (SPEC section 12 quotes the text).

## Tools and the notes folder

A chat session's tools: `todo`, `clarify`, `memory`, `session_search`,
`read_file`, `write_file`, `patch`, `search_files`, `web_search`,
`web_extract` (builtin order). Its directory is `<home>/notes/gateway`
(created when missing), and its file tools are confined to it:

```go
// paths.Policy gains Confine: when set, CheckRead and CheckWrite refuse any
// path outside it, symlinks resolved (a link inside that points out is outside).
Confine string
```

A refused path: "<path> is outside the notes folder (<dir>); the chat's file
tools reach only it." `search_files` treats the folder as its root and
refuses a path outside it the same way. The folder is under `notes/gateway`,
so recall already indexes it in the gateway scope.

## The model fallback

A chat session's requests go to its model (`gateway.model`, else
`model.default`). When a request to `gateway.model` fails after the
provider's retries with a retryable error (HTTP 429 or 5xx, a network error,
silence) before any output, that one request is sent again on
`model.default`; the next request tries `gateway.model` first again. The
session's stored model stays `gateway.model`; `served_model` names what
answered. No fallback when `gateway.model` is empty or equals
`model.default`.

## README

Under "Signal": the chat agent is fiona (her identity, her notes folder,
falling back to the default model when hers is down).

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 5b
(fiona)`, body listing what changed, ending with the `Model:` line.
