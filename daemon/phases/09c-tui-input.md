# Phase 9c: the Go TUI's pickers and prompt helpers

Goal: the parts of the Textual TUI that help you type: pickers for
`/model`, `/reasoning`, `/resume` and `/cancel`; completion of
command names, their arguments and paths; history suggestions; the command
palette; and composing in an external editor.

Read first: `phases/09a-tui-core.md`, `phases/09b-tui-look.md`, and the
reference: `son_of_anton_tui/tui.py` (T), `backend.py` (B),
`completion.py` (C). Where this file and the Python differ, this file wins.
Next: 9d the subagent view and the mouse; 9e switching `son-of-anton` over.

## Already written (do not modify)

`internal/tui/input_test.go`.

## Commands

One table drives `/help`, completion and the palette, in this order:

| name | aliases | args | description |
|---|---|---|---|
| `new` | `reset` | | Start a new session |
| `clear` | | | Start a new session and clear the screen |
| `resume` | `sessions` | `[id or title]` | Go back to a session, yours or one fiona started |
| `handoff` | | | Give this session to fiona and leave the TUI |
| `commit` | | `[extra instructions]` | Review uncommitted changes, write a message in the repo's style, and commit |
| `compact` | | `[focus topic \| --preview]` | Summarize older turns to free context (--preview shows what it would do) |
| `stop` | | | Kill all running background processes |
| `sendnow` | | `[text]` | Steer the running turn onto the oldest queued message, or onto the text after it |
| `sendall` | | | Steer the running turn onto every queued message at once |
| `cancel` | | | Pick queued messages to drop before they are sent |
| `status` | | | Show the session, model, mode and usage |
| `model` | | `[model]` | Switch this session's model |
| `reasoning` | | `[level]` | Set this session's reasoning effort |
| `perm` | | `[default\|ask\|lockdown\|yolo]` | Set the permission mode |
| `skin` | | `[name]` | Show or change the skin |
| `help` | | | Show the commands |
| `quit` | `exit`, `q` | | Quit; the session keeps running in soa-d |

`/help`: `commands:`, then `  /<name> <args> — <description>` per row (no
double space when there are no args), then `  !cmd runs a shell command here
· ctrl+p opens the palette · :q quits`.

## Pickers (T:533-680, B:854-1018)

They replace 9a's listing forms; with an argument each command acts at
once, as in 9a. A picker is a modal like 9a's: a title, an optional filter
row, the rows, a muted hint.

- Rows: when not filterable, `N ` muted for N up to 9 (else two spaces);
  the label, bold when it is the default; three spaces and the description
  muted; each row cut with `…`. The highlighted row is drawn in the accent
  colour, bold. At most 14 rows show, fewer when the screen is short (a
  modal never runs past the screen), scrolling with the highlight.
- Single choice: a digit picks (not filterable), `up`/`down` move, `enter`
  picks, `esc` cancels (nothing happens, no note). Hint: `1-9 pick · ↑↓ move
  · enter pick · esc cancel`.
- Filterable: a filter row (`type to filter…` until typed); typing narrows
  the rows to those whose label or key contains the text, case-insensitive;
  the highlight goes to the first match. Hint: `type to filter · ↑↓ move ·
  enter pick · esc cancel`.
