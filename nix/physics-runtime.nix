# nix/physics-runtime.nix — the interpreter physics computations run under.
#
# Deliberately NOT the agent's own venv. son-of-anton's `dependencies` carry a
# scope rule — "only packages used by EVERY session" — and this stack is ROOT
# plus a scientific Python environment, which would land on every deployment
# that never runs a physics turn. The physics modes execute model-authored
# scripts under a separate interpreter instead, named by `physics.python` in
# config.yaml and confined by bubblewrap.
#
# The lab's own library comes in as a flake input rather than being vendored or
# reimplemented. Analysis-Utilities is where the house conventions already live
# — CAEN/SOLARIS readers, waveform feature extraction, RooFit photopeak fits,
# a cached TTree -> DataFrame/ndarray loader, and the PlottingUtils style that
# makes a figure publication-ready without the agent inventing one. An agent
# that reaches for those gets the lab's answers; an agent left to write its own
# gets plausible ones.
#
# Built from Analysis-Utilities' OWN nixpkgs, not son-of-anton's. Three reasons,
# all of them practical: its `pythonPackage` is built against that nixpkgs'
# python3Packages, and mixing two nixpkgs in one `withPackages` env gives you
# two incompatible interpreters; ROOT there is already built and cached, while
# son-of-anton's pin would compile it (and scipy) from source; and the lab
# stack should move with the lab's flake, not with the agent's.
{
  system,
  analysis-utilities,
  extraPythonPackages ? (_ps: [ ]),
}:
let
  pkgs = import analysis-utilities.inputs.nixpkgs {
    inherit system;
    config.allowUnfree = true;
  };

  utils = analysis-utilities.packages.${system}.default;
  utilsPython = analysis-utilities.packages.${system}.pythonPackage;
  root = pkgs.root;

  pythonEnv = pkgs.python3.withPackages (
    ps:
    [
      utilsPython
    ]
    ++ (with ps; [
      # Numerics and fitting — the floor for any analysis task.
      numpy
      scipy
      sympy
      matplotlib
      pandas
      # Machine learning. The published analyses this is meant to reproduce
      # are scikit-learn + xgboost.
      scikit-learn
      xgboost
      joblib
      # ROOT files without a ROOT build, for the cases where PyROOT is the
      # wrong tool — streaming a branch, or a quick structural probe.
      uproot
      awkward
      h5py
      tqdm
    ])
    ++ extraPythonPackages ps
  );
in
# A wrapper, not a bare interpreter: PyROOT lives in $ROOTSYS/lib rather than a
# site-packages directory, and `libanalysis-utils.so` is found through
# LD_LIBRARY_PATH. The sandbox runs computations with a cleared environment, so
# anything the interpreter needs has to travel with the interpreter itself —
# including the handful of shell tools ROOT probes with while it starts up
# (`sed`, `ldd`), which otherwise print "command not found" over every run.
pkgs.runCommand "son-of-anton-physics-runtime"
  {
    nativeBuildInputs = [ pkgs.makeWrapper ];
    passthru = {
      inherit
        pythonEnv
        utils
        utilsPython
        root
        ;
    };
    meta = {
      description = "Interpreter for son-of-anton physics computations (ROOT + Analysis-Utilities + SciPy stack)";
      mainProgram = "python3";
    };
  }
  ''
    mkdir -p $out/bin
    makeWrapper ${pythonEnv}/bin/python3 $out/bin/python3 \
      --set ROOTSYS ${root} \
      --prefix PYTHONPATH : "${root}/lib" \
      --prefix LD_LIBRARY_PATH : "${root}/lib:${utils}/lib" \
      --prefix ROOT_INCLUDE_PATH : "${utils}/include:${root}/include" \
      --prefix PATH : "${
        pkgs.lib.makeBinPath [
          pkgs.coreutils
          pkgs.gnused
          pkgs.gnugrep
          pkgs.binutils
          # `ldd` — ROOT probes with it while resolving its own libraries.
          pkgs.glibc.bin
        ]
      }"
    ln -s $out/bin/python3 $out/bin/python
  ''
