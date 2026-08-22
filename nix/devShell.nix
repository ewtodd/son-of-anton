# nix/devShell.nix — Python-only dev shell
{ ... }:
{
  perSystem =
    { pkgs, self', ... }:
    let
      package = self'.packages.default;
    in
    {
      devShells.default = pkgs.mkShell {
        packages = [
          pkgs.uv
          package
        ]
        ++ package.passthru.devDeps;

        shellHook = ''
          ${package.passthru.devShellHook}

          # for the editable venv to pick up the src
          export SON_OF_ANTON_PYTHON_SRC_ROOT=$(git rev-parse --show-toplevel)

          echo "Son of Anton dev shell in $SON_OF_ANTON_PYTHON_SRC_ROOT"
          echo "Run 'son-of-anton' to start."
        '';
      };
    };
}
