# nix/python.nix — uv2nix virtual environment builder
{
  python312,
  lib,
  callPackage,
  uv2nix,
  pyproject-nix,
  pyproject-build-systems,
  stdenv,
  # Filtered Python source (see son-of-anton.nix pythonSrc) — keeps
  # skills/plugins/nix/JS edits from invalidating the venv derivation.
  pythonSrc,
  dependency-groups ? [ "all" ],
}:
let
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = pythonSrc; };
  hacks = callPackage pyproject-nix.build.hacks { };

  overlay = workspace.mkPyprojectOverlay {
    sourcePreference = "wheel";
  };

  isAarch64Darwin = stdenv.hostPlatform.system == "aarch64-darwin";

  # Supply wheel-heavy packages from nixpkgs on aarch64-darwin so wheel-only
  # transitive artifacts do not break evaluation.
  mkPrebuiltPassthru = dependencies: {
    inherit dependencies;
    optional-dependencies = { };
    dependency-groups = { };
  };

  mkPrebuiltOverride =
    final: from: dependencies:
    hacks.nixpkgsPrebuilt {
      inherit from;
      prev = {
        nativeBuildInputs = [ final.pyprojectHook ];
        passthru = mkPrebuiltPassthru dependencies;
      };
    };

  pythonPackageOverrides =
    final: _prev:
    if isAarch64Darwin then
      {
        numpy = mkPrebuiltOverride final python312.pkgs.numpy { };
        pyarrow = mkPrebuiltOverride final python312.pkgs.pyarrow { };
      }
    else
      { };

  pythonSet =
    (callPackage pyproject-nix.build.packages {
      python = python312;
    }).overrideScope
      (
        lib.composeManyExtensions [
          pyproject-build-systems.overlays.default
          overlay
          pythonPackageOverrides
          # ``setup.py`` permits wheel/sdist creation only from the sealed
          # Son of Anton derivation. This is deliberately a derivation environment
          # variable, not a devShell variable: ``nix develop -c uv build``
          # must remain blocked.
          (final: prev: {
            son-of-anton = prev.son-of-anton.overrideAttrs (_old: {
              SON_OF_ANTON_NIX_BUILD = "1";
            });
          })
        ]
      );

  # The editable venv points at the live checkout, so it uses an
  # UNFILTERED workspace rooted at a real path — mkEditablePyprojectOverlay
  # computes relative paths via lib.path.splitRoot, which rejects the
  # filtered pythonSrc (a cleanSourceWith set, not a path).
  workspaceRoot = ./..;
  editableWorkspace = uv2nix.lib.workspace.loadWorkspace { inherit workspaceRoot; };
  editableOverlay = editableWorkspace.mkEditablePyprojectOverlay {
    root = "$SON_OF_ANTON_PYTHON_SRC_ROOT"; # resolved at shellHook time
  };

  editableSet = pythonSet.overrideScope (
    lib.composeManyExtensions [
      editableOverlay
      (final: prev: {
        son-of-anton = prev.son-of-anton.overrideAttrs (old: {
          # point straight at the real source instead of the filtered nix store copy
          src = workspaceRoot;
          nativeBuildInputs = old.nativeBuildInputs ++ final.resolveBuildSystem { editables = [ ]; };
        });
      })
    ]
  );
in
{
  venv = pythonSet.mkVirtualEnv "son-of-anton-env" {
    son-of-anton = dependency-groups;
  };
  editableVenv = editableSet.mkVirtualEnv "son-of-anton-editable-env" {
    son-of-anton = dependency-groups;
  };
}
