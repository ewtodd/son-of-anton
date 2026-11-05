# Phase 9d: the Go TUI's subagent view and mouse

Goal: `task` calls show as they do in the Textual TUI (and opencode), the
user can open a child's transcript and step between siblings while the
parent runs on, and the mouse folds rows, scrolls and copies.

Read first: `phases/04b-subagent-view.md` (the behaviour, written for the
Textual TUI: this phase is it in Go), `phases/09a-tui-core.md` to
`09c-tui-input.md`, and the reference: `son_of_anton_tui/backend.py` (B,
especially 88-113 and 560-712), `tui.py` (T, 936-1062, 2239-2409), `feed.py`
(F, 425-544, 683-722, 949-991). Where this file and the Python differ, this
file wins. Next: 9e switching `son-of-anton` over.

## Already written (do not modify)

`internal/tui/subagent_test.go`, `internal/tui/mouse_test.go`.

## API additions

```go
func (a *App) Click(x, y int)            // a left press and release at a cell
func (a *App) Drag(x0, y0, x1, y1 int)   // a left press, motion, release
func (a *App) Wheel(x, y, notches int)   // notches > 0 scroll down, < 0 up
func (a *App) Copied() string            // the text of the last copy, "" before one
```

Cells are 0-based columns and rows of the screen as `Screen` draws it. In a
real terminal the program takes mouse cell motion (Bubble Tea's
`WithMouseCellMotion`).

## Subagents (04b's TUI half)

- **Task rows**: the label is the daemon's summary (`<Agent> Task —
  <description>`, history rows through B's `tool_summary`); no duration on
  the first line; a muted second line, the detail: `↳ <name> <summary>`
  from the latest `child.progress` while the child runs; on `child.done`
  with status done `↳ <N> toolcall(s) · <duration>` (or `↳ <duration>`
  with no tool calls; `toolcall` for one), B's `subagent_duration`
  (`450ms`, `12.3s`, `2m 5s`, `1h 2m`, `1d 1h`); otherwise none. A child maps
  to its row through `child.created`'s `call_id`; in history through the
  `<task id="…">` in its result, then one `session.children` call fills the
  done details. A background task's row finishes at once and its detail
  follows the child.
- **The hint**: one feed line, `ctrl+x down` then muted ` view subagents`,
  after the tool rows of an assistant message that called `task` (live once
  that message's calls have ended; in history after its rows). While any
  child runs, the status line's right side starts with bold `ctrl+x down`
  and ` view subagents  ·  `.
- **The leader**: `ctrl+x`, then within 2s `down` opens the newest child
  (`session.children`, newest first); with none it does nothing. Any other
  key ends the leader and acts as usual.
- **The child view** replaces the main screen: the child's feed (drawn by
  the same code as the main feed, no wordmark or intro), no prompt and no
  sidebar, then the footer: a 1-row pad, the row, a 1-row pad, on the panel
  colour with the accent `▎` rail, then one blank row. Left: the child's
  agent in titlecase bold (`Subagent` when empty), muted ` (<i> of <n>)`
  among its siblings in creation order, muted ` <tokens>` (opencode's
  `Locale.number`: as is under 1,000, then `1.2K`, `3.4M`) and ` (<pct>%)` when
  the context length is known, left out at 0 tokens. Right: `Parent ↑   Prev
  ←   Next →`, the arrows muted. It follows the child's `session.updated`.
  The child is attached as a viewer on the same connection; the parent
  stays attached as driver, its events keep drawing the main feed, so going
  back shows it current. Keys there (no modal open): `up` goes back
  (detaching the child), `right` the next sibling, `left` the previous, both
  wrapping; `esc` does nothing; the other keys act as on the main screen
  (`ctrl+c` interrupts the parent's turn, which cancels its children).
- **A child's approval** (its request has `child_session`): the title starts
  `<Agent> Task — <description>: `, read from the child's title `<description>
  (@<type> subagent)`. It opens over whichever view is showing.

## The mouse (F:949-991, T:2239-2283)

- A click on a reasoning header folds or unfolds it; on a tool row unfolds
  it: the whole label wrapped (continuation rows indented 2) and, below it,
  the call's output, wrapped and indented 4 in the muted colour, from the
  call's stored result (`tool.end`'s `result`, or the stored tool row's
  content in history). A second click folds it back to one line.
  `ctrl+t` expands or folds the most recent finished tool row. Elsewhere a
  click does nothing.
- A drag over the feed selects from the press cell to the release cell
  (rows in between whole, the last cell excluded), drawn in reverse video;
  on release the selected text, each row's trailing spaces trimmed and rows
  joined with `\n`, is copied: OSC 52 to the terminal, and `Copied`. A
  selection with no non-blank text copies nothing. The status line's right
  side shows `copied <N> chars` for 1.5s. The selection clears on the next
  key or click.
- The wheel scrolls the feed under the pointer 3 rows a notch; scrolling
  back to the bottom follows again (9a's rule).

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md` (with the race run of
`./internal/client/ ./internal/tui/`), `ruff check .` and `ruff check
--select F401,F811,F821,F841 .`, `scripts/run_tests.sh`, and `nix flake
check`. One commit: summary `daemon: phase 9d (Go TUI subagent view and
mouse)`, body listing what changed, ending with the `Model:` line.
