# Phase 8g: models in chats, dead pins, and the databases after the move

Goal: after every instance moved to soa-d (2026-11-03), the owner found
fiona guessing which model she runs on, `/model` unknown in a chat, and
asked to clean up the databases: sessions pinned to a renamed model
(`unholy/glm53` is now `unholy/glm5.3-flash`), each instance's old Python
`state.db`, and the unused tables of the reverted active hours.

Read first: `phases/05a-signal.md`, `phases/05b-fiona.md`,
`phases/08a-import.md`, `phases/08b-nix.md`, and the code
(`internal/server/chat.go`, `live.go`, `internal/agent/agent.go`,
`internal/config/config.go`, `internal/store/store.go`,
`cmd/soa-d/import.go`, `cmd/soa-d/main.go`, `nix/moduleCommon.nix`). The
Python reference for dead pins is `_drop_dead_model_override` in
`gateway/run.py` (around 15363). Where this file and the Python differ, this
file wins.

## Already written (do not modify)

`internal/server/models_server_test.go`, `internal/store/cleanup_store_test.go`,
`internal/config/renamed_test.go`, `cmd/soa-d/retire_test.go`, and the
`nixos-soad-backend` assertions in `nix/checks.nix`.

## 1. fiona is told which model answered last

In a chat session (kind `chat`), once a reply has come back since the session
loaded, each new user message's per-turn context (the same place recall's
block goes: stored as the message's `api_content`, so the prompt stays
byte-stable and later requests replay the same bytes) ends with one line:

    [Model: your last reply came from <served model>.]

`<served model>` is `ServedModel()` (maitred's `x-maitred-model`, else the
session's model). Before any reply in this run there is no line. Other
session kinds get nothing.

## 2. `/model` in a chat

A control command like `/status` (it bypasses the queue, works while busy):

- `/model`: `Model: <the chat session's model>` then, once a reply has come
  back, `; the last reply came from <served>`, then `. /model <name> switches
  this chat (e.g. /model auto).` as one message.
- `/model <name>`: `<name>` must be one of the names `model.list` would give
  (the provider's routes and its `/models` ids); then the chat session's
  model is set (stored, as `session.set` does; effort reset as there) and the
  reply is `Model: <name>, from the next message.` An unknown name: `Unknown
  model <name>. Known: <the names, sorted, comma-separated>.`
- `/new` starts the next session on the instance's chat model again (as
  today).
- The unknown-command reply lists `/model` with the others.

## 3. A dead pin falls back

When a request fails with HTTP 404 and the session's model is not the one it
would start with (the chat's model for a chat, `model.default` for the
others), the model is gone (renamed or removed): the session is set back to
that default (stored), a `notice` event (level `warning`) and, in a chat, a
chat message say `<old> is no longer served; this session is back on
<default>.`, and the request is sent again, once, on the default. A 404 on
the default itself is an error as before.

## 4. Declared renames

`[models] renamed = { "<old>" = "<new>", ... }` in config.toml. 
`config.Endpoint` resolves a renamed name to the new one (so a cron job or
any other pin keeps working), and at daemon start every stored session whose
model is a renamed name is rewritten to the new name (one UPDATE per
rename). Chains are not followed (old to new only).

## 5. The Python state.db is imported and retired

With `[daemon] import_python = true` (Nix sets it on instances with
`backend = "soa-d"`, next to `autostart = false`), `soa-d serve` checks,
before it listens, for `<home>/state.db`: if present it runs the phase 8a
import into `soa-d.db` (sessions already imported are skipped, as `soa-d
import` does), then renames `state.db` and any `state.db-wal`/`-shm` to
`state.db.imported` (`-wal`, `-shm` likewise), so it is kept but never read
again. An import error is logged and leaves `state.db` where it is; the
daemon starts anyway. Without the setting nothing happens (a TUI-started
daemon on a home whose Python gateway still runs must never touch it). The
`son-of-anton` CLI's Python subcommands that read `state.db` then find none;
they go with the Python backend.

## 6. Schema v13

Drops `held_messages` and `held_asked` (v12, unused since the active-hours
revert).

## README

Under "Sessions and the command line" or the Signal section: one line each
for chat `/model`, `[models] renamed`, and that a soa-d instance imports and
retires its Python `state.db` on first start.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md` (with `go test -race
./internal/server/`), `ruff check .` and `ruff check --select
F401,F811,F821,F841 .`, `scripts/run_tests.sh`, and `nix flake check`. One
commit: summary `daemon: phase 8g (models in chats, dead pins, database
cleanup)`, body listing what changed, ending with the `Model:` line.
