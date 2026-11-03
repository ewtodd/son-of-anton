# nix/physics-runtime.nix — the interpreter physics computations run under.
#
# Deliberately NOT the python of execute_code and cron scripts (plain nixpkgs
# python3). This stack is ROOT plus a scientific
# Python environment, which would land on every deployment that never runs a
# physics turn. The physics mode executes model-authored scripts under this
# interpreter instead, named by `physics.python` in config.toml and confined by
# bubblewrap.
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
# two incompatible interpreters; ROOT is the flake's own `packages.root` (a
# pinned ROOT master build), the one libanalysis-utils is linked against, so
# PyROOT and the library never load two ROOTs; and the lab stack should move
# with the lab's flake, not with the agent's.
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
  root = analysis-utilities.packages.${system}.root;

  # What the sandbox may run, named: bin/ holds these and nothing else of the
  # host. gcc is the wrapped compiler of this nixpkgs, so a program built with
  # `root-config` links the ROOT the Python uses and finds it through the rpath
  # root-config itself emits (-Wl,-rpath,${root}/lib).
  shellPackages = with pkgs; [
    bash
    coreutils
    findutils
    gnugrep
    gnused
    gawk
    diffutils
    which
    less
    file
    gzip
    gnutar
  ];

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
      h5py
      tqdm
      # ROOT I/O from python without PyROOT: uproot reads a TTree into numpy
      # and jagged awkward arrays. analysis_utils' loaders remain the fast path
      # for the lab's waveform branches (TArrayS/TArrayF object branches and
      # fixed-size array leaves are uproot's worst case: jagged arrays, no disk
      # cache, a per-event decode); uproot is for everything else.
      uproot
      awkward
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

    # ROOT: root and rootls carry ROOT's environment like python3 does;
    # root-config is linked as it is (it finds its own prefix from where the
    # link points).
    for prog in root rootls; do
      makeWrapper ${root}/bin/$prog $out/bin/$prog \
        --set ROOTSYS ${root} \
        --prefix LD_LIBRARY_PATH : "${root}/lib:${utils}/lib" \
        --prefix ROOT_INCLUDE_PATH : "${utils}/include:${root}/include"
    done
    ln -s ${root}/bin/root-config $out/bin/root-config

    # The compilers and make.
    for prog in gcc g++ cc c++; do
      ln -s ${pkgs.gcc}/bin/$prog $out/bin/$prog
    done
    ln -s ${pkgs.gnumake}/bin/make $out/bin/make

    # ROOT probes with ldd while it resolves its libraries (python3 carries it
    # in its own wrapper); a compiled program has only this PATH.
    ln -s ${pkgs.glibc.bin}/bin/ldd $out/bin/ldd

    # The shell and the usual tools.
    for pkg in ${pkgs.lib.concatMapStringsSep " " (p: "${pkgs.lib.getBin p}") shellPackages}; do
      for f in $pkg/bin/*; do
        [ -e "$out/bin/$(basename $f)" ] || ln -s $f $out/bin/$(basename $f)
      done
    done
  ''
