# Phase 10c3: our own Nix, and one version

Goal: as in 10c1 (read its "Goal" and "How to write it"). The packaging
still has hermes-agent's shape and much of its text: of the files below,
between a tenth and nine tenths of each matches hermes-agent v0.20.5 (this
repository's first commit, `677b5c39`, has hermes's `flake.nix` and `nix/`).
This phase writes them anew. 10c1 and 10c2 run at the same time and change
Go under `daemon/internal/`; there, touch only what the version needs (a new
package for it is fine).

**Write from behaviour, not from the old files.** Work out what the packages
and modules must produce (below), then design the Nix yourself: your own
structure, names for internal bindings, and comments. A reordered or renamed
copy of the old file is still the old file; the reviewer compares the result
with hermes-agent's.

## Files

Write anew: `flake.nix`, `nix/checks.nix`, `nix/homeManagerModules.nix`,
`nix/moduleCommon.nix`, `nix/nixosModules.nix`, `nix/overlays.nix`,
`nix/son-of-anton.nix`. You may merge, split or rename files under `nix/`.
`nix/packages.nix`, `nix/devShell.nix`, `nix/physics-harness.nix` and
`nix/physics-runtime.nix` are this project's own: change them only as the new
layout needs.

## What must stay the same

- **Outputs**: `packages.{default,soa-d,physics-runtime,physics-harness,
  physics-harness-test}`, `overlays.default` (giving `son-of-anton`),
  `nixosModules.default`, `homeManagerModules.default`, `checks`,
  `devShells.default`, for `x86_64-linux` and `aarch64-linux`. Inputs as
  today.
- **The option interface.** Before changing anything, record every option
  under `services.son-of-anton` that the NixOS and Home Manager modules
  declare (path, type, default), evaluated as `nix/checks.nix` evaluates the
  modules, and keep the listing. At the end it must be identical, except
  `instances.<name>.backend`, which goes: it has one value, and `/etc/nixos`
  no longer sets it. Put both listings' diff in the report.
- **What the modules produce**, in effect (attribute order and store paths
  aside): the units (command, user, group, environment, sandboxing, restart
  triggers, `no_new_privs`), users and groups, tmpfiles rules, the activation
  steps (the `.env` merge, `soa-d config merge`, `soa-d rules merge`, the git
  key install), and the wrapped package (its `PATH`, `SON_OF_ANTON_PYTHON`,
  `SOA_D_BIN`, and `.override { extraPackages = [...]; }`). Compare the
  checks' configurations before and after and report what differs and why.
  The reviewer also builds the owner's real configuration against the branch
  and requires the same.
- **Checks**: the Go tests, the module evaluation checks, the package smoke
  and the physics tests, at least as strict as today.

## One version

Both commands report one date version, `YY.MM.DD`, the date of the commit the
package is built from: `son-of-anton --version` prints `son-of-anton
26.11.03`, `soa-d version` prints `soa-d 26.11.03`, and the daemon's `hello`
reply carries `26.11.03`. Nix passes it to the Go build (`-ldflags -X`) from
the flake's `lastModifiedDate`, and names the packages with it. A plain `go
build` reports `dev`. One place in the Go code holds it (a variable both
commands read); the `0.4.3` and `0.0.1` constants go. The `son-of-anton
status` output and its tests follow.

## Done when

`nix build`, `nix build .#soa-d`, `nix flake check` and the Go gates in
`daemon/AGENTS.md` pass. One commit: summary `nix: phase 10c3 (our own Nix,
one version)`, body listing what changed, ending with the `Model:` line.
Report: the option listing diff and every difference in what the modules
produce.
