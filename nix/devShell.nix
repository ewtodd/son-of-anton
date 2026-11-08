# nix/devShell.nix — Go for the daemon, plain python3 for execute_code's tests,
# and typst and pdftotext for the report tests
{ ... }:
{
  perSystem =
    { pkgs, self', ... }:
    let
      reportTypst = self'.packages.soa-d.reportTypst;
    in
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
          # the report tests render PDFs and read them back
          reportTypst
          pkgs.poppler-utils
        ];

        shellHook = ''
          export SON_OF_ANTON_PYTHON=${pkgs.python3}/bin/python3
          export SON_OF_ANTON_TYPST=${reportTypst}/bin/typst
          echo "Son of Anton dev shell. Go: cd daemon && go test ./..."
        '';
      };
    };
}
