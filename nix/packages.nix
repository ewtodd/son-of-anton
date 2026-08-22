# nix/packages.nix — Son of Anton Agent package built with uv2nix
{ inputs, ... }:
{
  perSystem =
    { pkgs, ... }:
    let
      package = pkgs.callPackage ./son-of-anton.nix {
        inherit (inputs) uv2nix pyproject-nix pyproject-build-systems;
        # Only embed clean revs — dirtyRev doesn't represent any upstream
        # commit, so comparing it would always claim "update available".
        rev = inputs.self.rev or null;
      };
    in
    {
      packages.default = package;
    };
}
