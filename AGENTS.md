# Son of Anton - Development Guide

Instructions for AI coding assistants and developers working on the son-of-anton codebase.

**Never give up on the right solution.**

## What Son of Anton Is

Son of Anton began as a hard fork of [Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent)
v0.20.5 (upstream commit `fcbd1076a9`), extended with the physics mode from
[huggingface/physics-intern](https://github.com/huggingface/physics-intern)
(commit `5553bb6`); none of either remains. Licensed 0BSD, with opencode-derived
TUI parts under opencode's MIT notice (see LICENSE). It is the successor to the archived
[temple](https://github.com/ewtodd/temple) harness. The Python agent is gone
(phase 10b2) and so is the Python physics mode (phase 11a); the repository has no
Python files. What remains is:

- **`daemon/`** - the Go daemon `soa-d` (always-on: Signal, cron, the socket the
  TUI attaches to) and the `son-of-anton` command (TUI client, `-q`, `-z`,
  `sessions`, `status`; `problem` only says the physics mode is being
  rebuilt). The module is `daemon/go.mod`, packages are
  under `daemon/internal/`, binaries under `daemon/cmd/`. Read
  `daemon/AGENTS.md` (working agreement, gates) and `daemon/SPEC.md` (the
  approved design); each phase's task is in `daemon/phases/`.
- **`nix/`, `flake.nix`** - the packaging and the NixOS and Home Manager
  modules. Nix is the only build and install path; there is no pip or uv.

Two properties shape almost every design decision:

- **Per-conversation prompt caching is sacred.** Never mutate past context,
  swap toolsets, or rebuild the system prompt mid-conversation (compaction is
  the one exception).
- **The core is a narrow waist; capability lives at the edges.** Prefer, in
  order: extend existing code, a CLI command, a service-gated tool, an MCP
  server, and only last a new core tool.

## Nix layout

- Inputs: `nixpkgs`, `flake-parts`, `analysis-utilities` (the physics
  runtime's library), `home-manager` (checks only).
- `packages.soa-d` - the Go package (both commands; its tests run in the check
  phase). `packages.default` (also the overlay's `son-of-anton`) - the two
  commands wrapped with the runtime `PATH` (`bash`, `coreutils`, `git`,
  `ripgrep`, `bubblewrap`, ..., plus `.override { extraPackages = [...]; }`),
  `SON_OF_ANTON_PYTHON` (plain nixpkgs `python3`, for `execute_code` and cron scripts) and `SOA_D_BIN`.
- `packages.physics-runtime` - the interpreter model-authored computations run
  under (ROOT, Analysis-Utilities, the SciPy stack).
- `nixosModules.default`, `homeManagerModules.default` - option interface
  unchanged for `/etc/nixos`; every instance runs `soa-d serve`.
- Checks (`nix flake check`): the Go tests, the module evaluation checks, the
  package smoke.
- `nix develop`: Go, gopls, gcc, plain `python3` (the Go tests of `execute_code` run it).

The flake only sees files in the git index: `git add` a new file before
`nix build` or `nix flake check`.

## Gates

There is no CI. Run them yourself before committing:

```bash
cd daemon
test -z "$(gofmt -l .)"
CGO_ENABLED=0 go vet ./...
CGO_ENABLED=0 go test ./...
CGO_ENABLED=1 go test -race ./internal/server/ ./internal/tui/ ./internal/client/
cd .. && nix build && nix build .#soa-d && nix flake check
```

Tests must not touch a real home: the Go tests use `t.TempDir()` homes.

### Commit convention

- Author: `son-of-anton-bot <307402699+son-of-anton-bot@users.noreply.github.com>`
  (repo git config already set - do not change it)
- Sole authorship. No `Co-authored-by:` trailers, for anyone - not the repo owner,
  not the model, not an upstream account.
- Name the model. A commit made by an agent ends its body with one line,
  `Model: <id>`: the exact model that wrote the change (`Model: claude-opus-5-5`,
  `Model: radiance/qwen3.8-27b`). Under `auto`, name the model that actually served
  it, never `auto`: maitred reports it in its `x-maitred-model` response header, and
  soa-d gives it to shell commands as `$SOA_MODEL`.
  It records provenance; it is not a co-author.
- Message: a short summary line (`area: what changed`), a blank line, then a body that
  explains what changed and why. Never cram several changes into one long subject line.
- Remote: `git@github.com:ewtodd/son-of-anton.git`, branch `main`
- Before merging anything, make sure the branch is up to date with `main`.

## Physics

The physics mode was removed in phase 11a and is being rebuilt in Go (phase 11b);
`son-of-anton problem` says so. The old run directories under workspaces are history.

## Deployment (the owner's instances)

e-workstation's instances are built from `/etc/nixos`'s `son-of-anton` input;
merging to `main` deploys nothing until that input is updated and e-workstation
is rebuilt with `nrs` (see /etc/nixos/AGENTS.md). Those installs are Nix-managed
with `pruneUnmanagedSettings`: settings live in
/etc/nixos/hosts/e-workstation/configuration.nix.

## Known pitfalls

- Never hardcode `~/.son-of-anton`; the home is `$SON_OF_ANTON_HOME`.
- Don't wire in dead code without end-to-end validation against a temp home.
- Dependencies in the Go module need a reason; standard library first.
