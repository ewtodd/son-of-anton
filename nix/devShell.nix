# nix/devShell.nix — Go for the daemon, Python for the physics mode
{ ... }:
{
  perSystem =
    { pkgs, self', ... }:
    {
      devShells.default = pkgs.mkShell {
        packages = [
          pkgs.go
          pkgs.gopls
          # cgo, for the race-detector gate
          pkgs.gcc
          pkgs.ripgrep
          pkgs.bashInteractive
          pkgs.git
          # python3 with autophysicist installed and pytest
          self'.packages.physics-harness-test
        ];

        shellHook = ''
          # The daemon's execute_code and `son-of-anton problem` run this.
          export SON_OF_ANTON_PYTHON=${self'.packages.physics-harness-test}/bin/python3
          echo "Son of Anton dev shell. Go: cd daemon && go test ./...; physics: python3 -m pytest autophysicist/tests"
        '';
      };
    };
}
