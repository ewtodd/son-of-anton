# Phase 4b: the TUI's subagent view

Goal: in the TUI, a `task` call shows as opencode shows it (the agent, the
description, the child's live progress), and the user can open any child's
transcript, step between siblings, and come back, while the parent keeps
running underneath. A few daemon additions feed it.

Read first: `SPEC.md` section 9, `phases/04a-subagents.md`,
`phases/02b-tui-client.md`, `TUI_AESTHETICS.md`, and the code
(`son_of_anton_tui/backend.py`, `tui.py`, `feed.py`). The reference is
opencode v1.19.5, cloned read-only at `~/.cache/claude-ref/opencode-v1.19.5`
(tui = `packages/tui/src/`): `tui/routes/session/index.tsx` (`children`,
`moveFirstChild`, `moveChild`, the `Task` component, the "view subagents"
hint), `tui/routes/session/subagent-footer.tsx`, `tui/util/locale.ts`
(`titlecase`, `number`, `duration`), `tui/config/keybind.ts` (leader
`ctrl+x`). Where this file and opencode differ, this file wins.

## Already written (do not modify)

`daemon/internal/store/usage_store_test.go`,
`daemon/internal/server/view_server_test.go`, and the "Subagents (phase 4b)"
section at the end of `tests/test_tui_daemon.py`.

## Daemon

**Usage is stored** (it was in memory only, so a resumed session or a
finished child read 0 context). Schema v8: `sessions` gains `input_tokens`,
`output_tokens`, `cached_tokens`, `reasoning_tokens`, `api_calls` and
`context_tokens` (integers, default 0).

```go
type Usage struct{ Input, Output, Cached, Reasoning, Calls int }
// Session gains Usage Usage and ContextTokens int, read by Session().
func (s *Store) AddUsage(ctx context.Context, id string, u Usage, contextTokens int) error // adds u; sets context_tokens when contextTokens > 0; unknown id: an error
type RunStats struct{ ToolCalls int; Duration time.Duration }
func (s *Store) RunStats(ctx context.Context, id string) (RunStats, error) // tool rows; last row's ts minus the first's (0 under two rows)
```

- Every turn end (any status) adds the turn's usage and its last request's
  context (what `ctxTokens` already holds) with `AddUsage`. A loaded session
  starts from the stored values, and `summaryObj` reports them instead of
  zeros, so `usage` and `context_tokens` survive unloading and a restart.

**Task rows.**
- `summarize("task", args)`: `"<Agent> Task — <description>"`, with
  `" (background)"` after "Task" when `background` is true. Agent is
  `subagent_type` in opencode's `titlecase` (each letter that starts a word
  upper-cased), "General" when missing. `isError` also counts a task result
  whose first line has `state="error"`.
- `child.progress` on the parent each time a child's tool starts:
  `{child_id, name, summary, tool_calls}`, `name` and `summary` as that tool's
  `tool.start` has them, `tool_calls` the child's calls so far, this one
  included (a resumed child counts on from its stored rows).
- `child.done` gains `tool_calls` and `duration_ms` (its `RunStats`), and
  each `session.children` entry gains the same two fields.

## TUI

**Task rows** (opencode's `Task`). `tool_summary("task", args)` is the
daemon's `summarize` exactly (history rows use it). `_ToolBlock` gains
`detail` (a string, "" for none), drawn as a second, muted line under the
label; `finish()` keeps it. A task row shows no duration on its first line.
The detail:
- while the child runs, after its first tool: `"↳ <name> <summary>"` from
  the latest `child.progress`;
- when the child is done (`child.done` status done):
  `"↳ <N> toolcall(s) · <duration>"`, or `"↳ <duration>"` with no tool calls
  (`formatCompletedSubagentDetail`); `subagent_duration(ms)` in
  `son_of_anton_tui/backend.py` is opencode's `Locale.duration`;
- interrupted or failed: none.

A background task's row finishes at once (its call returns at once), and its
detail keeps following the child until `child.done`. The backend maps a child
to its row through `child.created`'s `call_id`. In history, a task row's
child is the id in its result's `<task id="…">`; after rendering, one
`session.children` call fills in the done detail of each row whose result
state is completed or running.

**The hint** (opencode's "view subagents"): one feed line, `ctrl+x down`
then ` view subagents` muted, after the tool rows of an assistant message
that called `task`: live when that message's calls have all ended, in
history after its tool rows. While any child of the session runs, the
status row's right side also begins `ctrl+x down view subagents  ·  `.

**The leader.** `ctrl+x` is an app-level priority binding (so it works from
the prompt); the next key within 2 seconds is the leader's. `ctrl+x down`
opens the newest child of the session (opencode's `moveFirstChild`: its
children are newest first); with no children it does nothing. Any other key
ends leader mode and is handled as usual.

**The child view** is a pushed `SubagentScreen` (exported from
`son_of_anton_tui.tui`): the child's transcript in a `VirtualFeed` with id
`feed`, and a footer `Static` with id `subagent-footer`. No prompt, no
sidebar. The backend attaches the child as a **viewer** on the same
connection, keeping the parent attached as driver, and renders the child's
history and live events into the screen's feed with the same rendering code
as the main feed (no second copy of the feed-event handlers). The parent's
events keep reaching the main feed underneath, so returning shows it current.
`DaemonBackend.view_id` is the viewed child's id, `None` otherwise.

- Keys, only on the child view and with no dialog open: `up` returns to the
  parent (detaching the child), `right` the next sibling in creation order,
  `left` the previous, both wrapping (opencode's `moveChild`). The app's other
  bindings act as on the parent (ctrl+c interrupts the parent's turn, which
  cancels its children).
- The footer (opencode's `SubagentFooter`): left, the label bold (the
  child's `agent` in `titlecase`; "Subagent" if empty), then muted
  `(N of M)` among its siblings in creation order, then muted the context:
  `Locale.number(context_tokens)` plus ` (<pct>%)` when the context length is
  known, left out at 0 tokens. Right: `Parent ↑   Prev ←   Next →`. It
  follows the child's `session.updated`. No cost: soa-d has no prices.
- An approval whose request has `child_session` names the subagent: the
  modal's title is prefixed `"<Agent> Task — <description>: "`, Agent and
  description read from the child's title `"<description> (@<type>
  subagent)"`. It opens on whichever screen is showing.

## README

Status: drop "subagents (4)" from "Not in the daemon yet". Under "Sessions
and the command line", one bullet: the agent can hand work to subagents
(`explore` searches and reads; `general` has the agent's tools), several at
once; `ctrl+x ↓` opens the newest one's transcript, `←`/`→` move between
them, `↑` comes back.

## Not in 4b

Clicking a task row to open its child, a sidebar in the child view,
promoting a running task to the background, costs.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 4b
(subagent view)`, body listing what changed, ending with the `Model:` line.
