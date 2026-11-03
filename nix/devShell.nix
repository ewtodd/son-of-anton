# nix/devShell.nix — Go for the daemon, plain python3 for execute_code's tests
{ ... }:
{
  perSystem =
    { pkgs, ... }:
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
          # the Go tests of execute_code run Python
          pkgs.python3
        ];

        shellHook = ''
          export SON_OF_ANTON_PYTHON=${pkgs.python3}/bin/python3
          echo "Son of Anton dev shell. Go: cd daemon && go test ./..."
        '';
      };
    };
}
