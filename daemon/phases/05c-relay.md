# Phase 5c: the relay

Goal: fiona hands real work to son-of-anton **workers** with one tool,
`relay` (SPEC section 12). A worker is a daemon session like any other
(kind `worker`), so a terminal can take it over with `/pickup`, and none of
the Python's host/worker/yield machinery exists.

Read first: `SPEC.md` sections 3 and 12, `phases/05a-signal.md` and
`05b-fiona.md`, AGENTS.md "The relay" (the Python's design, for its
reasons), and the code. The reference is `tools/relay_tool.py` (RT) and
`gateway/relay_workers.py`. Where this file and the Python differ, this
file wins.

## Already written (do not modify)

`internal/server/relay_server_test.go`, and the chat tool list in
`chat_server_test.go` and `fiona_server_test.go` (gaining `relay`).

## Decisions made without the owner (2026-11-03, night)

- The relay drives workers only (sessions it started). The Python could also
  bind a terminal session to the chat; with soa-d a terminal user just
  `/pickup`s the worker instead. **The owner reversed this on 2026-11-05: see
  "Terminal sessions" below.** There was never meant to be a difference
  between a worker and a terminal session.
- A worker's approvals and questions go to the terminal that has picked it
  up when there is one, else to its chat.
- When a worker's turn ends, its chat gets a notice turn (the 2a notice path
  on the chat session) carrying the outcome, and fiona tells the user.

## The worker

Kind `worker`, source `relay`, scope `common` (the terminal may see it),
`owner` the chat stamp that started it (schema v10: `sessions.owner`, never
the `chat` column, which is the wall's), cwd the `new_in` directory (it must
exist), model `model.default`, the full prompt of an interactive session
(identity, guidance, the cwd's project context, common rules) with recall,
and an interactive session's tools (not `relay`, not `cronjob`). Its title
is derived from its first message like any session's.

Bindings (schema v10: a `relay_bindings` table, chat stamp to worker id):
the worker a chat last started or sent to. On a shared instance
(`gateway.single_user` false) a chat sees and drives only the workers it
owns; with `single_user`, every worker.

## Hours

`relay.worker_hours` (5b). Outside the hours a send is queued on the worker
(its durable queue) and the worker stays idle; within them the queue runs as
usual. The daemon checks every minute and starts a waiting worker's queue
when its hours begin. A worker a terminal has picked up ignores the hours.

## The relay tool

Chat sessions only (after `web_extract` in their list; never anywhere else).
Schema RT:84-140 with these words changed in the description: "a relay to
the user's terminal coding sessions" becomes "a relay to son-of-anton
workers", "terminal session" and "session" become "worker", and the list
entry reads "- list: this chat's workers, most recent first (optional
`workspace` path filter), each marked bound to this chat, with its state:
running, idle, queued (with when it will run) or picked up (a terminal is
driving it)." Parameter descriptions RT:118-140, with "Session id or title"
becoming "Worker id or title".

Every answer is JSON with `"success"`. The worker is `session` (an id, or a
title matched case-insensitively, among the workers this chat may drive),
else the chat's bound one.

- `send {message, session?, new_in?, now?}`: `new_in` starts a new worker
  there and binds it. With no worker and no `new_in`: `{"success": false,
  "error": "no worker is bound to this chat: start one with new_in (a
  directory), or pick one from list"}`. Then: picked up by a terminal →
  queued, `"when": "when the terminal that picked it up gets to it"`;
  outside hours → queued, `"when": "queued until HH:00"`; running → queued,
  `"when": "after the current turn"`, or with `now` steered, `"when":
  "now"`; idle → started, `"when": "now"`. Answer `{"success": true,
  "worker": id, "title", "status": "started"|"queued"|"steered", "when"}`.
- `status {session?}`: `{"success": true, "worker", "title", "cwd",
  "state", "queued": [texts], "approvals_waiting": n, "last_reply": its
  final reply so far, first 500 characters}`.
