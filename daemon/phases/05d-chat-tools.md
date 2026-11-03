# Phase 5d: fiona sees what she is sent, and schedules for her chat

Goal: the last pieces of a chat's toolset (SPEC section 12, phases 5b and
6): `vision_analyze` (fiona "reads images", as her identity says) and
`cronjob` (a job made in a chat reports back to it), and read access to
what the chat was sent.

## Already written (do not modify)

`internal/server/chat_extras_server_test.go`, and the chat tool lists in
`chat_server_test.go` and `fiona_server_test.go`.

## Changes

- Chat sessions gain `cronjob` and `vision_analyze` (builtin order; the
  list in the tests is sorted). Their `cronjob` knows the chat
  (`tools.Cron.Chat` the session's stamp), so a job it creates has the
  chat as its `origin` and delivers there (`deliver` "origin").
- `paths.Policy` gains `ReadAlso []string`: directories a confined policy
  may read but never write. A chat session's policy has
  `<home>/cache/signal` (where 5a saves attachments) there, so
  `read_file`, `search_files` and `vision_analyze` can open what the chat
  was sent; `write_file` and `patch` still reach only the notes folder.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 5d (chat
tools)`, body listing what changed, ending with the `Model:` line.
