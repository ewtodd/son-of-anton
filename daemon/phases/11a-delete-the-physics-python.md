# Phase 11a: delete the Python physics mode

Goal: the Python Autophysicist (`autophysicist/`, with its toy problems in
`problems/`) has never produced a successful run, and the owner decided
(2026-11-03) to delete it now, whatever the state of its replacement. Phase
11b rebuilds the physics mode in Go on a different design; nothing here
prepares for it beyond keeping the physics runtime package. After this phase
the repository has no Python at all.

Read first: `AGENTS.md`, `daemon/AGENTS.md`, `flake.nix`, every file in
`nix/`, `daemon/cmd/son-of-anton/main.go` (the `problem` subcommand) and its
tests, and the places that read `SON_OF_ANTON_PYTHON`
(`internal/tools/execute_code.go`, `internal/cron/script.go`).

## Delete (git rm)

- `autophysicist/` and `problems/` (all of both).
- `nix/physics-harness.nix`.

## Keep

- `nix/physics-runtime.nix` and `packages.physics-runtime`: the interpreter
  phase 11b runs computations under, and `/etc/nixos` refers to it. Its
  comments say what it is for without naming the deleted harness.
- `SON_OF_ANTON_PYTHON`: `execute_code` and cron's script jobs run on it. It
  becomes plain `python3` from nixpkgs (the standard library is all
  `execute_code` promises), set by the wrapper as today.

## Change

- `nix/packages.nix`: no `physics-harness` or `physics-harness-test`
  packages; `son-of-anton.nix` gets nixpkgs `python3` for
  `SON_OF_ANTON_PYTHON`.
- `nix/son-of-anton.nix`: the argument and comments follow (no "physics
  harness"); the package description says what the package is now.
- `nix/checks.nix`: the physics-tests check goes; the package smoke no longer
  runs `son-of-anton problem --help` but still checks that the wrapper sets
  `SON_OF_ANTON_PYTHON` to a working `python3`.
- `nix/devShell.nix`: Go, gopls, gcc and plain `python3` (the Go tests of
  `execute_code` run Python); no pytest, no physics line in the greeting.
- `flake.nix`: the description drops "with physics research modes".
- `son-of-anton problem ...`: prints `the physics mode is being rebuilt
  (daemon/phases/11b); it is not in this version` to stderr and exits 2. The
  usage text no longer lists it. Its tests follow.
- Docs: top-level `AGENTS.md` (no Python, no physics tests in the gates; the
  "Physics runs" section becomes two lines saying the mode is being rebuilt
  and that the old run directories are history), `daemon/AGENTS.md` (gates),
  `README.md` (the Physics section says it is being rebuilt; Status and
  Provenance say no Python and no physics-intern code remain),
  `daemon/SPEC.md` (where it describes physics as Python, a sentence saying
  it was removed in 11a and is rebuilt in 11b).

## Done when

`git ls-files '*.py'` lists nothing. The Go gates in `daemon/AGENTS.md`,
`nix build`, `nix build .#soa-d` and `nix flake check` pass. One commit:
summary `repo: phase 11a (delete the physics Python)`, body listing what
changed, ending with the `Model:` line.
