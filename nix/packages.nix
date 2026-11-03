# nix/packages.nix — the Go daemon and command, the physics harness, and the wrapped package
{ inputs, ... }:
{
  perSystem =
    { pkgs, system, lib, ... }:
    let
      # YY.MM.DD of the commit this flake is built from.
      version = import ./version.nix { inherit (inputs) self; };

      # The Go daemon and the son-of-anton command (son-of-anton.nix wraps both).
      # Only daemon/ is the source, so physics changes never rebuild it.
      # Pure Go (CGO off); tests run in the check phase.
      soa-d = pkgs.buildGoModule {
        pname = "soa-d";
        inherit version;

        src = lib.cleanSource ../daemon;
        subPackages = [ "cmd/soa-d" "cmd/son-of-anton" ];
        env.CGO_ENABLED = "0";

        # One date version for both commands (internal/buildinfo).
        ldflags = [ "-X github.com/ewtodd/son-of-anton/daemon/internal/buildinfo.Version=${version}" ];

        # The terminal and search tools shell out to bash and ripgrep (and
        # use coreutils); the wrapper puts them on PATH at run time and the
        # check phase needs them too.
        nativeBuildInputs = [ pkgs.makeWrapper pkgs.bash pkgs.ripgrep pkgs.coreutils ];
        nativeCheckInputs = [ pkgs.bash pkgs.ripgrep pkgs.coreutils pkgs.git pkgs.python3 ];

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

        vendorHash = "sha256-kn0pKTfxgsVjpEai2HKeI3WRGGZCXgBfcWNhxfFr7hs=";
      };

      harness = import ./physics-harness.nix { inherit pkgs; };

      package = pkgs.callPackage ./son-of-anton.nix {
        # `son-of-anton` is its Go command; the TUI, -q and -z run on the daemon
        inherit soa-d;
        physicsHarness = harness.env;
      };
    in
    {
      packages.default = package;
      # The interpreter physics computations run under — separate from the
      # harness below on purpose, and built from the Analysis-Utilities flake's
      # nixpkgs rather than ours. See nix/physics-runtime.nix for why.
      packages.physics-runtime = import ./physics-runtime.nix {
        inherit system;
        inherit (inputs) analysis-utilities;
      };
      packages.soa-d = soa-d;
      # The Python that runs autophysicist (SON_OF_ANTON_PYTHON), and the same
      # with pytest, which the check and the dev shell use.
      packages.physics-harness = harness.env;
      packages.physics-harness-test = harness.testEnv;
    };
}
