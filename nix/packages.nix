# nix/packages.nix — Son of Anton Agent package built with uv2nix
{ inputs, ... }:
{
  perSystem =
    { pkgs, system, lib, ... }:
    let
      # The Go daemon. Only daemon/ is the source, so Python changes never
      # rebuild it. Pure Go (CGO off); tests run in the check phase.
      soa-d = pkgs.buildGoModule rec {
        pname = "soa-d";
        version = "0.0.1";

        src = lib.cleanSource ../daemon;
        subPackages = [ "cmd/soa-d" ];
        env.CGO_ENABLED = "0";

        # The terminal and search tools shell out to bash and ripgrep (and
        # use coreutils); the wrapper puts them on PATH at run time and the
        # check phase needs them too.
        nativeBuildInputs = [ pkgs.makeWrapper pkgs.bash pkgs.ripgrep pkgs.coreutils ];
        nativeCheckInputs = [ pkgs.bash pkgs.ripgrep pkgs.coreutils pkgs.git ];

        # The contract tests live in internal/*, which subPackages would skip
        # in the default check phase; test the whole module instead.
        checkPhase = ''
          runHook preCheck
          go test ./...
          runHook postCheck
        '';

        postFixup = ''
          wrapProgram $out/bin/soa-d --prefix PATH : \
            "${pkgs.bash}/bin:${pkgs.ripgrep}/bin:${pkgs.coreutils}/bin"
        '';

        vendorHash = "sha256-MSix6HMa7NttemtLTpgFeGXuKQ0+UCPu2Suz+bF72uY=";
      };

      package = pkgs.callPackage ./son-of-anton.nix {
        inherit (inputs) uv2nix pyproject-nix pyproject-build-systems;
        # on the wrapper PATH: `son-of-anton` (TUI, -q, -z) runs on the daemon
        inherit soa-d;
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
      packages.soa-d = soa-d;
    };
}
