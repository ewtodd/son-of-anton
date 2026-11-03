# Phase 10b2: delete the Python

Goal: every instance runs soa-d and `son-of-anton` is Go (10a); the owner
asked for the Python to go. Everything Python goes except the physics mode
(`autophysicist/`, with `problems/`), which stays until its Go port
(phase 11) and becomes self-contained. The packaging drops uv: no
`pyproject.toml`, no `uv.lock`, no uv2nix. The NixOS and Home Manager
modules keep the option interface `/etc/nixos` uses (their from-scratch
rewrite is phase 10c).

Read first: `phases/10a-son-of-anton-command.md`,
`phases/10b1-merges-in-go.md`, `flake.nix`, every file in `nix/`, the
physics code (`autophysicist/`) and its tests (`tests/test_physics_*.py`,
`tests/test_workspace_continue.py`, `tests/test_physics_entry.py`).

## Delete (git rm)

- `agent/`, `tools/`, `gateway/`, `cron/`, `son_of_anton_cli/`, `locales/`,
  `scripts/` (all of it: the test runner, release script and the sandbox
  scripts), `assets/`.
- Every top-level `*.py` (`run_agent.py`, `cli.py`, `model_tools.py`,
  `toolsets.py`, `utils.py`, `mcp_serve.py`, `registration_lifecycle.py`,
  `son_of_anton_*.py`, ...).
- `tests/` except the physics tests above, which move to
  `autophysicist/tests/`.
- `pyproject.toml`, `uv.lock`, `.env.example`, `STATUS.md`.
- `nix/python.nix`, and anything else in `nix/` that exists only for the
  Python venv or its checks (the venv import sweep, the Python suite).

Untracked files in a working tree are not this phase's business.

## The physics mode stands alone

- Its imports from outside `autophysicist/` (config loading,
  `get_son_of_anton_home`, the custom provider's reasoning declaration,
  `get_model_context_length`, `clamp_effort`, `dump_toml`, the `.env`
  loader) become small functions of its own (one module, e.g.
  `autophysicist/support.py`), doing what the physics code needs of them,
  no more: config is `<home>/config.toml` read with `tomllib` (home:
  `$SON_OF_ANTON_HOME`, else `~/.son-of-anton`); `.env` is that home's
  `KEY=value` lines, loaded as `load_son_of_anton_dotenv` does (they
  override variables already set). Read each original for the exact
  behaviour the physics code relies on.
- `python -m autophysicist problem create|run` (10a's entry) is its only
  command line; the parser `son_of_anton_cli/subcommands/problem.py` built
  moves into it.
- Its tests pass under the new environment (below), adjusted only where they
  imported a deleted module; a test of the deleted Python's own behaviour
  (not the physics mode's) goes with it. List each adjustment.

## Packaging (Nix)

- Flake inputs: `nixpkgs`, `analysis-utilities`, `home-manager` (checks
  only), and whatever else the remaining Nix needs; uv2nix, pyproject-nix and
  pyproject-build-systems go.
- `packages.soa-d`: as today (both Go commands).
- `packages.physics-runtime`: as today.
- A physics harness environment from nixpkgs: `python3.withPackages` with
  what `autophysicist` imports (openai, httpx, mcp, rich, numpy, ...) and
  `autophysicist` itself importable, so `<python> -m autophysicist problem
  --help` works.
- `packages.default` (and the overlay's `son-of-anton`): `bin/son-of-anton`
  and `bin/soa-d`, wrapped with the runtime `PATH` (bash, coreutils, git,
  ripgrep, bubblewrap, and `extraPackages`), `SON_OF_ANTON_PYTHON` the
  harness environment's `python3` (it runs `son-of-anton problem` and
  `execute_code`), `SOA_D_BIN` the wrapped soa-d. It stays overridable with
  `.override { extraPackages = [...]; }` (`/etc/nixos` uses that).
- The modules: every option `/etc/nixos` sets keeps its name and meaning
  (`instances.<name>.{enable,user,createUser,managedAccount,son-of-antonHome,
  workingDirectory,stateDir,addToSystemPackages,environmentFiles,environment,
  settings,extraPackages,protectedPaths,git,memoryRules,memoryRulesFile,
  pruneUnmanagedSettings,package,backend,group}`). `backend` accepts only
  `"soa-d"` (its default now), so the Python gateway path, its units and
  environment variables go. Nothing in the modules refers to Python any more
  except `SON_OF_ANTON_PYTHON`.
- Checks: the Go package's tests (as today), the module evaluation checks
  (updated: no python backend), a check that runs the physics tests with
  pytest in the harness environment, and the package smoke (`son-of-anton
  --version`, `soa-d version`, `son-of-anton problem --help`).
- The dev shell: Go, gopls, the harness environment with pytest.

## Docs

- `AGENTS.md` (top level): rewritten for what the repo now is: the Go
  daemon and command (pointing at `daemon/AGENTS.md` and `daemon/SPEC.md`),
  the Nix packaging, the physics mode as Python until phase 11, the gates
  below, the commit convention (unchanged: copy it). Short.
- `daemon/AGENTS.md`: the gates (below).
- The README is rewritten separately; leave it.

## Gates (from now on)

In `daemon/`: `test -z "$(gofmt -l .)"`, `CGO_ENABLED=0 go vet ./...`,
`CGO_ENABLED=0 go test ./...`, and the race run of `./internal/server/
./internal/tui/ ./internal/client/`. From the root: `nix build`, `nix build
.#soa-d`, `nix flake check` (which runs the physics tests). No ruff, no
`scripts/run_tests.sh`.

## Done when

The gates pass; `git ls-files '*.py'` lists only `autophysicist/`. One
commit: summary `repo: phase 10b2 (delete the Python)`, body listing what
changed, ending with the `Model:` line.
