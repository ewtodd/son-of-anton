# nix/configMergeScript.nix — merge Nix settings into an existing config.yaml
#
# Used by the NixOS and Home Manager activation scripts. The logic lives in
# ./config_merge.py rather than in a Nix string: it is a real three-way merge
# with a state file, and embedding it here would mean escaping every `${` and
# `''` a Python program happens to contain.
#
# Read through it with `builtins.readFile` and plain concatenation, NOT into an
# indented string — interpolation would treat the Python as Nix source.
{ pkgs }:
let
  python = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);
in
pkgs.writeScript "son-of-anton-config-merge" (
  "#!" + "${python}/bin/python3" + "\n" + builtins.readFile ./config_merge.py
)
