# nix/overlays.nix — pkgs.son-of-anton for configurations outside this flake.
#
# It is this flake's own package, not a rebuild against the consumer's nixpkgs,
# so `pkgs.son-of-anton`, `nix build .#default` and the modules' default
# package are one derivation. `.override { extraPackages = [ ... ]; }` still
# works on it.
{ inputs, ... }:
{
  flake.overlays.default = final: _prev: {
    son-of-anton = inputs.self.packages.${final.stdenv.hostPlatform.system}.default;
  };
}
