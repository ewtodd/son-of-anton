# Phase 10b1: the activation merges in Go

Goal: NixOS and Home Manager activation run two Python scripts on every
rebuild, `nix/config_merge.py` (declared settings into `config.toml`) and
`nix/rules_merge.py` (declared rules into `memories/rules.toml`). The owner
is deleting the Python (phase 10b2), so both become soa-d subcommands with
the same behaviour, and activation calls them.

Read first: both Python files (their docstrings are the design),
`nix/configMergeScript.nix`, `nix/rulesMergeScript.nix`,
`nix/moduleCommon.nix` (where activation calls them), `nix/checks.nix`
(the check that runs the rules merge), and their Python tests
`tests/test_nix_config_merge.py` and `tests/test_nix_rules_merge.py`.

## The subcommands

```
soa-d config merge DECLARED CONFIG [--state STATE] [--adopt] [--dry-run]
soa-d rules merge DECLARED RULES --state STATE
```

The same arguments, the same three-way decisions, the same messages on
stderr (word for word) and the same exit codes as the Python. The files
they write must read back identically (as parsed TOML and JSON) to what the
Python writes, and each must take over from files the other wrote: the state
files `.nix-managed.json` and `.nix-rules.json` keep the Python's JSON
shapes, and `rules.toml` keeps TOML local dates (`added = 2026-11-01`) as
local dates, never datetimes. A file that does not parse is never
overwritten (as the Python).

## The contract: test vectors from the Python

The Python is the oracle. Before writing Go:

1. Write a scenario corpus covering every case the two Python test files
   cover, plus at least: first run without a state file; `--adopt`;
   `--dry-run`; a retired key with the on-disk value unchanged (removed) and
   changed (kept); keys Nix never declared (untouched); arrays of tables;
   quoted keys with `/` and `.` in them (`"unholy/glm5.3-flash"`); and for
   rules: add, update, keep-and-report, remove, user-added ids, an `added`
   local date, a rules file that does not parse.
2. Run the Python scripts on each scenario (in a temp directory) and record
   the inputs and the results (written files, stderr, exit code) under
   `daemon/cmd/soa-d/testdata/merge/` as JSON. The generator is a throwaway
   script: keep it out of the repo (it is Python, and the Python goes in
   10b2).
3. `daemon/cmd/soa-d/merge_test.go` runs `soa-d config merge` and `soa-d
   rules merge` (through `run`) on every recorded scenario and requires the
   same parsed files, stderr and exit code. Also: Go's output fed back to the
   Python, and the Python's output fed to Go, for a second run each, agree.

## Nix

`configMergeScript.nix` and `rulesMergeScript.nix` go; activation in
`moduleCommon.nix` runs `<the package's soa-d> config merge ...` and `...
rules merge ...` with the same arguments (the soa-d to use is the instance's
effective package's). The check in `checks.nix` that ran the rules merge runs
the Go one. `nix/config_merge.py`, `nix/rules_merge.py`,
`tests/test_nix_config_merge.py` and `tests/test_nix_rules_merge.py` are
deleted (the Go tests and vectors replace them).

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and `ruff
check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh`, `nix build`,
and `nix flake check` (including the NixOS module checks that run
activation). One commit: summary `nix: phase 10b1 (the activation merges in
Go)`, body listing what changed, ending with the `Model:` line.
