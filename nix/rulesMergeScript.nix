# nix/rulesMergeScript.nix — seed memories/rules.toml from `memoryRules`
#
# Used by the NixOS and Home Manager activation scripts. The logic lives in
# ./rules_merge.py (a three-way merge by rule id, like ./config_merge.py), and
# it imports the TOML writer from ./config_merge.py, so both files are copied
# side by side and the script is run from there.
{ pkgs }:
let
  python = pkgs.python3;
  src = pkgs.runCommand "son-of-anton-rules-merge-src" { } ''
    mkdir -p $out
    cp ${./config_merge.py} $out/config_merge.py
    cp ${./rules_merge.py} $out/rules_merge.py
  '';
in
pkgs.writeShellScript "son-of-anton-rules-merge" ''
  exec ${python}/bin/python3 ${src}/rules_merge.py "$@"
''
