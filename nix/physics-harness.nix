# nix/physics-harness.nix — the Python the physics mode runs on, from nixpkgs.
#
# `autophysicist/` stays Python until its Go port (daemon/phases/11). This is
# the interpreter that runs it (`python3 -m autophysicist problem ...`, which
# `son-of-anton problem` execs through SON_OF_ANTON_PYTHON) and that soa-d's
# execute_code uses. It is not the interpreter physics *computations* run
# under: that is packages.physics-runtime (nix/physics-runtime.nix).
{ pkgs }:
let
  python = pkgs.python3;

  # The package is its source tree, installed whole: the prompts and the
  # default config are read from beside the code.
  autophysicist = python.pkgs.buildPythonPackage {
    pname = "autophysicist";
    version = "0.1.0";
    pyproject = false;

    src = pkgs.lib.cleanSourceWith {
      src = ../autophysicist;
      name = "autophysicist-source";
      filter =
        path: _type:
        let
          base = baseNameOf path;
        in
        base != "tests" && base != "__pycache__";
    };

    dontBuild = true;
    installPhase = ''
      runHook preInstall
      mkdir -p $out/${python.sitePackages}
      cp -r . $out/${python.sitePackages}/autophysicist
      runHook postInstall
    '';

    # What autophysicist imports. numpy is the spec builder's data probe;
    # mcp brings its own HTTP client.
    dependencies = with python.pkgs; [
      openai
      httpx
      mcp
      rich
      numpy
    ];
    doCheck = false;
  };

  withExtra = extra: python.withPackages (ps: [ autophysicist ] ++ extra ps);
in
{
  inherit autophysicist;
  # SON_OF_ANTON_PYTHON.
  env = withExtra (_: [ ]);
  # The same plus pytest, for the check and the dev shell.
  testEnv = withExtra (ps: [ ps.pytest ]);
}
