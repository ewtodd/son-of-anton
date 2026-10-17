# nix/python.nix — uv2nix virtual environment builder
{
  python312,
  lib,
  callPackage,
  uv2nix,
  pyproject-nix,
  pyproject-build-systems,
  # Filtered Python source (see son-of-anton.nix pythonSrc) — keeps
  # nix/ edits from invalidating the venv derivation.
  pythonSrc,
  dependency-groups ? [ "all" ],
}:
let
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = pythonSrc; };

  overlay = workspace.mkPyprojectOverlay {
    sourcePreference = "wheel";
  };

  pythonSet =
    (callPackage pyproject-nix.build.packages {
      python = python312;
    }).overrideScope
      (
        lib.composeManyExtensions [
          pyproject-build-systems.overlays.default
          overlay
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
