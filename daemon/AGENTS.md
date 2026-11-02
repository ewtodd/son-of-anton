# soa-d - working agreement for agents

You are working on `soa-d`, the Go daemon that replaces son-of-anton's Python
backend. The Python code in the parent directory is the running system and the
behavioral reference; do not modify it unless a task says so.

Read `SPEC.md` (the approved master spec) before anything else. The current
phase's task is in `phases/`.

## Rules

- **The tests are the contract.** Phase contract tests are written before the
  code and reviewed. Make them pass by implementing the specified API; never
  weaken, skip, or delete a contract test. If a test cannot compile because of
  an obvious typo in the test itself, fix only that and list the change in your
  report.
- **Never touch a real home.** No reads or writes of `~/.son-of-anton`, no
  calls to real model endpoints, no ssh. Tests use `t.TempDir()` homes and the
  `internal/fakellm` server.
- **Never touch the Python fork's state.** soa-d's database is
  `<home>/soa-d.db`; `state.db` belongs to the Python fork until migration.
- **Pure Go.** `CGO_ENABLED=0` always (SQLite is `modernc.org/sqlite`).
- **Small and plain.** Standard library first; a dependency needs a reason.
  No speculative abstractions, no code for later phases.
- Go 1.26, packages under `internal/`, the binary under `cmd/soa-d`.

## Gates (all must pass before you commit)

```bash
cd daemon
export CGO_ENABLED=0
test -z "$(gofmt -l .)"
go vet ./...
go test ./...
cd .. && nix build .#soa-d && nix flake check
```

`git add` new files before `nix build` / `nix flake check`: the flake only
sees files in the git index.

## Commits

The repo's git config sets the author (`son-of-anton-bot`); do not change it.
One summary line (`daemon: what changed`), a blank line, then a body saying
what changed and why, ending with one line `Model: <id>` naming the model that
wrote it (the exact id, never `auto`). No `Co-authored-by` trailers of any kind.
Never push.
Do not commit task scratch files (`TASK.md`, `REPORT.md`, `usage.json`,
`final.txt`).
