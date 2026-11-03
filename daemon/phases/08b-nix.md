# Phase 8b: an instance on soa-d (Nix)

Goal: moving an instance to soa-d is one option in `/etc/nixos`:
`services.son-of-anton.instances.<name>.backend = "soa-d"`. The instance's
unit then runs the Go daemon (Signal, cron, the socket the TUI uses)
instead of the Python gateway (SPEC section 15).

Read first: `SPEC.md` section 15, `nix/moduleCommon.nix`,
`nix/nixosModules.nix`, `nix/homeManagerModules.nix`, `nix/son-of-anton.nix`
and `nix/checks.nix`.

## Already written (do not modify)

The `nixos-soad-backend` check in `nix/checks.nix`.

## Decisions made without the owner (2026-11-03, night)

- One unit per instance, whichever backend: with `soa-d` the unit
  `son-of-anton-<name>` runs `soa-d serve`, so an instance's Python gateway
  and its soa-d can never both serve it (the Python's lock check would not
  stop it; see `phases/05a-signal.md`'s review note).
- The default stays `python` until the owner moves each instance.

## The option

`backend` (enum `"python"`, `"soa-d"`; default `"python"`) in the shared
options, so both modules have it.

- `python`: everything as now.
- `soa-d`: the unit's `ExecStart` is `<package>/bin/soa-d serve` (the home
  comes from `SON_OF_ANTON_HOME`, as now), its description "Son of Anton
  daemon (<name>)", everything else (user, hardening, paths, environment,
  `.env`, git) unchanged. The instance's `settings` gain
  `daemon = { instance = <name>; signal = true; cron = true; }`, defined so
  that any explicit `settings.daemon.*` wins key by key and the rest of the
  user's settings are untouched (the settings type merges with
  `recursiveUpdate` in definition order: put this definition first, e.g.
  with `lib.mkBefore`, rather than `mkDefault`, which would drop the whole
  definition as soon as the user sets any setting). The Home Manager module
  does the same without `instance`.

## The package

`nix/son-of-anton.nix` installs `$out/bin/soa-d`: `soa-d` wrapped with the
same runtime `PATH` suffix as `son-of-anton` (ripgrep, git, the language
servers and other `extraPackages`, ...) and `SON_OF_ANTON_PYTHON` set to the
sealed venv's `python3` (execute_code's interpreter). The `son-of-anton`
wrapper keeps putting soa-d on its `PATH`, so a TUI-started daemon behaves
the same as the unit's.

## README

"Deploying on NixOS": one paragraph: set `backend = "soa-d"` on an instance
to move it; the TUI then attaches to the instance's daemon; `soa-d import
--from ~/.son-of-anton` once to bring the Python sessions over.

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check` (with the new check). One commit: summary
`nix: phase 8b (an instance on soa-d)`, body listing what changed, ending
with the `Model:` line.
