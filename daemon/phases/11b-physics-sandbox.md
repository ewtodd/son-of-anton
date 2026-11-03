# Phase 11b: the physics sandbox

Goal: the physics mode is rebuilt on soa-d (phase 11c): a physics problem is
worked by an ordinary soa-d session that runs every command itself, inside a
sandbox, with a manager reviewing at the end of each work session. The
Python harness it replaces never produced a successful run; its runs went
into finding the data, the compiler and ROOT's headers, because the sandbox
had only the interpreter on PATH and mounted the data at a different path
than the problem named. This phase builds the sandbox and lets a session run
in it. Nothing starts physics runs yet.

Read first: `AGENTS.md`, `daemon/AGENTS.md`, `nix/physics-runtime.nix`,
`nix/son-of-anton.nix`, `nix/checks.nix`, `internal/tools/terminal.go`,
`internal/tools/process.go`, `internal/tools/state.go`, the file tools in
`internal/tools/`, `internal/agent/agent.go` (Runner, newSession), and how
the approval gate is applied to the terminal (`internal/approval`).

## 1. The runtime (nix/physics-runtime.nix)

`packages.physics-runtime`'s `bin/` holds everything a sandboxed command may
run, all from Analysis-Utilities' nixpkgs (so compiled code links against
the same ROOT the Python uses):

- `python3` and `python` as today (same wrapper environment), with `uproot`
  and `awkward` added to its packages;
- ROOT's `root`, `root-config` and `rootls`;
- the C and C++ compilers of that nixpkgs (`gcc`, `g++`, `cc`, `c++`) and
  `make`;
- `bash`, `sh`, coreutils, findutils, grep, sed, gawk, diffutils, `which`,
  `less`, `file`, gzip and tar.

`g++ -O2 prog.cpp $(root-config --cflags --libs) -o prog` must give a binary
that runs with nothing set up but this `PATH`. `bin/python3` keeps its path:
`/etc/nixos` sets `physics.python` to it.

## 2. The sandbox (internal/sandbox, new)

A policy and a function that turns a command into a bubblewrap invocation:

- `Policy`: `RunDir` (read-write, bound at its own path), `Data` (read-only,
  each bound at its own path), `Binds` (extra read-write paths, bound at
  their own paths: the session's private temp dir), `Runtime` (the runtime's
  `bin/` directory) and `Home` (`RunDir/.home`).
- bwrap: `--die-with-parent --new-session --unshare-all --clearenv`,
  `--ro-bind /nix/store /nix/store`, `--proc /proc`, `--dev /dev`,
  `--tmpfs /tmp`, `/bin/sh` and `/usr/bin/env` as symlinks into the runtime
  (so `#!/bin/sh` and `#!/usr/bin/env python3` work), `--ro-bind-try` of
  `/etc/passwd`, `/etc/group` and `/etc/localtime`, the binds above, and the
  environment `PATH=<Runtime>`, `HOME=<Home>`, `TMPDIR=/tmp`,
  `LANG=C.UTF-8`, `MPLBACKEND=Agg`, plus what the terminal tool already sets
  for a command (`SOA_MODEL`, its own variables). No network (the network
  namespace is unshared), nothing else of the host.
- **It fails closed.** With no `bwrap` on PATH, or when bwrap cannot create
  its namespaces, the command is not run, and the tool's result says the
  sandbox is unavailable and why. Nothing ever runs unsandboxed because the
  sandbox failed.

## 3. A session in the sandbox

`agent.Runner` gains `Sandbox *sandbox.Policy`. For such a session:

- **terminal and process** run every command inside the sandbox: the same
  wrapper as today (environment snapshot, cwd tracking, markers) runs as
  `bwrap ... -- bash -c WRAPPER`, with the session's private temp dir bound
  so the snapshot works. `cd` and `export` carry over between calls, as in
  any session. Background processes run inside it too and die with the
  session.
- **File tools** (read_file, write_file, patch, search_files, and any other
  tool that takes a path) read only under `RunDir`, the `Data` paths and
  `/nix/store`, and write only under `RunDir`. Paths are resolved, symlinks
  included, before the check. Anything else is refused with an error naming
  the allowed roots.
- **Approval**: the session runs in mode `yolo`; nobody is there to approve,
  and the sandbox is the boundary. Hardline rules still refuse.
- **Tools offered**: terminal, process, read_file, write_file, patch,
  search_files, todo. Nothing that reaches the network, the instance's memory
  or other sessions, and no clarify (nobody answers).

## 4. A way to try it

`soa-d sandbox-run --run DIR [--data PATH]... -- COMMAND...` runs one
command in the sandbox with the configured runtime and prints its output; the
exit code is the command's. The runtime is the directory of `physics.python`
(error if unset); each `--data` must lie under one of `physics.data_dirs`
(error otherwise); `DIR` must exist. The owner uses it on the work instance
to check the environment against the real data before phase 11c.

## Tests (contract, before the code)

Go tests, skipped with a message saying why when bwrap cannot create a user
namespace where they run (they run on the host with the Go gates):

- In a sandboxed session's terminal: a file written in `RunDir` is there
  afterwards; writing in a `Data` path fails; a file in a temp directory
  outside the binds (standing in for a home) cannot be read; a TCP connection
  to a listener the test opened on 127.0.0.1 fails; `PATH` is the runtime;
  `cd` and `export` persist across calls; a background process runs and is
  gone after Close.
- File tools: reading outside the roots, writing in a `Data` path, and
  reaching outside through a symlink in `RunDir` are refused.
- With no bwrap on PATH, the terminal returns the sandbox-unavailable error
  and runs nothing.
- A dangerous (non-hardline) command runs without asking; a hardline one is
  refused.
- `soa-d sandbox-run`: refuses data outside `physics.data_dirs`, refuses with
  `physics.python` unset, and runs a command otherwise.

A Nix check, `physics-runtime`, without bwrap: from the runtime's `bin/`,
`python3 -c 'import ROOT, uproot, awkward, numpy, scipy'`; a C++ program
compiled with `root-config` writes a TTree to a file and reads it back; an
RDataFrame in python3 sums a column of that file; `uproot` reads it.

## Done when

The gates in `daemon/AGENTS.md` pass, including the new check. One commit:
summary `daemon: phase 11b (the physics sandbox)`, body listing what changed,
ending with the `Model:` line.