- `stop {session?}`: interrupts its running turn (`{"success": true,
  "stopped": true|false}`); a picked-up worker's terminal sees the interrupt
  like its own.
- `detach {session?}`: unbinds it from this chat (it keeps running).
- `list {workspace?}`: `{"success": true, "workers": [{"worker", "title",
  "cwd", "state", "bound", "last_active"}]}`, newest activity first.
- A worker not found (or another chat's on a shared instance):
  `{"success": false, "error": "no worker <ref> for this chat"}`.

## What the chat hears

When a worker's turn ends, a notice turn starts on its owner chat's session
(queued if that session is busy) with the text "[worker <title> (<id>)
finished]\n<its final reply>", "[worker <title> (<id>) failed: <error>]" or
"[worker <title> (<id>) was stopped]"; a steered turn sends nothing (its
steer turn follows). Approvals and questions of a worker
nobody has picked up go to its chat as a chat session's do (5a), each
prefixed "Worker <title>: ".

## /pickup

The TUI's `/pickup [id or title]` (`son_of_anton_cli/commands.py` gains it;
handled in `son_of_anton_tui/backend.py`): with no argument, a picker of
`session.list {kinds: ["worker"]}`; then the worker is attached as driver,
like `/resume`. While a terminal drives it, sends from the chat queue on it
(the terminal sees them in its queue strip) and its approvals and questions
ask the terminal. `/resume` of a worker works the same way. Leaving it
(another `/resume`, `/new`, quitting) hands it back to the chat.

## Terminal sessions (2026-11-05)

The other direction of `/pickup`, on single-user instances only; a shared
instance's chats still see only the workers they started.

- `relay list` also shows the ten most recent interactive sessions (leaving out
  empty idle ones), and every entry says its `kind`. A terminal with a queue
  nobody will run shows `queued (waiting for a terminal)`.
- `relay send` with an interactive session's id or title continues that session
  in place. It is never converted or copied: kind, prompt, history, model and
  directory stay what they were.
- The first send takes it on: `sessions.owner` is set to the chat, and the live
  session gets the chat link a worker has. From then on, whenever no terminal
  drives it, it acts as a worker of that chat. Its turns report back, its
  approvals and questions go to the chat (ones already waiting are asked at
  once), and its queue runs, within worker hours.
- A send takes any session, worker or terminal, from an attached terminal, even
  mid-turn, within worker hours. The chat and the terminal are one person on a
  single-user instance, so someone writing from the chat is not at the terminal
  they left open; and if a queued message waited behind a terminal's turn, its
  report would never reach them. The terminal gets `role` with `as: viewer, by:
  chat`, waiting approvals and questions move to the chat, and the send's result
  says `took_from_terminal`. Outside worker hours the terminal keeps it.
- The TUI closes when it loses the driver role, to the chat or to another
  terminal, and prints who took the session and `son-of-anton -r <id>`. That
  command takes it back: a turn that ends while a terminal drives a terminal
  session is not reported.
- `relay detach` lets it go: the owner is cleared and it waits for a terminal
  again.
- `new_in` is refused while a session the chat may drive has a turn running in
  that directory's workspace, naming the session to send to instead.
- `/handoff` (`session.handoff`, driver only) is the same from the terminal's
  end: it gives the session to the chat used last, sends fiona a notice saying
  whether it is running, and the TUI closes and prints how to take it back. The
  terminal leaving gives the chat the session's approvals, questions and queue.
  An instance has one Signal group, so there is no chat to choose, but one
  group can have several stamps, one per sender: any chat may let a terminal
  session go, not only the stamp that took it on.

## README

Under "Signal": fiona relays work to workers (`relay`), which run in their
own directories, respect worker hours, and report back; a terminal takes
one over with `/pickup`.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 5c (the
relay)`, body listing what changed, ending with the `Model:` line.
