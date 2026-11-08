# Phase 13: reports (a session as a PDF)

Goal (owner, 2026-11-08): a worker's session, turned into a nicely rendered
PDF on demand: from the command line (the owner's sessions, and a viewer's
through the view socket of phase 12) and from fiona, who sends it to the chat
as an attachment when asked. The report is the transcript rendered; no model
writes anything.

Read first: `phases/12-view-socket.md`, `cmd/son-of-anton/view.go`,
`internal/server/relay.go` (actions, `relayWorker`, `mayDrive`),
`internal/server/summary.go`, `internal/signal/client.go` (`Send`),
`nix/son-of-anton.nix`.

## internal/report

- `report.Session` is the input: id, kind, title, cwd, model, agent (a
  subagent's type), created, updated, input and output tokens, its messages
  (`report.Message`: the shape `session.history` returns) and its children
  (subagents, as `Session`s). The command fills it from the socket, the daemon
  from the store.
- `report.Build(s, loc) Doc` is pure: the data the template reads.
  - A header: the title (`(untitled)` without one), kind, id, cwd, and facts:
    model, started, last activity (local times in `loc`, `2006-01-02
    15:04`), requests (user messages that are not notes), tool calls, and
    tokens (`N in · M out`, thousands separated) when the session has usage.
  - Entries in order. A user message is a `user` entry (its markdown), except
    one starting `[background receipt]` or `[Background process`, which is a
    `note` (plain text). An assistant message is an `assistant` entry when it
    has text (reasoning is left out), then a `tool` entry per tool call: the
    name and the one-line summary the daemon's tool events use
    (`report.Summary`, moved here from `internal/server/summary.go` so both
    say the same thing), `error` when its result is one
    (`report.IsError`), and its output: a JSON result's `error` text, else
    its `output` (after `exit N` when `exit_code` is not 0); for
    `read_file`, `write_file`, `patch`, `todo`, `memory` and `task` none;
    otherwise the result as text. Output is cut to 12 lines and 1,500
    characters, with the count of lines left out (`more`), ANSI escapes and
    other control characters (but newline and tab) removed.
  - A subagent comes right after the `task` call whose result names it
    (`<task id="ID" ...`): a `subagent` entry with its title (without the
    ` (@type subagent)` suffix), its type, and its own entries. Children no
    call names follow at the end.
  - A message with images says so in its entry (`[N image(s)]`).
- `report.Render(ctx, s, loc) ([]byte, error)` writes the data as JSON and
  the template (embedded) into a temporary directory and runs `typst compile
  --ignore-system-fonts --root DIR`: `$SON_OF_ANTON_TYPST`, else `typst` on
  `PATH`. Missing, the error says that rendering needs typst with the cmarker
  package, which the son-of-anton package carries. A minute at most.
- The template: A4, Libertinus Serif with DejaVu Sans Mono (typst's own
  fonts, so no system fonts are read), a title block and a facts grid, each
  request in a tinted box with an accent rule, replies plain, tool calls one
  monospace line each (`▸`, or a red `✗` for an error) with the output in a
  small grey box, subagents indented under a rule, notes in small italics, a
  footer with the title, the id and `page N of M`. Markdown is rendered by
  `@preview/cmarker:0.1.9` with `raw-typst: false` (model-written text never
  runs as typst) and `set-document-title: false`, headings from level 3.
- `report.FileName(s)` is the attachment's name: the title as a slug (letters
  and digits, runs of anything else as one `-`, at most 60 characters) plus
  `.pdf`, else `<id>.pdf`.

## The commands

```
son-of-anton view INSTANCE|--socket PATH report ID [-o FILE]
son-of-anton sessions report ID [-o FILE]
```

Both fetch the session over a socket (`session.list` for its object,
`session.history` paged to the start, `session.children` and each child's
history) and render it here, as the user who runs the command: nothing new on
the view socket, and `view` still never starts a daemon or reads the home.
`-o FILE` (default `./<ID>.pdf`; `-` is standard output) and `wrote FILE`
on standard output unless it is the PDF. An existing file is replaced. A
session the socket does not show (a chat) is `no such session`, exit 1.

## fiona: `relay report`

A new relay action, `report`: `session` (id or title) or the bound worker,
found as `status` finds it (so on a shared instance only the chat's own
workers). The daemon renders the worker's report from the store and sends it
to the chat as a message `Report: <title>` with the PDF attached; the result
is `{"success": true, "sent": "<file name>", "title": ..., "messages": N}`, or
`success: false` with the error (no worker, rendering failed). The relay
tool's description gains the action.

`signal.Client.SendFile(ctx, to, text, name, mime, data)` sends one
attachment as a data URI (`data:<mime>;filename=<name>;base64,...`, which
signal-cli takes in `attachments`), since signal-cli runs on another host and
cannot read the daemon's files. It goes through the chat's ordered outbox like
any message.

## Nix

The package wraps both commands with `SON_OF_ANTON_TYPST` set to typst with
cmarker 0.1.9 (`typst.withPackages`), so reports render offline. The dev
shell and the Go check phase get it too (and poppler-utils, for the tests'
`pdftotext`).

## Tests (written first)

- `internal/report`: `Build` on a fixed transcript (requests, notes,
  replies, tool lines and their output, cuts, errors, ANSI, images, a nested
  subagent and an unclaimed one, the facts); `FileName`; `Render` makes a PDF
  whose text (`pdftotext`) holds the title, a request, a reply's markdown, a
  tool line and the subagent, and a `<!--raw-typst ...-->` in a reply does
  nothing; a missing typst is the clear error.
- `cmd/son-of-anton`: `view --socket S report ID` (default name, `-o`,
  `-o -`), `sessions report ID`, a chat is not found, usage errors; `view
  ... report` never starts a daemon.
- `internal/server`: fiona's `relay report` sends a PDF of the worker to the
  chat; another chat's worker on a shared instance is refused; no bound
  worker is refused.
- `nix/checks.nix`: the package smoke renders a report offline; the VM test
  makes one as a viewer.
