# Phase 10a: the son-of-anton command in Go

Goal: `son-of-anton` stops being Python. Every instance runs soa-d (2026-11-03)
and the owner asked to delete the Python; the command is the first piece: a
small Go program that runs soa-d for the terminal UI, `-q` and `-z` (as the
Python router does today, `son_of_anton_cli/main.py:_route_to_daemon`), and
adds `status` and `sessions`. The physics mode stays Python until its Go port
(phase 11); `son-of-anton problem` hands it on. Phase 10b then deletes the
rest of the Python.

Read first: `phases/09a-tui-core.md` (`soa-d tui` and its flags),
`phases/08a-import.md` and `07e-foreign.md` (`soa-d import`),
`phases/02a-server.md` (methods), `cmd/soa-d/main.go`, `internal/client`,
`tests/test_daemon_routing.py` (today's routing contract) and
`son_of_anton_cli/subcommands/problem.py`.

## Already written (do not modify)

`cmd/son-of-anton/main_test.go`, `internal/server/sessions_admin_server_test.go`,
`tests/test_physics_entry.py`.

## cmd/son-of-anton (new)

```
son-of-anton [flags]                          the terminal UI
son-of-anton [flags] PROMPT...                one query (the words joined by spaces)
son-of-anton [flags] -q PROMPT | --query PROMPT | --query-file FILE|-
son-of-anton [flags] -z PROMPT | --oneshot PROMPT
son-of-anton status
son-of-anton sessions [list] [--kind KIND]... [--all] [--limit N]
son-of-anton sessions rename ID TITLE...
son-of-anton sessions delete ID [--yes]
son-of-anton sessions import ARGS...          soa-d import's own flags
son-of-anton problem ARGS...                  the physics mode
son-of-anton chat [flags] ...                 the same as without `chat` (old spelling)
son-of-anton --version | -V
```

Flags: `-m`/`--model M`, `--reasoning L`, `--yolo`, `--home DIR` (else
`$SON_OF_ANTON_HOME`, else `~/.son-of-anton`, as `config.Home`), and for the
UI only `--in DIR`, `-c`/`--continue`, `-r`/`--resume ID|latest`; for `-z`
only `--usage-file F`. A flag's value may follow it or be joined with `=`.

**Running soa-d.** The UI, a query and a one-shot replace this process with
soa-d (`syscall.Exec`): `$SOA_D_BIN`, else `soa-d` beside this executable,
else `soa-d` on `PATH`; none found is `son-of-anton: soa-d is not installed`,
exit 1. The arguments, in this order:

- UI: `tui --home H [--model M] [--reasoning L] [--yolo] --in DIR [-c | --resume ID]`,
  `DIR` the absolute `--in` or the working directory (a missing `--in`
  directory: `son-of-anton: --in directory not found: DIR`, exit 1).
- query: `query --home H [--model M] [--reasoning L] [--yolo] PROMPT`. With
  `--query-file`, the file's text (`-` reads stdin); an empty one is
  `son-of-anton: --query-file FILE is empty`, exit 2.
- one-shot: `oneshot --home H [--model M] [--reasoning L] [--yolo] [--usage-file F] PROMPT`.
- import: `import ARGS... --home H`.

Usage errors (usage on stderr, exit 2): an unknown flag or command; a flag
missing its value; `-c NAME` (resume by name; the message says to use `-c`
or `--resume ID`); `--in`, `-c` or `--resume` with a query or one-shot;
`--usage-file` without `-z`; `-q` and `-z` together.

**problem** runs `$SON_OF_ANTON_PYTHON -m autophysicist problem ARGS...`
(replacing the process); without `SON_OF_ANTON_PYTHON`: `son-of-anton: the
physics mode needs SON_OF_ANTON_PYTHON (the Nix package sets it)`, exit 1.
`autophysicist/__main__.py` (new, Python) takes `problem create|run` with the
options `son_of_anton_cli/subcommands/problem.py` declares and does what
`cmd_problem` does (the code moves there in 10b).

**status** never starts a daemon. It prints, one item a line:

```
home: <home>
daemon: running (pid <pid>, soa-d <version>)        | not running
served by: the son-of-anton-<instance> unit        | started on demand by the terminal UI
signal: on | off
cron: on | off
models: <model.default>, chats <gateway.model or "the default">
sessions: <n> <kind>, ...                            (only when running; kinds in a fixed order)
```

"served by" is the unit when `[daemon] autostart = false` (with
`[daemon] instance`), else on demand. Session counts come from `session.list`
over every kind; chats and other gateway sessions are behind the wall from
the terminal and are not counted (say nothing about them).

**sessions** connects with `client.Connect` (so it starts a daemon where the
UI would, never where a unit serves the home: that is Connect's own rule).

- `list`: the interactive and worker sessions (`--kind` picks others,
  repeatable; `--all` every kind), newest first, `--limit` (default 50).
  One line each: `<id>  <kind>  <updated as YYYY-MM-DD HH:MM>  <title or
  (untitled)>  <cwd with ~>`, columns separated by two spaces; none: `no
  sessions`.
- `rename ID TITLE...`: `session.rename`; prints `renamed <id>: <title>`.
- `delete ID`: asks `delete <id> (<title>)? [y/N] ` on stdin unless `--yes`;
  anything but `y`/`yes` prints `not deleted`, exit 1. Then `session.delete`;
  prints `deleted <id>`.

An RPC error is `son-of-anton: <message>`, exit 1.

## The daemon

Two methods, not driver-only (a TUI attached as driver keeps its role):

- `session.rename {session, title}`: sets the title (title source `user`),
  `session.updated` to attached clients; `{}`. Empty title: -32602.
- `session.delete {session}`: deletes the session and everything stored for
  it (messages, compactions, queue, foreign import record, and its children).
  Refused with -32013 `session in use` while its turn runs or a client is
  attached; `{}`. Unknown or behind the wall: -32011.

Both go through the wall like every socket path (`socketSession`).

## Nix

The Go package builds `cmd/soa-d` and `cmd/son-of-anton`. `$out/bin/son-of-anton`
is the Go command, wrapped like `soa-d` (the runtime `PATH`,
`SON_OF_ANTON_PYTHON` the sealed venv's `python3`, `SOA_D_BIN` the wrapped
soa-d). The shell completions the Python generated go (no replacement in
10a). `tests/test_daemon_routing.py` is replaced by the Go tests and deleted.

## README

"Sessions and the command line": the command list above, briefly.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md` (with `go test -race
./internal/server/`), `ruff check .` and `ruff check --select
F401,F811,F821,F841 .`, `scripts/run_tests.sh`, `nix build`, `nix build
.#soa-d` and `nix flake check`. One commit: summary `cli: phase 10a (the
son-of-anton command in Go)`, body listing what changed, ending with the
`Model:` line.
