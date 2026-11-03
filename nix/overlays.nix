# nix/overlays.nix: adds `son-of-anton` to pkgs, for configurations that use
# this flake through an overlay.
#
# The attribute is packages.default itself, not a rebuild with the importing
# nixpkgs: the overlay, the modules' default and `nix build` give one store
# path, and `.override { extraPackages = [ ... ]; }` behaves the same on it.
{ inputs, ... }:
{
  flake.overlays.default = final: _prev: {
    son-of-anton = inputs.self.packages.${final.stdenv.hostPlatform.system}.default;
  };
}