- Several choices (`/cancel`, and 9a's multi-select question): rows start
  `[ ] ` or `[x] `; `space` toggles the highlighted row, `enter` confirms.
  Hint: `space toggle · enter confirm · esc cancel`.

| command | title | rows (key, label, description) | default, filter | empty |
|---|---|---|---|---|
| `/model` | `Switch model  ·  now <current>` | `model.list`: (name, `name  ●` for the current one else name, `reasoning: <efforts joined by ", ">` or empty) | the current one; filterable | |
| `/reasoning` | `Reasoning  ·  now <effort or default>` | (`""`, `default`, `the configured default`), then each of the model's efforts | the first; not filterable | `<model> has no configured reasoning levels` |
| `/resume`, `/sessions` | `Resume session` | `session.list {limit: 500}`: (id, title cut to 80 or `untitled`, `<~cwd> · <updated as YYYY-MM-DD HH:MM>`) | filterable | `no sessions to resume` |
| `/cancel` | `Drop queued messages` | (id, the text cut to 100 with `…`) | several | `nothing queued` |

Picking acts as the command with that argument would. `/cancel` drops the
picked items with `queue.remove` and notes `dropped <N> queued message(s)`;
picking none drops nothing.

## Completion (C, T:2060-2160)

A list above the prompt frame, between the queued strip and the frame:
background `surface`, rows padded 3 on the left and 1 on the right, at most
9 rows; a row is the candidate bold, left-justified to `min(longest + 2,
48)` columns, then its description muted, cut with `…`. The highlighted row
is in the accent colour, bold, with no background.

- **Command names**: a `/word` at the start of the prompt with nothing after
  it: every command and alias starting with the word, in table order, with
  the description (aliases: `alias for /<name>`). Opens while typing.
- **Arguments**: after `/<command> ` on one line: `/model` the
  `model.list` names (fetched once per prompt); `/reasoning` `default` and
  the model's efforts; `/perm` the four modes; `/skin` the skin names;
  `/resume` nothing. Opens while typing only once the word is
  not empty; `tab` opens it on an empty word.
- **Paths**, `tab` only: a word starting `./`, `../`, `~/` or `/`, or
  containing a `/`, completes against the session's cwd: entries whose name
  starts with what follows the last `/`, directories first, then files, each
  by name, hidden entries only when the typed name starts with `.`; a
  directory inserts with a trailing `/` and no space (the list opens again
  inside it), a file with a space after.
- `tab` with a list open, or `enter` with it open, accepts the highlighted
  row: the word is replaced by it (commands and arguments followed by a
  space, unless one already follows). `enter` on a row equal to the typed
  word sends instead. `up`/`down` move in the list; `esc` closes it; typing
  updates it. `tab` with no list: one candidate is inserted at once; several
  extend the word to their common prefix, or open the list when there is no
  longer prefix. `tab` never moves focus.
- `@` references are not completed: soa-d does not expand them.

## Suggestions (T:356, 403-517)

While the draft is one line, the cursor at its end and no list open, the
rest of the newest history entry that starts with the draft (and is longer,
and one line) is drawn after the cursor, muted. `right` takes it; any other
key leaves it a suggestion. A recalled entry does not open the list.

## The palette (T:715-740, B:820-829)

`ctrl+p` opens a filterable picker titled `Commands`: every command and
alias as `/<name>`, the description `<category> · <description>` (an
alias: `<category> · alias for /<name>`)
(categories: Session for new through status, Configuration for model
through skin, Info for help, Exit for quit). Filtering matches the command
name first; when nothing matches the name, the description. Picking puts
`/<name> ` in the prompt (replacing the draft), not sent. `esc` closes it.

## The editor (T:2322-2340, B:1070-1100)

`ctrl+g` writes the draft to a temporary file `son_of_anton_prompt_*.md`
after the header

    #! Compose your prompt below. Lines starting with '#!' are ignored.
    #! Save and quit to send; leave empty to cancel.

and a blank line, and runs `$VISUAL`, else `$EDITOR`, else `nano` on it
through `sh -c '<editor> "$1"'` (so an editor with arguments works), with
the program suspended (Bubble Tea's ExecProcess) and the prompt empty. Afterwards the file's
lines starting `#!` are dropped and the rest trimmed: text is sent as typed
(and recorded in history); nothing restores the draft. The status shows
`<spinner> running editor` meanwhile. The file is removed.

## Decisions made without the owner

- Command descriptions describe what soa-d does, not the Python's options
  it never had (`--global`, `--provider`, `show|hide`, `--delete`).
- No `@` completion, since nothing expands the references.
- `/help` mentions the palette now that there is one.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md` (with the race run of
`./internal/client/ ./internal/tui/`), `ruff check .` and `ruff check
--select F401,F811,F821,F841 .`, `scripts/run_tests.sh`, and `nix flake
check`. One commit: summary `daemon: phase 9c (Go TUI pickers and prompt
helpers)`, body listing what changed, ending with the `Model:` line.
