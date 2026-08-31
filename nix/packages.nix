# nix/packages.nix — Son of Anton Agent package built with uv2nix
{ inputs, ... }:
{
  perSystem =
    { pkgs, system, ... }:
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
      # The interpreter physics computations run under — kept out of the agent
      # venv on purpose, and built from the Analysis-Utilities flake's nixpkgs
      # rather than ours. See nix/physics-runtime.nix for why.
      packages.physics-runtime = import ./physics-runtime.nix {
        inherit system;
        inherit (inputs) analysis-utilities;
      };
    };
}
