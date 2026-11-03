# Phase 5e: active hours for Signal chats

Goal: an instance that shares the GPU with the owner's working day answers
its Signal chats only inside a window, as the Python gateway's
`gateway.active_hours` does today for markets and ricky (8pm to 7am).
Outside it, nothing runs a turn: messages are saved, each chat is told once,
and when the window opens the chat is shown what came in and asked whether
to go ahead. Until this exists those two instances cannot move to soa-d.

Read first: `phases/05a-signal.md`, `phases/05b-fiona.md`,
`phases/05c-relay.md` (worker hours: the same kind of window), and the code
(`internal/server/chat.go`, `relay.go`). The reference is the Python:
`gateway/active_hours.py` (AH), `gateway/held_messages.py` (HM),
`agent/hour_window.py`, and in `gateway/run.py` `_active_hours_notice`
(8650-8690), the hold and the confirmation (9330-9455) and
`_offer_held_messages_on_reopen` (15300-15361). Where this file and the Python
differ, this file wins.

## Already written (do not modify)

`internal/server/hours_server_test.go`, `internal/server/testclock_test.go`.

## Config

`[gateway] active_hours = [start, end]` (local hours 0-23, `[20, 7]` wraps
past midnight: 20:00 through 06:59) and `inactive_message` (the notice; empty
means AH's default). Parsed like `relay.worker_hours`, with the same warning
for a value that is not two hours; unset means always open. Local time is
the zone worker hours use (`SON_OF_ANTON_TIMEZONE`, else the local zone).

## The clock

The server reads the time through `now()` (package server), and
`setClock(f func() time.Time) (old func() time.Time)` swaps it; both are safe
for concurrent use (an atomic). `testclock_test.go` exposes `SetNow` on it for
the tests. Active hours use `now()`; worker hours may too.

## Outside the window

A chat message that would start or queue a turn (not a control command:
`/stop`, `/new`, `/sendnow`, `/sendall`, `/status`, `/approve`, `/deny`, and
an answer to an open question or approval) is held instead: saved with its
sender (the Signal name, else the number, else `someone`), its text (the
first 2,000 characters) and its time, durably, per chat (schema v12,
`held_messages (chat, seq, sender, text, ts)`). No turn runs and nothing is
queued.

The chat is told once per closed stretch (AH's gap key: the date the stretch
began and the end hour): the `inactive_message`, else `💤 Not on duty right
now — I run 20:00–07:00. Your message is saved; I'll ask about it when I'm
back.` with the window as `HH:00–HH:00`. Later messages in the same stretch
are held silently. The told-in-this-stretch mark may live in memory.

Control commands work as at any hour. Internal events (a relay worker's
completion, cron) are not held.

## When the window opens

For each chat with held messages, the summary is sent (HM's `summarize`,
exactly):

    <N> message[s] came in while I was off duty:

      • <sender>: <text, first 200 characters, then …>
      ...
      … and <K> more        (past the first 10)

    Process these? Reply **yes** to proceed or **no** to discard.

(a message with no text shows `(empty or media-only)`), and the chat is
marked as asked (durably: a restart keeps the question open). It is sent:

- at start-up inside the window, and when the window opens (checked every
  `hoursEvery`, with worker hours' watcher);
- on the chat's next message inside the window, when the chat has held
  messages and has not been asked yet: the summary is the reply, and that
  message is held too (it is shown with the others).

## The answer

While a chat is asked, its next message is the answer (HM, run.py
9360-9425):

- `yes`, `y`, `go`, `proceed` (case-insensitive, trimmed): the held
  messages become one turn whose text is HM's `compose_held_turn`:
  `The following messages were held while I was off duty. Please address
  them:` then, a blank line apart, `[<sender>]: <text>` for each with text.
  They are cleared.
- `no`, `n`, `skip`, `discard`: they are cleared and the chat is told `Held
  messages discarded.`
- a control command: the question is dropped (the held messages cleared) and
  the command runs.
- anything else: `Reply **yes** to process the held messages, or **no** to
  discard them.`

## README

Under the Signal paragraph: one sentence on `[gateway] active_hours` and
`inactive_message`.

## Decisions made without the owner

None beyond the Python's behaviour.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md` (with `go test -race
./internal/server/`), `ruff check .` and `ruff check --select
F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole suite), and `nix
flake check`. One commit: summary `daemon: phase 5e (active hours for
Signal chats)`, body listing what changed, ending with the `Model:` line.
