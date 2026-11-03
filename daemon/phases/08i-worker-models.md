# Phase 8i: fiona picks a worker's model

Goal: fiona (a chat session, SPEC section 12) hands work to workers with the
relay tool, and a worker runs on whatever model its session has (a new
worker starts on `model.default`, usually maitred's `auto`). The owner wants
fiona to choose a worker's model per task, and to know which models are up
and what each is good for. Today she only sees the per-turn "[Model: your
last reply came from X.]" note, and guesses.

Read first: SPEC sections 12 (fiona, relay) and the cache rules in
`daemon/AGENTS.md` and the top-level `AGENTS.md`; `internal/server/relay.go`,
`internal/tools/relay.go`, `internal/server/models.go` (modelRoutes,
defaultRoute, modelCommand), `internal/server/chat.go` (chatRunner and its
ModelNote), `internal/agent/agent.go` (where ModelNote is appended to
`api_content`), `internal/config/config.go` (ModelRoute).

## What fiona sees

A chat's system prompt is frozen for its life, and chats live for months, so
nothing here goes in the system prompt. Two things carry it, both reaching
existing chats at once:

1. **The relay tool's schema** (sent with every request). `send` gets an
   optional `model` property:
   `"send: the model this worker runs on, from its next turn: one of the names in the latest [Models] note, or \"default\" for this instance's model. Leave it out to keep the worker's model."`
   The tool description gains one sentence, at its end:
   `"A send can also choose the worker's model: pick from the latest [Models] note by what the task needs, and tell the user which you chose."`
   Change nothing else in that description (the project wrote it; it is the
   relay's whole instruction set).

2. **A models note on a chat's turn**, on the `api_content` sidecar the way
   the served-model note is (stored with the user message, so it is part of
   the cached transcript from then on and never changes past context). It is
   added on a chat's first turn after the daemon starts, and on any later
   turn when the list differs from the one last noted to that chat (kept in
   memory, per chat; a restart notes it once more). Exact form:

   `[Models for relay send's model, up now: NAME (DESCRIPTION; CONTEXT context); NAME (...); default is DEFAULT.]`

   - NAME: every name a session may be set to (`modelRoutes`: the configured
     routes plus the provider's live `/models` list), sorted.
   - DESCRIPTION: the route's `description` (below); left out when empty.
   - CONTEXT: the window from the route's `context_length` or the provider's
     metadata, as `128k` or `1M` (thousands rounded down, `M` from a
     million); left out when unknown. With neither, no parentheses.
   - DEFAULT: `model.default`.
   - When the provider's list cannot be fetched, the configured routes alone
     are listed; when there are no names at all, no note.

   Only chat sessions get it; workers, the TUI and cron never do.

## Config

`custom_providers.<provider>.models.<name>.description` (string, optional):
what the model is good for, in a few words ("strongest reasoning, slow",
"fast, light tasks"). The owner sets it in `/etc/nixos`. Nothing else reads
it.

## Relay behaviour

- `send` with `model`: before the message is queued or started, the worker
  session's model is set exactly as `session.set` with `{"model": NAME}` does
  (the same validation, persisted on the worker session, effective from its
  next turn; a running turn finishes on its model). `"default"` sets
  `model.default`. A name that is not known fails the send with
  `unknown model NAME; known: A, B, C` and sends nothing. With `new_in`, the
  new worker starts on the chosen model.
- `list` and `status` add `"model"` (the worker session's model) to each
  worker, and `"served_model"` when a reply in this daemon's life named one.

## Tests (contract, before the code)

- `send` with `model` sets the worker's stored and live model; its next turn
  requests that model (fakellm sees it); `"default"` returns it to
  `model.default`; an unknown name fails with the listed names and queues
  nothing; `new_in` plus `model` starts the worker on it.
- `list` and `status` report each worker's model.
- The models note: present on a chat's first turn (in the request the fake
  sees, on that turn's user message), absent on the next turn when the list
  is the same, present again after the provider's `/models` list changes;
  descriptions and windows appear as specified; a worker's and an
  interactive session's requests never carry it; the system prompt bytes are
  unchanged by any of this.
- The relay schema has the `model` property with a description.

## Done when

The Go gates in `daemon/AGENTS.md` pass. One commit: summary `daemon: phase
8i (fiona picks a worker's model)`, body listing what changed, ending with
the `Model:` line.
